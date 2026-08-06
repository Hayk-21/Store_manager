"""Help, the catch-all, and the global error handler."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable

log = logging.getLogger("vapestore.bot")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        texts.HELP.format(
            open_button=texts.BTN_OPEN,
            undo_button=texts.BTN_UNDO,
            end_button=texts.BTN_END_SHIFT,
        ),
        parse_mode=ParseMode.HTML,
    )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(texts.UNKNOWN_COMMAND)


async def location_from_desktop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Someone typed instead of sharing a location. Say why the button matters."""
    await update.effective_message.reply_text(
        texts.LOCATION_ONLY_FROM_PHONE, reply_markup=keyboards.request_location()
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence: the worker always gets an answer, never silence."""
    error = context.error
    log.exception("update %s raised", update, exc_info=error)

    if not isinstance(update, Update) or update.effective_message is None:
        return
    if isinstance(error, (ApiError, ApiUnavailable)):
        await update.effective_message.reply_text(error.human())
    else:
        await update.effective_message.reply_text(texts.UNEXPECTED)
