"""Counting the cash drawer, at both ends of a shift.

One question, asked twice. The worker leaving says what they are leaving in the
drawer; that money stays in the shop and is what the next session's till opens
with. The worker arriving says what they found, and the two figures are compared
for them — so a drawer that is short is noticed at the start of a shift by somebody
who did not cause it, rather than at the end by whoever gets blamed.

Neither answer changes the books. What the ledger says and what is in the drawer
are separate facts, and the gap between them is the thing worth recording.

Its own small conversation, entered by a button on a message rather than by a
keyboard label: the message may well be tapped minutes after the shift ended, long
after any flow of its own would have timed out.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.till")

ASK_AMOUNT = 80

_KEYS = ("till_kind", "till_key", "till_back")


def _keyboard_after(context):
    """The keyboard to leave behind.

    Taken from how the flow was entered rather than asked of the server. The entry
    point already knows: the main keyboard and the shift-*start* message both mean
    the worker is still working, and the shift-*end* message means they are not.
    Asking ``/me`` instead was a round trip to learn something already in hand — and
    it put a network call in a path that every test of this flow runs through.
    """
    return (
        keyboards.on_shift()
        if context.user_data.get("till_back") == "on_shift"
        else keyboards.off_shift()
    )


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The button on the shift-end or shift-start message."""
    query = update.callback_query
    await query.answer()
    kind = query.data.split(":", 1)[1]
    _clear(context)
    context.user_data["till_kind"] = kind
    # An opening count happens on a shift that is starting; a closing one on the
    # message that just ended it.
    context.user_data["till_back"] = "on_shift" if kind == "open" else "off_shift"

    # The button is gone once used, so a second tap cannot open a second count that
    # the server would then refuse under a different key.
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        texts.TILL_ASK_CLOSE if kind == "close" else texts.TILL_ASK_OPEN,
        parse_mode=ParseMode.HTML,
    )
    return ASK_AMOUNT


async def begin_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«Դրամարկղի մնացորդ» on the main keyboard, mid-shift.

    The same question as at the end of a shift, asked whenever the worker has the
    notes in their hand — counting up before locking the door and recording it then
    is the natural order, and waiting for the shift-end prompt means doing it from
    memory.

    Filed as a closing count, so it is what the next session's till opens with. A
    later count replaces it: the most recent one is the one that carries over, which
    means counting twice costs nothing and counting early is never wrong.
    """
    _clear(context)
    context.user_data["till_kind"] = "close"
    context.user_data["till_back"] = "on_shift"
    await update.effective_message.reply_text(
        texts.TILL_ASK_CLOSE,
        parse_mode=ParseMode.HTML,
        # Nothing but «Չեղարկել» while a number is expected, so a stray tap on the
        # main menu cannot be read as an amount.
        reply_markup=keyboards.selling(),
    )
    return ASK_AMOUNT


async def type_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The commit. Nothing here can be wrong except the number."""
    raw = (update.effective_message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        counted = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(texts.TILL_BAD_AMOUNT)
        return ASK_AMOUNT
    if counted < 0:
        await update.effective_message.reply_text(texts.TILL_BAD_AMOUNT)
        return ASK_AMOUNT

    kind = context.user_data.get("till_kind", "close")
    key = context.user_data.get("till_key") or new_idempotency_key()
    context.user_data["till_key"] = key

    try:
        result = await api.count_till(
            telegram_id=update.effective_user.id,
            kind=kind,
            counted=str(counted),
            key=key,
        )
    except (ApiError, ApiUnavailable) as exc:
        await update.effective_message.reply_text(exc.human())
        if isinstance(exc, ApiError) and exc.code == "validation_error":
            context.user_data.pop("till_key", None)
            return ASK_AMOUNT
        _clear(context)
        return ConversationHandler.END
    except Exception:  # noqa: BLE001
        log.exception("could not record a till count")
        _clear(context)
        await update.effective_message.reply_text(texts.UNEXPECTED)
        return ConversationHandler.END

    # Recorded. Nothing below may report a failure.
    #
    # Three ways in — the main keyboard mid-shift, the message that opens a shift,
    # and the one that ends it — and they must not all end on the same keyboard.
    # Read *before* clearing, or the answer is always the off-shift one.
    keyboard = _keyboard_after(context)
    _clear(context)
    try:
        body = _confirmation(result["count"], kind)
    except (KeyError, TypeError):
        log.exception("could not render a till count from %r", result)
        body = texts.TILL_DONE_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    return ConversationHandler.END


def _confirmation(count: dict, kind: str) -> str:
    """What was counted, and whether it agrees with the books.

    The difference is spelled out rather than left to be worked out, and it is not
    called an error: a drawer can be over as easily as short, and either way the
    worker is the person who can still explain it.
    """
    difference = Decimal(count["difference"])
    head = texts.TILL_DONE_CLOSE if kind == "close" else texts.TILL_DONE_OPEN
    body = head.format(counted=format.money(count["counted"]))

    if difference == 0:
        return body + texts.TILL_MATCHES
    if difference > 0:
        return body + texts.TILL_OVER.format(
            expected=format.money(count["expected"]),
            difference=format.money(difference),
        )
    return body + texts.TILL_SHORT.format(
        expected=format.money(count["expected"]),
        difference=format.money(-difference),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skipping the count. Allowed, because a worker standing at a locked shop with
    a queue behind them should not be held there by a number."""
    keyboard = _keyboard_after(context)
    _clear(context)
    if update.callback_query is not None:
        await update.callback_query.answer()
    await update.effective_message.reply_text(
        texts.TILL_SKIPPED, reply_markup=keyboard
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
