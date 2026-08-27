"""Correcting a receipt already rung up, from «Վիճակ».

A cashier makes mistakes at a counter: two instead of three, cash instead of card,
the flavour next to the one the customer asked for. The undo button under a fresh
confirmation covers the first ten seconds of that, and then the next sale scrolls it
away — after which the mistake was the owner's problem and the shop's figures were
wrong until somebody noticed.

This is the way back, and it is deliberately narrow:

* **Their own shift only.** The list is one work session's, and a work session
  belongs to one worker, so there is no argument by which a cashier reaches a
  colleague's receipt or yesterday's.
* **Cancel, not edit.** A receipt is never rewritten and never deleted. It keeps its
  row with ``voided_at`` set, the goods go back on the shelf, a reversing entry goes
  in the ledger, and the correct sale is entered afterwards as a new one. The owner
  sees both halves. An "edit" that quietly changed 3 to 2 would leave no trace of
  the 3, which is the shape a shortfall hides in.
* **Two taps.** The row is chosen from a list, then the whole receipt is read back
  before it goes. Cancelling the receipt above the one you meant is the obvious
  failure here, and a confirmation is what stops it.

Every handler lives outside the conversations. These buttons sit on a message that
may be tapped ten minutes and three sales later, which is the whole point of them.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api

log = logging.getLogger("storemanager.bot.records")

# What the server is asked for. Twenty rows is already a tall keyboard on a phone,
# and a receipt older than the twenty most recent of one shift is not what somebody
# is looking for when they say "I just got that wrong".
PAGE = 20


async def _fail(query, exc: Exception) -> None:
    """Say what went wrong on the screen the tap came from."""
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await query.message.reply_text(exc.human())
        return
    log.exception("could not answer a record correction")
    await query.message.reply_text(texts.UNEXPECTED)


async def show_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The shift's receipts, newest first, each one a button.

    Reached both from «Վիճակ» and from «⬅️ Ցուցակ» on a single receipt, so it is
    written to be drawn more than once. It replies with a new message rather than
    editing the one tapped: the status screen above it is worth keeping.
    """
    query = update.callback_query
    await query.answer()
    try:
        result = await api.shift_receipts(update.effective_user.id, PAGE)
    except Exception as exc:  # noqa: BLE001
        await _fail(query, exc)
        return

    receipts = result.get("receipts") or []
    if not receipts:
        await query.message.reply_text(texts.FIX_NOTHING)
        return

    body = texts.FIX_LIST + texts.FIX_HOW.format(sell_button=texts.BTN_SELL)
    total = result.get("total_receipts") or len(receipts)
    if total > len(receipts):
        body += texts.FIX_LIST_TRUNCATED.format(shown=len(receipts), total=total)

    await query.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.fix_list(receipts)
    )


async def pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """One receipt, read back in full, with the cancel button under it.

    The receipt is fetched again rather than carried in the button. Callback data is
    64 bytes and a receipt does not fit; more to the point, the list is a snapshot,
    and the thing being cancelled should be described from what the server says now
    rather than from what it said when the keyboard was drawn.
    """
    query = update.callback_query
    await query.answer()
    sale_id = int(query.data.split(":", 1)[1])
    try:
        result = await api.shift_receipts(update.effective_user.id, PAGE)
    except Exception as exc:  # noqa: BLE001
        await _fail(query, exc)
        return

    receipt = next(
        (r for r in (result.get("receipts") or []) if r.get("id") == sale_id), None
    )
    if receipt is None:
        # Ended shift, another window, or a keyboard older than the list behind it.
        await query.message.reply_text(
            texts.FIX_GONE.format(status_button=texts.BTN_STATUS)
        )
        return
    if receipt.get("voided"):
        await query.message.reply_text(texts.FIX_ALREADY_VOIDED)
        return

    method = (
        texts.METHOD_CASH if receipt.get("payment_method") == "cash" else texts.METHOD_CARD
    )
    body = texts.FIX_ONE.format(
        time=format.clock(receipt.get("sold_at")),
        lines=format.esc(receipt.get("lines") or "—"),
        total=format.money(receipt.get("total")),
        method=method,
    )
    if receipt.get("is_delivery"):
        body += texts.FIX_ONE_DELIVERY

    await query.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.fix_one(sale_id)
    )


async def void(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the chosen receipt.

    The same server call as the undo button under a fresh sale, named by id — so the
    server's rules about whose receipt this is and which shift it belongs to are the
    ones that decide, and they are not restated here.
    """
    query = update.callback_query
    await query.answer()
    sale_id = int(query.data.split(":", 1)[1])
    try:
        result = await api.void_last(
            update.effective_user.id, reason="ուղղում աշխատողի կողմից", sale_id=sale_id
        )
    except Exception as exc:  # noqa: BLE001
        await _fail(query, exc)
        return

    # The buttons go before the confirmation does: a cancel button left under a
    # receipt that has just been cancelled invites a second tap.
    await query.edit_message_reply_markup(reply_markup=None)
    try:
        body = texts.FIX_DONE.format(
            total=format.money(result["voided"]["total"]),
            cash=format.money(result["sold_totals"]["cash"]),
            card=format.money(result["sold_totals"]["card"]),
        )
    except (KeyError, TypeError):
        # Same reasoning as the undo button: a screen with no figures beats one with
        # wrong figures, and the receipt is cancelled either way.
        log.exception("could not render a correction confirmation from %r", result)
        body = texts.VOID_DONE_PLAINLY

    await query.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=keyboards.on_shift()
    )


async def already_voided(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on a receipt that is already cancelled.

    It answers and leaves the screen alone. The row is drawn so the cashier can see
    what they already did, not so they can do it again — and an unanswered callback
    spins on their phone until Telegram gives up on it.
    """
    await update.callback_query.answer(texts.FIX_ALREADY_VOIDED, show_alert=False)


async def close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Leave the correction screen having changed nothing.

    Its own word — «Փակվեց» — rather than the shared «Չեղարկվեց» of ``common.dismiss``.
    On a screen that asks whether to cancel a receipt, an answer reading "cancelled"
    is the one thing it must not say.
    """
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(texts.FIX_CLOSED)
