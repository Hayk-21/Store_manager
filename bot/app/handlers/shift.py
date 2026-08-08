"""Opening the store, checking status, ending a shift, closing the store."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key

log = logging.getLogger("storemanager.bot.shift")

# A location older than this is being replayed rather than shared now. Generous
# enough for a slow connection, short enough that yesterday's pin is useless.
MAX_LOCATION_AGE_S = 120


async def _reply_error(update: Update, exc: Exception) -> None:
    if isinstance(exc, (ApiError, ApiUnavailable)):
        await update.effective_message.reply_text(exc.human())
    else:
        log.exception("unhandled error in a shift handler")
        await update.effective_message.reply_text(texts.UNEXPECTED)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — greet, and say whether a shift is already running."""
    user = update.effective_user
    try:
        # The profile name rides along so the owner never has to type one: they
        # register a Telegram id, and the name fills itself in here.
        me = await api.me(user.id, user.full_name, user.username)
    except Exception as exc:  # noqa: BLE001 - every failure is reportable to the worker
        await _reply_error(update, exc)
        return

    worker = me.get("worker")
    admin = me.get("admin")

    if worker is None:
        # An owner who is not also a cashier. Pressing /start is what binds their
        # Telegram account so login codes can be delivered — that already
        # happened server-side by the time this reply is sent.
        await update.effective_message.reply_text(
            texts.WELCOME_ADMIN.format(name=format.esc((admin or {}).get("label", ""))),
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    session = me.get("session")
    if session:
        await update.effective_message.reply_text(
            texts.WELCOME_ON_SHIFT.format(
                name=worker["name"],
                store=session["store_name"],
                since=format.clock(session["started_at"]),
            ),
            reply_markup=keyboards.on_shift(),
        )
    else:
        await update.effective_message.reply_text(
            texts.WELCOME.format(name=worker["name"], open_button=texts.BTN_OPEN),
            reply_markup=keyboards.off_shift(),
        )


async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The worker pressed "open". Ask for a live location; the server decides the rest.

    No location button here any more. ``request_location`` sends a single static
    point, which is exactly what is no longer accepted — offering it would teach
    the wrong gesture and then refuse it. Sharing a live location is four taps in
    the attachment menu, so the instructions are the interface.
    """
    # One key for this whole attempt at opening, reused if the location arrives
    # twice or the request has to be retried.
    context.user_data["open_key"] = new_idempotency_key()
    await update.effective_message.reply_text(
        texts.ASK_LOCATION,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.off_shift(),
    )


def _reject_faked_location(message) -> str | None:
    """Why this location should not be trusted, or None if it looks genuine.

    A live location is the one thing here that cannot be faked through the
    normal interface. A point dropped on the map and a real "send my current
    location" arrive as the same object -- ``horizontal_accuracy`` looked like
    it separated them and does not, since plenty of clients omit it on a
    perfectly genuine reading. But a *live* location is different in kind:
    Telegram streams it from the device and keeps editing the message as it
    moves, and there is no way to aim that at a chosen point.

    Two cheaper checks come first, because they give a more specific answer:

    * a forwarded location is someone else's, from whenever they sent it;
    * a message far older than now is an earlier reading being replayed.
    """
    if getattr(message, "forward_origin", None) or getattr(message, "forward_date", None):
        return texts.LOCATION_FORWARDED

    age = (datetime.now(UTC) - message.date).total_seconds()
    if age > MAX_LOCATION_AGE_S:
        return texts.LOCATION_STALE

    if not message.location.live_period:
        return texts.LOCATION_NOT_LIVE

    return None


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Requirement 8. The bot forwards the raw coordinate and nothing else."""
    message = update.effective_message
    location = message.location
    user = update.effective_user

    complaint = _reject_faked_location(message)
    if complaint is not None:
        log.warning(
            "refused a location from %s: %s (accuracy=%s, live=%s, forwarded=%s)",
            user.id, complaint[:40], location.horizontal_accuracy,
            location.live_period, bool(getattr(message, "forward_origin", None)),
        )
        await message.reply_text(
            complaint,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.off_shift(),
        )
        return

    key = context.user_data.get("open_key") or new_idempotency_key()
    context.user_data["open_key"] = key

    try:
        result = await api.open_store(
            telegram_id=user.id,
            lat=location.latitude,
            lng=location.longitude,
            accuracy_m=getattr(location, "horizontal_accuracy", None),
            key=key,
            telegram_name=user.full_name,
            telegram_username=user.username,
            live_period=location.live_period,
        )
    except ApiError as exc:
        if exc.code == "session_already_open":
            session = (exc.details or {}).get("session", {})
            await update.effective_message.reply_text(
                texts.SHIFT_ALREADY_OPEN.format(
                    store=session.get("store_name", "—"),
                    since=format.clock(session.get("started_at")),
                ),
                reply_markup=keyboards.on_shift(),
            )
            return
        # Out of range, vague GPS, unknown worker: the server already said why.
        await update.effective_message.reply_text(
            exc.human(), reply_markup=keyboards.off_shift()
        )
        return
    except Exception as exc:  # noqa: BLE001
        await _reply_error(update, exc)
        return

    context.user_data.pop("open_key", None)
    session = result["session"]
    await update.effective_message.reply_text(
        texts.SHIFT_OPENED.format(
            store=format.esc(session["store_name"]),
            distance=session["distance_m"],
            minutes=round((location.live_period or 0) / 60),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )


async def handle_live_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A live location moved.

    Telegram does not send a new message for this — it edits the original, once
    every few seconds to a few minutes depending on how much the device has
    moved. Each edit is one reading, forwarded and forgotten.

    Deliberately silent. A worker who gets a chat notification every time they
    walk to the counter will turn sharing off, which is the one outcome that
    breaks the feature. Failures are swallowed for the same reason: a missed
    reading is a gap in a trail, not something the worker did wrong and not
    something they can fix.
    """
    location = update.effective_message.location
    if location is None:  # pragma: no cover - the filter already guarantees this
        return
    try:
        await api.ping(
            telegram_id=update.effective_user.id,
            lat=location.latitude,
            lng=location.longitude,
            accuracy_m=getattr(location, "horizontal_accuracy", None),
        )
    except ApiError as exc:
        # "You are not on shift" is the normal end of the story: the worker
        # closed up but left sharing running. Nothing to say about it.
        if exc.code != "no_open_session":
            log.info("live location ping refused for %s: %s", update.effective_user.id, exc.code)
    except Exception:  # noqa: BLE001 - telemetry must never interrupt a shift
        log.warning("could not record a live location ping", exc_info=True)

    # Nothing else may see this update. Without stopping here it would fall
    # through to the write-up conversation, whose fallbacks would read it as the
    # worker abandoning the flow — every few seconds, all shift.
    raise ApplicationHandlerStop


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        me = await api.me(user.id, user.full_name, user.username)
    except Exception as exc:  # noqa: BLE001
        await _reply_error(update, exc)
        return

    session = me.get("session")
    if not session:
        await update.effective_message.reply_text(
            texts.STATUS_NO_SHIFT.format(open_button=texts.BTN_OPEN),
            reply_markup=keyboards.off_shift(),
        )
        return

    await update.effective_message.reply_text(
        texts.STATUS.format(
            store=format.esc(session["store_name"]),
            since=format.clock(session["started_at"]),
            duration=format.duration_since(session["started_at"]),
            receipts=session["sales"]["receipts"],
            sold=format.sold_summary(session["sales"]),
            cash=format.money(session["store_totals"]["cash"]),
            card=format.money(session["store_totals"]["card"]),
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.on_shift(),
    )


async def end_shift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Requirement 5, from the worker's side."""
    key = context.user_data.get("end_key") or new_idempotency_key()
    context.user_data["end_key"] = key
    try:
        result = await api.end_shift(update.effective_user.id, key)
    except Exception as exc:  # noqa: BLE001
        await _reply_error(update, exc)
        return
    context.user_data.pop("end_key", None)
    await report_end(update, result["summary"])



async def report_end(update: Update, summary: dict) -> None:
    message = texts.SHIFT_ENDED.format(
        duration=format.duration_minutes(summary.get("duration_minutes")),
        receipts=summary["sales"]["receipts"],
        sold=format.sold_summary(summary["sales"]),
        salary=format.money(summary["salary_deducted"]),
    )
    if summary.get("salary_halved"):
        message += texts.SALARY_HALVED.format(
            hours=summary.get("full_shift_hours", 8)
        )
    if summary.get("store_closed"):
        message += texts.STORE_CLOSED.format(
            cash=format.money(summary["store_totals_after"]["cash"]),
            card=format.money(summary["store_totals_after"]["card"]),
        )
    else:
        message += texts.STORE_STILL_OPEN

    await update.effective_message.reply_text(
        message, parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove()
    )
    await update.effective_message.reply_text(
        texts.WELCOME.format(name="", open_button=texts.BTN_OPEN).strip(),
        reply_markup=keyboards.off_shift(),
    )
