"""Recording a sale the moment it happens.

The end-of-shift write-up is still there and is still the safety net: a cashier
who serves customers all day without touching the bot can list everything at
close. But a shop that wants the stock count to be right *now* — because the
next customer asks whether there is one left — needs to record as it goes, and
there was no way to.

So both exist and they do not overlap: this commits one line immediately, the
write-up commits whatever was not recorded this way. A sale registered here is
already in the books, so it does not want listing again at close.

Nothing here decides anything about money or stock. It collects an item, a
quantity, a price and a payment method, and the server applies the lot in one
transaction — the same endpoint, the same rules and the same idempotency key
behaviour as everything else.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.sell")

# Numbered apart from the write-up's states so a stray update can never be read
# as belonging to the wrong flow.
PICK_ITEM, ASK_QUANTITY, ASK_PRICE, ASK_METHOD = range(20, 24)

MAX_QUANTITY = 10_000

_KEYS = ("sell_item", "sell_qty", "sell_price", "sell_kind", "sell_key",
         "sell_candidates", "sell_delivery")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def _fail(update: Update, context, exc: Exception) -> int:
    """Any failure ends the flow rather than leaving the cashier somewhere they
    cannot see. The server's own sentence is printed verbatim."""
    _clear(context)
    message = update.effective_message
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
    else:
        log.exception("unhandled error in the sell flow")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The cashier pressed "sell". Ask what went out."""
    _clear(context)
    await update.effective_message.reply_text(
        texts.ASK_ITEM, reply_markup=keyboards.selling()
    )
    return PICK_ITEM


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(texts.ASK_ITEM)
        return PICK_ITEM

    try:
        result = await api.search_items(update.effective_user.id, query)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    items = result["items"]
    if not items:
        await update.effective_message.reply_text(texts.NOTHING_FOUND.format(query=query))
        return PICK_ITEM

    context.user_data["sell_candidates"] = {str(i["id"]): i for i in items}
    await update.effective_message.reply_text(
        texts.CHOOSE_ITEM, reply_markup=keyboards.item_choices(items)
    )
    return PICK_ITEM


async def choose_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    item = (context.user_data.get("sell_candidates") or {}).get(query.data.split(":", 1)[1])
    if item is None:  # pragma: no cover - the buttons come from this dict
        await query.answer()
        return PICK_ITEM

    # Selling below zero is refused by the server anyway; saying so on the button
    # saves a round trip and keeps the cashier in the flow.
    if item["count"] <= 0:
        await query.answer(texts.OUT_OF_STOCK_ALERT.format(item=item["name"]), show_alert=True)
        return PICK_ITEM

    await query.answer()
    context.user_data["sell_item"] = item
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.ASK_QUANTITY.format(item=item["name"], available=item["count"])
    )
    return ASK_QUANTITY


async def choose_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item = context.user_data.get("sell_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if quantity <= 0:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if quantity > MAX_QUANTITY:
        await update.effective_message.reply_text(
            texts.QUANTITY_TOO_BIG.format(limit=MAX_QUANTITY)
        )
        return ASK_QUANTITY
    if quantity > item["count"]:
        await update.effective_message.reply_text(
            texts.NOT_ENOUGH_STOCK.format(
                item=item["name"], available=item["count"], requested=quantity
            )
        )
        return ASK_QUANTITY

    context.user_data["sell_qty"] = quantity
    await update.effective_message.reply_text(
        texts.CLOSEOUT_ASK_PRICE.format(
            item=format.esc(item["name"]),
            quantity=quantity,
            suggested=format.money(item["sell_price"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.suggested_prices(item),
    )
    return ASK_PRICE


async def choose_suggested_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    item = context.user_data.get("sell_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    if kind == "other":
        # Stay on this step and wait for a number. Nothing is decided yet.
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(texts.ASK_OTHER_PRICE)
        return ASK_PRICE

    price = item.get("wholesale_price") if kind == "wholesale" else item.get("sell_price")
    if price is None:  # pragma: no cover - the button is only offered when set
        price = item["sell_price"]
    context.user_data["sell_price"] = Decimal(price)
    context.user_data["sell_kind"] = kind
    await query.edit_message_reply_markup(reply_markup=None)
    return await _ask_method(query.message, context)


async def type_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = (update.effective_message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        price = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_PRICE
    if price < 0:
        await update.effective_message.reply_text(texts.CLOSEOUT_BAD_PRICE)
        return ASK_PRICE

    context.user_data["sell_price"] = price
    # The server files this as 'custom' only if it differs from the list price
    # it was offered as, so leaving a prefilled number alone is not a haggle.
    context.user_data["sell_kind"] = "custom"
    return await _ask_method(update.effective_message, context)


async def _ask_method(message, context) -> int:
    item = context.user_data["sell_item"]
    total = context.user_data["sell_price"] * context.user_data["sell_qty"]
    await message.reply_text(
        texts.ASK_PAYMENT.format(
            item=format.esc(item["name"]),
            quantity=context.user_data["sell_qty"],
            total=format.money(total),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.payment_methods(
            context.user_data.get("sell_delivery", False)
        ),
    )
    return ASK_METHOD


async def toggle_delivery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Tick or untick "delivery". Commits nothing and stays on this step.

    Only the keyboard changes, so the cashier can see the box is ticked before
    they decide how it was paid — and can untick it if they mistapped, which a
    button that sent the sale would not have allowed.
    """
    query = update.callback_query
    await query.answer()
    is_delivery = not context.user_data.get("sell_delivery", False)
    context.user_data["sell_delivery"] = is_delivery
    await query.edit_message_reply_markup(
        reply_markup=keyboards.payment_methods(is_delivery)
    )
    return ASK_METHOD


