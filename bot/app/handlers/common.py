"""Help, the catch-all, and the global error handler."""

from __future__ import annotations

import logging

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import ContextTypes

from app import keyboards, texts
from app.api import ApiError, ApiUnavailable, api

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


async def stray_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Something typed while no flow is waiting for it.

    Registered last, so it only ever sees text that every conversation declined.
    That happens for a reason worth catering to: conversation state lives in this
    process's memory, so a restart — or a deploy leaving two containers polling the
    same token for a few seconds — drops it. The worker is then mid-task as far as
    they know, and the bot had nothing at all to say back. Silence and a broken bot
    look identical from behind a counter.
    """
    await update.effective_message.reply_text(texts.LOST_THE_THREAD)


async def stray_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Չեղարկել» tapped with nothing to cancel.

    The button belongs to the flows, and each of them handles its own — so this
    only runs when the flow that drew it is gone. It was the worst of the dead ends
    to hit: a keyboard showing one button that did nothing, and no way back to the
    menu but /start.
    """
    context.user_data.clear()
    await update.effective_message.reply_text(
        texts.CANCELLED, reply_markup=await keyboard_for(update)
    )


async def stray_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any other button that nothing wanted.

    Registered last of all, so it only ever sees a label every flow declined — and
    the reason that can happen is that a reply keyboard outlives the flow that drew
    it. Telegram leaves the last one on screen until something replaces it, so a
    cashier can be looking at the write-up's buttons while the write-up is over, or
    at buttons belonging to a flow this process no longer remembers after a restart.

    It exists because the alternative is silence, and silence is the one answer a
    worker cannot act on: it is indistinguishable from the bot being down. This was
    not hypothetical — «Ավարտել իմ հերթափոխը» pressed inside the write-up matched
    nothing anywhere and answered nothing at all, twice, to somebody trying to go
    home. The flows have proper answers for their own buttons; this is the net under
    them, and it puts the right keyboard back, which is usually the whole repair.

    ``user_data`` is deliberately left alone. Something may still legitimately own a
    half-typed basket, and throwing it away to tidy up a stale keyboard would cost
    more than the confusion it fixes.
    """
    await update.effective_message.reply_text(
        texts.BUTTON_NOT_AVAILABLE, reply_markup=await keyboard_for(update)
    )


async def keyboard_for(update: Update):
    """The menu this worker should be looking at.

    Asked rather than assumed: showing the on-shift menu to somebody who is not on
    shift offers six buttons that all answer "you are not on shift", and showing
    the off-shift one to somebody who is invites them to open a store twice. If the
    question cannot be reached, the on-shift menu is the better guess — they were
    part-way through something, which means they were working.
    """
    user = update.effective_user
    try:
        me = await api.me(user.id)
    except (ApiError, ApiUnavailable):
        return keyboards.on_shift()
    if me.get("worker") is None:
        return ReplyKeyboardRemove()
    return keyboards.on_shift() if me.get("session") else keyboards.off_shift()


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
