"""Recording a sale: type a name, tap the item, give a quantity, pick cash or card.

Typing is the fast path for a cashier who knows the catalogue, but a typo must
never sell the wrong SKU — so what is typed only ever *searches*. The thing that
actually gets sold is an item id from a button.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("vapestore.bot.sell")

ASK_ITEM, ASK_QUANTITY, ASK_PAYMENT = range(3)


async def _fail(update: Update, exc: Exception) -> int:
    message = update.effective_message
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
    else:
        log.exception("unhandled error in the sell flow")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


async def prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The "Վաճառք" button — but typing a name straight away works too."""
    await update.effective_message.reply_text(texts.ASK_ITEM)
    return ASK_ITEM


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(texts.ASK_ITEM)
        return ASK_ITEM

    try:
        result = await api.search_items(update.effective_user.id, query)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, exc)

    items = result["items"]
    if not items:
        await update.effective_message.reply_text(texts.NOTHING_FOUND.format(query=query))
        return ASK_ITEM

    context.user_data["candidates"] = {
        str(item["id"]): item for item in items
    }
    await update.effective_message.reply_text(
        texts.CHOOSE_ITEM, reply_markup=keyboards.item_choices(items)
    )
    return ASK_ITEM


async def choose_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item_id = query.data.split(":", 1)[1]
    item = (context.user_data.get("candidates") or {}).get(item_id)
    if item is None:  # the message is older than the conversation state
        await query.edit_message_text(texts.CANCELLED)
        return ConversationHandler.END

    context.user_data["item"] = item
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.ASK_QUANTITY.format(item=item["name"], available=item["count"])
    )
    return ASK_QUANTITY


async def choose_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if quantity <= 0:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY

    item = context.user_data.get("item")
    if item is None:  # pragma: no cover - state lost between restarts
        return ConversationHandler.END

    context.user_data["quantity"] = quantity
    # The key is minted here, at the last step before money moves, and survives
    # every retry of the request that follows.
    context.user_data["sale_key"] = new_idempotency_key()

    total = Decimal(item["sell_price"]) * quantity
    await update.effective_message.reply_text(
        texts.ASK_PAYMENT.format(
            item=item["name"], quantity=quantity, total=format.money(total)
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.payment_methods(),
    )
    return ASK_PAYMENT


async def choose_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    method = query.data.split(":", 1)[1]
    item = context.user_data.get("item")
    quantity = context.user_data.get("quantity")
    key = context.user_data.get("sale_key")
    if not (item and quantity and key):  # pragma: no cover
        await query.edit_message_text(texts.CANCELLED)
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)

    try:
        result = await api.sell(query.from_user.id, item["id"], quantity, method, key)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, exc)

    line = result["sale"]["lines"][0]
    prefix = texts.SALE_ALREADY_RECORDED + "\n" if result.get("duplicate") else ""
    await query.message.reply_text(
        prefix
        + texts.SALE_DONE.format(
            item=line["name"],
            quantity=line["quantity"],
            total=format.money(result["sale"]["total"]),
            method=texts.BTN_CASH if method == "cash" else texts.BTN_CARD,
            remaining=line["remaining_count"],
            cash=format.money(result["store_totals"]["cash"]),
            card=format.money(result["store_totals"]["card"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )
    _clear(context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is not None:
        await query.answer()
        await query.edit_message_text(texts.CANCELLED)
    else:
        await update.effective_message.reply_text(
            texts.CANCELLED, reply_markup=keyboards.on_shift()
        )
    _clear(context)
    return ConversationHandler.END


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Void the worker's own last receipt in this shift."""
    try:
        result = await api.void_last(update.effective_user.id)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, (ApiError, ApiUnavailable)):
            await update.effective_message.reply_text(exc.human())
        else:
            log.exception("unhandled error while voiding")
            await update.effective_message.reply_text(texts.UNEXPECTED)
        return

    await update.effective_message.reply_text(
        texts.VOID_DONE.format(
            total=format.money(result["voided"]["total"]),
            cash=format.money(result["store_totals"]["cash"]),
            card=format.money(result["store_totals"]["card"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("item", "quantity", "sale_key", "candidates"):
        context.user_data.pop(key, None)