async def choose_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The commit. One key for this sale, reused by every retry of it."""
    query = update.callback_query
    await query.answer()
    method = query.data.split(":", 1)[1]
    is_delivery = context.user_data.get("sell_delivery", False)
    item = context.user_data.get("sell_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    key = context.user_data.get("sell_key") or new_idempotency_key()
    context.user_data["sell_key"] = key
    await query.edit_message_reply_markup(reply_markup=None)

    try:
        result = await api.sell(
            telegram_id=update.effective_user.id,
            item_id=item["id"],
            quantity=context.user_data["sell_qty"],
            payment_method=method,
            key=key,
            unit_price=str(context.user_data["sell_price"]),
            price_kind=context.user_data.get("sell_kind", "retail"),
            is_delivery=is_delivery,
        )
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    # Past this line the sale is in the books. Nothing below may report a
    # failure: telling a cashier their sale did not go through when it did is
    # how the same sale gets entered twice.
    _clear(context)
    sale_id = (result.get("sale") or {}).get("id")
    await query.message.reply_text(
        _confirmation(result, method), parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )
    # The undo sits on the confirmation itself, naming this sale. A cashier who
    # realises immediately should not have to find a menu, and naming the sale
    # means a later one cannot be reversed by mistake.
    if sale_id is not None:
        await query.message.reply_text(
            texts.UNDO_HINT, reply_markup=keyboards.undo_sale(sale_id)
        )
    return ConversationHandler.END


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reverse the sale this button belongs to.

    Registered outside the conversation: the sale is finished, so this is not
    part of entering one. The button carries its own sale id, so tapping it ten
    minutes and three sales later still reverses the right receipt.
    """
    query = update.callback_query
    await query.answer()
    sale_id = int(query.data.split(":", 1)[1])
    try:
        result = await api.void_last(
            update.effective_user.id, reason="սխալ գրանցում", sale_id=sale_id
        )
    except (ApiError, ApiUnavailable) as exc:
        await query.message.reply_text(exc.human())
        return
    except Exception:  # noqa: BLE001
        log.exception("could not undo sale %s", sale_id)
        await query.message.reply_text(texts.UNEXPECTED)
        return

    await query.edit_message_reply_markup(reply_markup=None)
    voided, totals = result["voided"], result["store_totals"]
    await query.message.reply_text(
        texts.VOID_DONE.format(
            total=format.money(voided["total"]),
            cash=format.money(totals["cash"]),
            card=format.money(totals["card"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )


def _confirmation(result: dict, method: str) -> str:
    """What to show once the server has taken the sale.

    Defensive on purpose. The receipt is already written, so a field missing
    from the response is a cosmetic problem — falling back to a plain "recorded"
    is right, and raising here would tell the cashier the opposite of the truth.
    """
    try:
        line = result["sale"]["lines"][0]
        totals = result["store_totals"]
        body = texts.SALE_DONE.format(
            item=format.esc(line["name"]),
            quantity=line["quantity"],
            total=format.money(line["line_total"]),
            method=texts.BTN_CASH if method == "cash" else texts.BTN_CARD,
            remaining=line["remaining_count"],
            cash=format.money(totals["cash"]),
            card=format.money(totals["card"]),
        )
    except (KeyError, IndexError, TypeError):
        log.exception("could not render a sale confirmation from %r", result)
        body = texts.SALE_RECORDED_PLAINLY

    if result.get("duplicate"):
        body = texts.SALE_ALREADY_RECORDED + "\n\n" + body
    return body


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Nothing was sent to the server yet, so backing out costs nothing."""
    _clear(context)
    message = update.effective_message
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await message.reply_text(texts.CANCELLED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


async def interrupted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The cashier started a sale and then asked to end the shift instead.

    The sale is dropped — nothing was sent to the server — and they are asked to
    press again. Handing straight over is not worth the machinery: the write-up
    is a different conversation, so starting it from inside this one would leave
    this one's state behind, and the next number typed would be read as a
    quantity for a sale that no longer exists.
    """
    _clear(context)
    await update.effective_message.reply_text(
        texts.SELL_INTERRUPTED, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


def escape(handler):
    """Wrap a handler so pressing its button also leaves the sell flow.

    Same reasoning as the write-up's version: a reply-keyboard tap arrives as
    plain text, and a state that consumes text would read the label as a product
    name and strand the cashier inside the flow.
    """

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear(context)
        await handler(update, context)
        return ConversationHandler.END

    wrapped.__name__ = f"escape_{getattr(handler, '__name__', 'handler')}"
    wrapped.__wrapped__ = handler
    return wrapped
