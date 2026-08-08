"""Writing off stock that broke, leaked or expired.

Deliberately not a sale at a price of zero. A sale means a customer and a
receipt, and filing a damaged vape as one would put it in the revenue figures,
the receipt count and the worker's bonus progress — and the last of those would
pay somebody for breaking things.

Nothing moves in the till: the money left when the stock was bought. What it
costs the shop is what we paid for it, and the server snapshots that.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.defect")

# Its own range, so a stray update can never be read as belonging to the sell
# flow or the write-up.
#
# Two steps, not three. A reason was asked for and then dropped: at the counter
# the answer to "what happened" is always some form of "it broke", so the step
# cost a tap and bought nothing. What the owner needs is which item and how
# many, and the report shows who wrote it off and when.
PICK_ITEM, ASK_QUANTITY = range(30, 32)

MAX_QUANTITY = 10_000

_KEYS = ("def_item", "def_qty", "def_key", "def_candidates")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def _fail(update: Update, context, exc: Exception) -> int:
    _clear(context)
    message = update.effective_message
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
    else:
        log.exception("unhandled error in the write-off flow")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    await update.effective_message.reply_text(
        texts.DEFECT_ASK_ITEM, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.selling(),
    )
    return PICK_ITEM


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = (update.effective_message.text or "").strip()
    if not query:
        await update.effective_message.reply_text(texts.CLOSEOUT_ASK_ITEM)
        return PICK_ITEM

    try:
        result = await api.search_items(update.effective_user.id, query)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    items = result["items"]
    if not items:
        await update.effective_message.reply_text(texts.NOTHING_FOUND.format(query=query))
        return PICK_ITEM

    context.user_data["def_candidates"] = {str(i["id"]): i for i in items}
    await update.effective_message.reply_text(
        # No prices. Writing off a broken vape needs its name and how many; what
        # the shop charges for it is not part of the job.
        texts.CHOOSE_ITEM, reply_markup=keyboards.item_choices(items, show_price=False)
    )
    return PICK_ITEM


async def choose_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item = (context.user_data.get("def_candidates") or {}).get(query.data.split(":", 1)[1])
    if item is None:  # pragma: no cover
        return PICK_ITEM

    context.user_data["def_item"] = item
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.DEFECT_ASK_QUANTITY.format(item=item["name"], available=item["count"])
    )
    return ASK_QUANTITY


async def choose_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    item = context.user_data.get("def_item")
    if item is None:  # pragma: no cover
        return ConversationHandler.END

    raw = (update.effective_message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if not (0 < quantity <= MAX_QUANTITY):
        await update.effective_message.reply_text(texts.BAD_QUANTITY)
        return ASK_QUANTITY
    if quantity > item["count"]:
        await update.effective_message.reply_text(
            texts.NOT_ENOUGH_STOCK.format(
                item=item["name"], available=item["count"], requested=quantity
            )
        )
        return ASK_QUANTITY

    context.user_data["def_qty"] = quantity
    return await _submit(update.effective_message, update.effective_user.id, context)


async def _submit(message, telegram_id: int, context) -> int:
    item = context.user_data.get("def_item")
    quantity = context.user_data.get("def_qty")
    if item is None or quantity is None:  # pragma: no cover
        return ConversationHandler.END

    key = context.user_data.get("def_key") or new_idempotency_key()
    context.user_data["def_key"] = key

    try:
        result = await api.write_off(
            telegram_id=telegram_id,
            item_id=item["id"],
            quantity=quantity,
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        _clear(context)
        await message.reply_text(exc.human(), reply_markup=keyboards.on_shift())
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not record a write-off")
        _clear(context)
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
        return ConversationHandler.END

    # Written off. Like a sale, nothing below here may report a failure.
    _clear(context)
    try:
        written = result["write_off"]
        body = texts.DEFECT_DONE.format(
            item=format.esc(written["name"]),
            quantity=written["quantity"],
            remaining=written["remaining_count"],
        )
    except (KeyError, TypeError):
        log.exception("could not render a write-off confirmation from %r", result)
        body = texts.DEFECT_DONE_PLAINLY

    await message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    message = update.effective_message
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await message.reply_text(texts.CANCELLED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


def escape(handler):
    """Leave the write-off flow and run something else, like the other flows."""

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear(context)
        await handler(update, context)
        return ConversationHandler.END

    wrapped.__name__ = f"escape_{getattr(handler, '__name__', 'handler')}"
    wrapped.__wrapped__ = handler
    return wrapped
