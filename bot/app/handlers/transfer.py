"""Asking another shop for stock, and answering when yours is asked.

Two shops, two people. A cashier who needs a box cannot reach into a shelf they
are not standing at, so this is a request and not a command: it waits until
somebody at the other shop confirms the box exists and has left. Only then does
any count change, at either end.

The screen shows both directions, because they are the same conversation from
different sides — what we have asked for, and what we are being asked for.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.transfer")

# Its own range, clear of every other flow's.
PICK_STORE, PICK_ITEM, ASK_QUANTITY = range(70, 73)

MAX_QUANTITY = 10_000

_KEYS = ("tr_store", "tr_store_name", "tr_item", "tr_candidates", "tr_key")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def _fail(update, context, exc: Exception) -> int:
    _clear(context)
    message = update.effective_message
    if isinstance(exc, (ApiError, ApiUnavailable)):
        off_shift = isinstance(exc, ApiError) and exc.code == "no_open_session"
        await message.reply_text(
            exc.human(),
            reply_markup=keyboards.off_shift() if off_shift else keyboards.on_shift(),
        )
    else:
        log.exception("unhandled error in the transfer flow")
        await message.reply_text(texts.UNEXPECTED, reply_markup=keyboards.on_shift())
    return ConversationHandler.END


# -- the shared screen -------------------------------------------------------

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """What is waiting for an answer here, plus the way to ask for something."""
    _clear(context)
    try:
        result = await api.pending_transfers(update.effective_user.id)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    incoming = result.get("incoming") or []
    if incoming:
        body = texts.TRANSFER_INCOMING.format(
            rows="\n".join(
                texts.TRANSFER_INCOMING_ROW.format(
                    quantity=row["quantity"],
                    item=format.esc(row["item_name"]),
                    store=format.esc(row["to_store"]),
                )
                for row in incoming
            )
        )
    else:
        body = texts.TRANSFER_NOTHING_PENDING

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.transfer_menu(incoming),
    )
    return PICK_STORE


# -- asking ------------------------------------------------------------------

async def begin_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Which shop are we asking?"""
    query = update.callback_query
    await query.answer()
    try:
        result = await api.transfer_sources(update.effective_user.id)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    stores = result.get("stores") or []
    if not stores:
        # One shop, or the rest are closed. Nothing to ask, and saying so beats an
        # empty list that looks broken.
        await query.message.reply_text(
            texts.TRANSFER_NO_OTHER_STORES, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.TRANSFER_PICK_STORE, parse_mode=ParseMode.HTML,
        reply_markup=keyboards.transfer_stores(stores),
    )
    return PICK_STORE


async def choose_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    store_id = int(query.data.split(":", 1)[1])
    context.user_data["tr_store"] = store_id

    try:
        result = await api.transfer_items(update.effective_user.id, store_id)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    items = result.get("items") or []
    context.user_data["tr_store_name"] = result.get("store_name", "")
    if not items:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            texts.TRANSFER_STORE_EMPTY.format(
                store=format.esc(result.get("store_name", ""))
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.on_shift(),
        )
        return ConversationHandler.END

    context.user_data["tr_candidates"] = {str(i["id"]): i for i in items}
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.TRANSFER_PICK_ITEM.format(store=format.esc(result.get("store_name", ""))),
        parse_mode=ParseMode.HTML,
        # No prices. Choosing a box to ask for is not selling it.
        reply_markup=keyboards.item_choices(items, show_price=False),
    )
    return PICK_ITEM


