"""Counting the drawer at the end of a shift.

One question, asked once, by the person who was standing at the drawer: how much are
you leaving in it. That amount becomes the shop's float — it stays on the premises
and is what the next shift opens with — and everything above it goes to the owner.

Nobody is asked at the *start* of a shift. That asked a worker to answer for a
drawer somebody else had filled, and the answer bound nobody to anything.

Reachable two ways: the button on the message that ends a shift, and «Դրամարկղի
մնացորդ» on the working keyboard, so it can be recorded while the notes are still in
hand rather than from memory once the door is locked.
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

_KEYS = ("till_key", "till_back")


def _clear(context) -> None:
    for key in _KEYS:
        context.user_data.pop(key, None)


def _keyboard_after(context):
    """The keyboard to leave behind.

    Taken from how the flow was entered rather than asked of the server. The entry
    point already knows: the working keyboard means the shift is still running, the
    message that ends one means it is not.
    """
    return (
        keyboards.on_shift()
        if context.user_data.get("till_back") == "on_shift"
        else keyboards.off_shift()
    )


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The button on the message that ends a shift."""
    query = update.callback_query
    await query.answer()
    _clear(context)
    context.user_data["till_back"] = "off_shift"

    # The button is gone once used, so a second tap cannot open a second count that
    # the server would then refuse under a different key.
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(texts.TILL_ASK_CLOSE, parse_mode=ParseMode.HTML)
    return ASK_AMOUNT


async def begin_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """«Դրամարկղի մնացորդ» on the working keyboard, mid-shift.

    The same question as at the end of a shift, asked whenever the worker has the
    notes in their hand. Counting up before locking the door and recording it then is
    the natural order; waiting for the shift-end prompt means doing it from memory.

    A later count replaces an earlier one, so counting twice costs nothing and
    counting early is never wrong.
    """
    _clear(context)
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

    key = context.user_data.get("till_key") or new_idempotency_key()
    context.user_data["till_key"] = key

    try:
        result = await api.count_till(
            telegram_id=update.effective_user.id, counted=str(counted), key=key
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

    # Recorded. Nothing below may report a failure. The keyboard is read before the
    # state is cleared, or the answer is always the off-shift one.
    keyboard = _keyboard_after(context)
    _clear(context)
    try:
        body = _confirmation(result["count"])
    except (KeyError, TypeError):
        log.exception("could not render a till count from %r", result)
        body = texts.TILL_DONE_PLAINLY

    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )
    return ConversationHandler.END


def _confirmation(count: dict) -> str:
    """What stays in the shop, and what goes to the owner.

    The handover is the figure the worker is about to act on — they are holding that
    money — so it is stated outright rather than left as a subtraction they do in
    their head at the door.

    There is no "the books disagree" line, and there deliberately cannot be one: the
    worker says what they are *leaving*, not what the whole drawer holds, so there is
    no second reading of the same quantity to compare. The one case worth naming is
    leaving more than the till is supposed to contain, which means either an
    unrecorded sale or a miscount, and either way the money stays put.
    """
    handed = Decimal(count["handed_over"] or 0)
    body = texts.TILL_DONE_CLOSE.format(counted=format.money(count["counted"]))

    if handed > 0:
        return body + texts.TILL_HANDED_OVER.format(handed=format.money(handed))
    if handed < 0:
        return body + texts.TILL_FOUND_EXTRA.format(extra=format.money(-handed))
    return body + texts.TILL_NOTHING_TO_HAND


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skipping the count. Allowed, because a worker standing at a locked shop with a
    queue behind them should not be held there by a number — and the shop's float
    simply stays whatever it already was."""
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
