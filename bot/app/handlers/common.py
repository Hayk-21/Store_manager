"""Help, the catch-all, and the global error handler."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import ContextTypes

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable

log = logging.getLogger("storemanager.bot")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        texts.HELP.format(
            open_button=texts.BTN_OPEN,
            stock_button=texts.BTN_STOCK,
            end_button=texts.BTN_END_SHIFT,
        ),
        parse_mode=ParseMode.HTML,
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(texts.UNKNOWN_COMMAND)


async def dismiss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close a confirmation the worker decided against. Nothing else to do."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(texts.CANCELLED)


async def location_from_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Somebody sent a plain location, or tapped a location button, on desktop.

    Telegram Desktop cannot share a live location at all — no menu item, no
    button that works. Without this the message falls through to the write-up
    flow and the worker is told they are not on shift, which is true and
    baffling. The one useful thing to say is "do this on your phone".
    """
    await update.effective_message.reply_text(
        texts.LOCATION_ONLY_FROM_PHONE,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.off_shift(),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence: the worker always gets an answer, never silence."""
    error = context.error

    if isinstance(error, Conflict):
        # Two processes polling one token. During a Railway rollover the old
        # container is briefly still alive while the new one starts, which is
        # normal and resolves itself in seconds. A stack trace every time is
        # just noise that hides real problems — but if it keeps up after a
        # deploy settles, the bot service has more than one replica.
        log.warning("another process is polling this bot token (normal during a deploy)")
        return

    log.exception("update %s raised", update, exc_info=error)

    if not isinstance(update, Update) or update.effective_message is None:
        return
    if isinstance(error, (ApiError, ApiUnavailable)):
        await update.effective_message.reply_text(error.human())
    else:
        await update.effective_message.reply_text(texts.UNEXPECTED)
