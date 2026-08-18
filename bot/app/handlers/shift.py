"""Opening the store, checking status, ending a shift, closing the store."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationHandlerStop, ContextTypes

from app import format, keyboards, texts
from app.api import ApiError, ApiUnavailable, api, new_idempotency_key
from app.handlers import till

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
            # What the last person to lock up left in the drawer. Said here because
            # this is the moment the worker becomes answerable for it, and because
            # by closing time «the till was 1,000 heavier all along» is no longer
            # something anybody can check.
            drawer=format.drawer_line(session.get("till")),
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
            deliveries=format.delivery_line(session.get("deliveries")),
            # The drawer, and only the drawer. It stays the real figure — a delivery
            # paid in cash at the door is physically in there, and this is the number
            # the worker counts against at closing, so hiding part of it would make
            # their count come out over. The card line that used to sit beside it is
            # gone: card money is not in the drawer, it is the shop's income, and a
            # cashier's own card sales are already in «Ձեր վաճառքը» above.
            cash=format.money(session["store_totals"]["cash"]),
            # How much of that drawer is the float somebody else left. Without it
            # the cashier reads the whole figure as the day's cash and counts on
            # handing all of it over.
            carried=format.carried_line(session["store_totals"]),
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



async def report_end(update: Update, summary: dict, till_count: dict | None = None) -> None:
    """The shift read back. ``till_count`` is the drawer reading it closed with.

    There is one only when this shift shut the shop, because that is the only time
    it is asked for — and when there is, it is reported here rather than invited,
    since it has already been taken.
    """
    message = texts.SHIFT_ENDED.format(
        duration=format.duration_minutes(summary.get("duration_minutes")),
        receipts=summary["sales"]["receipts"],
        sold=format.sold_summary(summary["sales"]),
        salary=format.money(summary["salary_deducted"]),
    )
    # Only when there were any. A line reading «Առաքում՝ 0 պատվեր» on every shift
    # that never took one is noise on the screen a worker reads once.
    deliveries = summary.get("deliveries") or {}
    if deliveries.get("receipts"):
        # How many, not how much. The worker did not sell these and their wage and
        # bonus do not turn on them, so the amount is the owner's to know.
        message += texts.SHIFT_ENDED_DELIVERIES.format(receipts=deliveries["receipts"])
    if summary.get("salary_halved"):
        message += texts.SALARY_HALVED.format(
            hours=summary.get("full_shift_hours", 8)
        )
    message += _what_the_till_could_not_pay(summary)
    if summary.get("store_closed"):
        message += _the_closing_figures(summary["store_totals_after"])
    else:
        message += texts.STORE_STILL_OPEN

    # Two messages, not three. The reply keyboard rides on this one — it used to be
    # restored by re-sending the welcome, which greeted a worker who had just
    # finished with «Բարև, ։» and instructions for opening a shop they had closed.
    await update.effective_message.reply_text(
        message, parse_mode=ParseMode.HTML, reply_markup=keyboards.off_shift()
    )

    # Only whoever locked up is asked. The drawer belongs to the shop, not to a shift:
    # one of two cashiers going home cannot hand over the change their colleague needs
    # for the next four hours, and a count made while the shop is trading is stale on
    # the very next sale.
    if not summary.get("store_closed"):
        return

    # The drawer goes out last, and that ordering is the whole point: it was in the
    # middle once, and the message after it pushed the button up off the top of a
    # phone screen — the worker was told to count the till and then shown something
    # else, so they never saw the button and reasonably reported it missing.
    if till_count is not None:
        # Already answered — the shop does not shut without it. What is left to say
        # is what the figure came to: what stays here, and what they are carrying.
        await update.effective_message.reply_text(
            till.confirmation(till_count),
            parse_mode=ParseMode.HTML,
            reply_markup=keyboards.off_shift(),
        )
        return

    # No reading came back, which means the close did not ask for one — a service
    # older than the question. Whatever cash is left is the float the next shift
    # opens with, so somebody still has to say how much, and the person who just
    # locked up is the one who knows.
    await update.effective_message.reply_text(
        texts.TILL_HANDOVER_PROMPT,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.count_the_till(),
    )


def _the_closing_figures(totals: dict) -> str:
    """What is in the drawer as the shop shuts — and nothing else.

    The cash is left out when it came to less than nothing. A drawer cannot hold
    negative money, so that figure is a bookkeeping artefact rather than something a
    cashier can act on. «Կանխիկ՝ -4,500 ֏» is the one a worker saw and reported, and
    the honest answer to "how much cash is there" in that state is not a smaller
    negative number — it is nothing, and the line is better unsaid than said wrongly.
    Anything the till could not pay is named a few lines above.

    «Քարտով մուտք» used to stand beside it and no longer does. It was the shop's card
    income for the whole session — every delivery in it — reported to whoever happened
    to be last out of the door, at the moment they are reading their own day back.
    What they sold on card is stated above under their own name, and that is the only
    card figure a cashier has any use for.
    """
    cash = Decimal(str(totals.get("cash") or "0"))
    if cash < 0:
        return texts.STORE_CLOSED_NO_CASH
    return texts.STORE_CLOSED.format(cash=format.money(cash))


def _what_the_till_could_not_pay(summary: dict) -> str:
    """The part of the wage the drawer was too thin to cover.

    Worth its own sentence. A worker whose shop took the whole day on card goes home
    with less cash in hand than the wage they were quoted, and if nothing says so
    the number above looks like a mistake or a deduction. It is neither: it is money
    the owner owes them.
    """
    owed = Decimal(str(summary.get("salary_unpaid") or "0"))
    owed += Decimal(str(summary.get("bonus_unpaid") or "0"))
    if owed <= 0:
        return ""
    return texts.WAGE_STILL_OWED.format(owed=format.money(owed))