async def search_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Typing part of a name, for a shop with more products than fit on a screen."""
    store_id = context.user_data.get("tr_store")
    if store_id is None:  # pragma: no cover
        return ConversationHandler.END

    text = (update.effective_message.text or "").strip()
    if not text:
        return PICK_ITEM

    try:
        result = await api.transfer_items(update.effective_user.id, store_id, text)
    except Exception as exc:  # noqa: BLE001
        return await _fail(update, context, exc)

    items = result.get("items") or []
    if not items:
        await update.effective_message.reply_text(
            texts.NOTHING_FOUND.format(query=format.esc(text)), parse_mode=ParseMode.HTML
        )
        return PICK_ITEM

    context.user_data["tr_candidates"] = {str(i["id"]): i for i in items}
    await update.effective_message.reply_text(
        texts.CHOOSE_ITEM,
        reply_markup=keyboards.item_choices(items, show_price=False),
    )
    return PICK_ITEM


async def choose_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    item = (context.user_data.get("tr_candidates") or {}).get(query.data.split(":", 1)[1])
    if item is None:  # pragma: no cover
        return PICK_ITEM

    context.user_data["tr_item"] = item
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.TRANSFER_ASK_QUANTITY.format(
            item=format.esc(item["name"]), available=item["count"]
        ),
        parse_mode=ParseMode.HTML,
    )
    return ASK_QUANTITY


async def choose_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The commit — of the request, not of the stock. Nothing moves yet."""
    item = context.user_data.get("tr_item")
    store_id = context.user_data.get("tr_store")
    if item is None or store_id is None:  # pragma: no cover
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
            texts.TRANSFER_TOO_MANY.format(available=item["count"])
        )
        return ASK_QUANTITY

    key = context.user_data.get("tr_key") or new_idempotency_key()
    context.user_data["tr_key"] = key

    try:
        result = await api.request_transfer(
            telegram_id=update.effective_user.id,
            from_store_id=store_id,
            item_id=item["id"],
            quantity=quantity,
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code in {"validation_error", "insufficient_stock"}:
            # The count at the other shop moved, or the number was wrong. Another
            # go at the number beats starting from the shop list again.
            context.user_data.pop("tr_key", None)
            return ASK_QUANTITY
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not request a transfer")
        _clear(context)
        await update.effective_message.reply_text(
            texts.UNEXPECTED, reply_markup=keyboards.on_shift()
        )
        return ConversationHandler.END

    _clear(context)
    try:
        transfer = result["transfer"]
        body = texts.TRANSFER_REQUESTED_OK.format(
            quantity=transfer["quantity"],
            item=format.esc(transfer["item_name"]),
            store=format.esc(transfer["from_store"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a transfer confirmation from %r", result)
        body = texts.TRANSFER_REQUESTED_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


# -- answering ---------------------------------------------------------------

async def decide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Approve or reject a request, from the shop being asked.

    Outside the conversation on purpose: the buttons live on a message that may be
    tapped an hour later, long after any flow has timed out.
    """
    query = update.callback_query
    await query.answer()
    _, raw_id, verdict = query.data.split(":")

    try:
        result = await api.decide_transfer(
            update.effective_user.id, int(raw_id), verdict == "y"
        )
    except (ApiError, ApiUnavailable) as exc:
        await query.message.reply_text(exc.human())
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not decide a transfer")
        await query.message.reply_text(texts.UNEXPECTED)
        return ConversationHandler.END

    # Decided. Whatever happens below is presentation.
    try:
        transfer = result["transfer"]
        template = (
            texts.TRANSFER_APPROVED
            if transfer["status"] == "approved"
            else texts.TRANSFER_REJECTED
        )
        body = template.format(
            quantity=transfer["quantity"],
            item=format.esc(transfer["item_name"]),
            store=format.esc(transfer["to_store"]),
        )
    except (KeyError, TypeError):
        log.exception("could not render a transfer decision from %r", result)
        body = texts.TRANSFER_DECIDED_PLAINLY

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


async def noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The line naming a request. A button only because Telegram cannot draw plain
    text above two buttons on their own row."""
    await update.callback_query.answer()
    return PICK_STORE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear(context)
    if update.callback_query is not None:
        await update.callback_query.answer()
        await update.callback_query.edit_message_reply_markup(reply_markup=None)
    await update.effective_message.reply_text(
        texts.CANCELLED, reply_markup=keyboards.on_shift()
    )
    return ConversationHandler.END


def escape(handler):
    """Leave this flow and run something else, like the others."""

    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        _clear(context)
        await handler(update, context)
        return ConversationHandler.END

    wrapped.__name__ = f"escape_{getattr(handler, '__name__', 'handler')}"
    wrapped.__wrapped__ = handler
    return wrapped
