"""The lifecycle of being open: store sessions and the shifts inside them.

Requirements 5, 6 and 8 live here.

A worker pressing "open" in the bot either starts a store session or joins the
one already running at that store. Pressing "end my shift" pays that worker's
salary out of the till. When the last shift ends — or when the owner or the
auto-close task intervenes — the store session closes, its cash and card are
snapshotted onto the row, and the store's visible total goes back to zero.

Store sessions and shifts are in one module because closing a store ends its
shifts and ending the last shift closes the store; splitting them would only buy
a circular import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg

from app.config import settings
from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import tracking as tracking_repo
from app.repo import workers as workers_repo
from app.services import geofence
from app.services import till as till_service
from app.services.geofence import require_store

log = logging.getLogger("storemanager.shifts")

ZERO = Decimal("0.00")

# A full day behind the counter. Shorter than this and the shift wage is halved:
# the figure is a day's pay, and somebody who left after two hours has not worked
# a day. One step rather than pay-by-the-minute, because the wage is agreed as a
# day rate and billing it by the second would turn every late start into an
# argument about four minutes.
FULL_SHIFT_HOURS = 8
SHORT_SHIFT_FRACTION = Decimal("0.5")

# What a bonus row says on the owner's ledger. Armenian, like every other note there.
BONUS_NOTE = "Բոնուս՝ {period} վաճառքը {sold} ֏ (նպատակը՝ {threshold} ֏)"
BONUS_PERIODS = {"day": "օրվա", "month": "ամսվա"}


def salary_for_hours_worked(full: Decimal, started_at, ended_at=None) -> Decimal:
    """What a shift wage actually comes to, given how long the shift ran.

    ``started_at`` may be missing on rows that predate the column being selected;
    that reads as a full shift rather than as a free one, because guessing against
    the worker over a bookkeeping gap is the wrong way round.
    """
    if full <= ZERO or started_at is None:
        return full if full > ZERO else ZERO
    end = ended_at or datetime.now(UTC)
    hours = (end - started_at).total_seconds() / 3600
    if hours >= FULL_SHIFT_HOURS:
        return full
    return (full * SHORT_SHIFT_FRACTION).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Worker:
    """The caller of a bot request, already resolved to an owner."""

    id: int
    owner_id: int
    name: str
    salary_amount: Decimal
    # 'shift' -- paid out of the till when the shift ends, which the system
    # settles itself. 'month' -- a monthly wage the owner pays separately, so
    # ending a shift costs the till nothing.
    salary_period: str = "shift"

    @property
    def salary_due_at_shift_end(self) -> Decimal:
        return self.salary_amount if self.salary_period == "shift" else ZERO


# -- opening -----------------------------------------------------------------

# The shortest span Telegram offers for sharing a live location. Requiring at
# least this means every one of its three choices (15 minutes, 1 hour, 8 hours)
# is accepted, while a value that could only have been synthesised is not.
MIN_LIVE_PERIOD_S = 15 * 60


def require_live(live_period: int | None) -> int:
    """Refuse anything but a genuinely live position.

    This is the one check that cannot be worked around through the normal
    interface. A dropped pin and a real reading arrive as the same object, and
    ``horizontal_accuracy`` does not separate them — plenty of clients omit it on
    a genuine one. A live location is different in kind: Telegram streams it from
    the device and edits the message as it moves, so it cannot be aimed at a
    chosen point.

    Enforced here rather than only in the bot, because the bot is a client. The
    rule belongs where the other rules that decide whether a shift may open are.
    """
    if not live_period:
        raise BotError("location_not_live")
    if live_period < MIN_LIVE_PERIOD_S:
        raise BotError(
            "location_not_live",
            details={"live_period": live_period, "minimum_s": MIN_LIVE_PERIOD_S},
        )
    return live_period


async def open_store(
    worker: Worker,
    lat: float,
    lng: float,
    accuracy_m: float | None,
    idempotency_key: str,
    live_period: int | None = None,
) -> dict:
    """Requirement 8: located, matched to a store, attached to it."""
    replay = await sessions_repo.by_start_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return _open_payload(worker, replay, duplicate=True)

    require_live(live_period)
    match = await require_store(worker.owner_id, lat, lng, accuracy_m)
    store = match.matched

    try:
        async with db.transaction() as conn:
            store_session_id = await sessions_repo.open_store_session(
                conn, worker.owner_id, store.id, worker.id, settings.local_day()
            )
            joined_existing = store_session_id is None
            if joined_existing:
                # Somebody else already opened this store; join their session.
                existing = await sessions_repo.lock_open_for_store(
                    conn, worker.owner_id, store.id
                )
                if existing is None:  # pragma: no cover - closed between the two statements
                    raise BotError("store_not_open")
                store_session_id = existing["id"]
            else:
                # A shop that keeps cash in the drawer overnight still has it in the
                # morning, and the till of a fresh session has to start there — or
                # the first sale of the day makes the drawer look that much heavy.
                # Only for a session actually being opened: joining one that is
                # already running must not add the float a second time.
                await till_service.carry_over_float(
                    conn, worker.owner_id, store.id, store_session_id
                )

            work_session_id = await sessions_repo.open_work_session(
                conn,
                owner_id=worker.owner_id,
                worker_id=worker.id,
                store_id=store.id,
                store_session_id=store_session_id,
                lat=lat,
                lng=lng,
                distance_m=store.distance_m,
                idem_key=idempotency_key,
            )
            await tracking_repo.set_live_window(conn, work_session_id, live_period)
            # The opening position is the first point of the trail, so the track
            # starts where the shift did rather than at whenever the first
            # movement happened to arrive.
            await tracking_repo.record_ping(
                conn,
                owner_id=worker.owner_id,
                worker_id=worker.id,
                work_session_id=work_session_id,
                store_id=store.id,
                lat=lat,
                lng=lng,
                distance_m=store.distance_m,
                accuracy_m=int(accuracy_m) if accuracy_m is not None else None,
                in_range=True,
            )
            await tracking_repo.mark_position(
                conn, work_session_id, lat, lng, store.distance_m, in_range=True
            )
    except asyncpg.exceptions.UniqueViolationError as exc:
        return await _resolve_open_conflict(worker, idempotency_key, exc)

    log.info(
        "worker %s opened shift %s at store %s (%s m, %s)",
        worker.id, work_session_id, store.id, store.distance_m,
        "joined" if joined_existing else "new session",
    )
    row = await sessions_repo.by_start_idem(worker.owner_id, idempotency_key)
    return _open_payload(worker, row, duplicate=False)


async def _resolve_open_conflict(
    worker: Worker, idempotency_key: str, exc: asyncpg.exceptions.UniqueViolationError
) -> dict:
    """A collision means one of two opposite things, and the key tells them apart.

    Ask about the idempotency key *first*, whichever index actually fired. Two
    simultaneous retries of one tap trip ``one_open_session_per_worker`` just as
    readily as the idempotency index — Postgres reports whichever it reached —
    so branching on the constraint name would answer 409 to a caller who simply
    retried. Blocking on the conflicting row means the winner has committed by
    the time we get here, so this lookup sees it.
    """
    replay = await sessions_repo.by_start_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return _open_payload(worker, replay, duplicate=True)

    constraint = getattr(exc, "constraint_name", "") or ""
    if constraint == "one_open_session_per_worker":
        # A genuinely different tap while a shift is already running.
        open_shift = await sessions_repo.open_for_worker(worker.id)
        raise BotError(
            "session_already_open",
            details={
                "session": {
                    "id": open_shift["id"],
                    "store_id": open_shift["store_id"],
                    "store_name": open_shift["store_name"],
                    "started_at": open_shift["started_at"].isoformat(),
                }
            }
            if open_shift
            else {},
        )

    raise  # pragma: no cover - an unexpected constraint is a bug, not a user error


def _open_payload(worker: Worker, row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "session": {
            "id": row["id"],
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "store_session_id": row["store_session_id"],
            "started_at": row["started_at"].isoformat(),
            "distance_m": row["start_distance_m"],
        },
        "worker": {
            "id": worker.id,
            "name": worker.name,
            "salary_amount": f"{worker.salary_amount:.2f}",
        },
    }


# -- tracking ----------------------------------------------------------------

async def record_position(
    worker: Worker, lat: float, lng: float, accuracy_m: float | None = None
) -> dict:
    """One reading from a live location that is already running.

    Recorded, never enforced. A cashier who steps out to the bank has not ended
    their shift, and a bot that decided otherwise from a GPS reading would be
    wrong often enough to be worse than useless. The owner sees where people
    were; the system does not act on it.

    Distance is measured to the store the shift is attached to, not to the
    nearest one — the question is "how far from your post", and the nearest shop
    to a worker on an errand may well be a different branch.
    """
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    store = await stores_repo.get(worker.owner_id, shift["store_id"])
    distance = None
    in_range = True
    if store is not None and store["lat"] is not None:
        distance = await geofence.distance_to(store["lat"], store["lng"], lat, lng)
        in_range = distance <= store["radius_m"]

    async with db.transaction() as conn:
        await tracking_repo.record_ping(
            conn,
            owner_id=worker.owner_id,
            worker_id=worker.id,
            work_session_id=shift["id"],
            store_id=shift["store_id"],
            lat=lat,
            lng=lng,
            distance_m=distance,
            accuracy_m=int(accuracy_m) if accuracy_m is not None else None,
            in_range=in_range,
        )
        await tracking_repo.mark_position(
            conn, shift["id"], lat, lng, distance, in_range=in_range
        )

    return {
        "ok": True,
        "distance_m": distance,
        "in_range": in_range,
        "store_name": shift["store_name"],
    }


# -- closing -----------------------------------------------------------------

async def _pay_and_close_shift(
    conn,
    shift,
    salary: Decimal,
    *,
    lat: float | None,
    lng: float | None,
    idem_key: str | None,
    closed_by: str,
) -> Decimal:
    """Requirement 5: end one shift and take its salary out of the till.

    Returns what the till actually paid, which is not always what the shift cost —
    see ``_pay_from_the_till``.

    Every way a shift can end comes through here — the worker pressing "end my
    shift", the write-up, the last one out closing the store, the owner forcing it
    and the auto-close — which is why the short-shift halving is applied here and
    not at each of those call sites. Five copies of one rule is five chances for
    them to disagree about what a day's work is.
    """
    salary = salary_for_hours_worked(salary, shift["started_at"])
    await sessions_repo.close_work_session(
        conn, shift["id"], salary, lat, lng, idem_key, closed_by
    )

    # The wage first, then the bonus, and both only as far as the drawer reaches.
    # The order is the priority: a wage is owed for work done, a bonus is on top
    # of it, and when there is not enough cash for both the wage is the one that
    # should be in the worker's hand.
    unpaid_salary = await _pay_from_the_till(conn, shift, "salary", salary, note=None)
    unpaid_bonus = await pay_bonus_if_earned(conn, shift)
    if unpaid_salary or unpaid_bonus:
        await sessions_repo.record_unpaid(conn, shift["id"], unpaid_salary, unpaid_bonus)
    return salary - unpaid_salary


async def _pay_from_the_till(conn, shift, kind: str, amount: Decimal, note) -> Decimal:
    """Take a wage out of the drawer, as far as the drawer goes. Returns the rest.

    Cash in a drawer cannot be negative, and a till that says it is makes every
    figure downstream describe money that is not there — a closing snapshot below
    zero, a handover of a negative amount, a worker told the drawer held more than
    expected when in fact the wage had overdrawn it.

    So what the till cannot cover is not booked against the till. It is a debt:
    the owner settles it from the card money or their own pocket, and the shift
    row carries the figure so nobody has to remember.
    """
    if amount <= ZERO:
        return ZERO

    in_the_till = Decimal(
        (await money_repo.totals_on(conn, shift["store_session_id"]))["cash"]
    )
    payable = min(amount, max(ZERO, in_the_till))
    if payable <= ZERO:
        log.info(
            "the till at store %s could not cover %s of %s for shift %s",
            shift["store_id"], kind, amount, shift["id"],
        )
        return amount

    try:
        await money_repo.insert_movement(
            conn,
            owner_id=shift["owner_id"],
            store_id=shift["store_id"],
            store_session_id=shift["store_session_id"],
            method="cash",
            kind=kind,
            amount=-payable,
            work_session_id=shift["id"],
            worker_id=shift["worker_id"],
            note=note,
        )
    except asyncpg.exceptions.UniqueViolationError:
        # one_salary_per_work_session fired: a concurrent close already paid this
        # shift. Nothing to do, and nothing wrong.
        log.warning("shift %s was already paid; skipping duplicate %s", shift["id"], kind)
        return ZERO

    if payable < amount:
        log.info(
            "the till at store %s paid %s of a %s of %s for shift %s",
            shift["store_id"], payable, kind, amount, shift["id"],
        )
    return amount - payable


async def pay_bonus_if_earned(conn, shift) -> Decimal:
    """Pay the worker's bonus if they have beaten their target this period.

    Evaluated as the shift closes, because that is when the period's sales are
    finally known and when money already leaves the till — so a bonus is one
    more row in the same ledger rather than a second scheme running beside it.

    Once per period, not per shift: somebody who crosses the target in the
    morning and works again in the evening has earned it once. The evening shift
    finds it already paid and adds nothing.

    Returns whatever the drawer could not cover, which the caller records as owed.
    """
    worker = await conn.fetchrow(
        """
        SELECT w.bonus_threshold, w.bonus_amount, w.bonus_period,
               s.day_start_hour
          FROM workers w
          JOIN stores s ON s.id = $2
         WHERE w.id = $1
        """,
        shift["worker_id"], shift["store_id"],
    )
    if worker is None or worker["bonus_threshold"] is None:
        return ZERO

    since = _period_start(worker["bonus_period"], worker["day_start_hour"])
    if await tracking_repo.bonus_paid_since(conn, shift["worker_id"], since):
        return ZERO

    sold = Decimal(await tracking_repo.sold_by_worker_since(conn, shift["worker_id"], since))
    if sold < Decimal(worker["bonus_threshold"]):
        return ZERO

    bonus = Decimal(worker["bonus_amount"])
    unpaid = await _pay_from_the_till(
        conn, shift, "bonus", bonus,
        # In Armenian, like every other note. This one is read on the owner's ledger
        # page beside «Հանձված» and «Աշխատավարձ», where `day: 101,500 ≥ 100,000` was
        # the only row in English and read as a debug line.
        note=BONUS_NOTE.format(
            period=BONUS_PERIODS.get(worker["bonus_period"], worker["bonus_period"]),
            sold=f"{sold:,.0f}",
            threshold=f"{worker['bonus_threshold']:,.0f}",
        ),
    )
    # Earned is earned. The column says what the target was worth, and whether the
    # drawer had it on the night is a separate fact, kept separately.
    await conn.execute(
        "UPDATE work_sessions SET bonus_paid = $2 WHERE id = $1", shift["id"], bonus
    )
    log.info(
        "worker %s earned a %s bonus of %s (sold %s)",
        shift["worker_id"], worker["bonus_period"], bonus, sold,
    )
    return unpaid


def _period_start(period: str, day_start_hour: int):
    """When the stretch a bonus is measured over began.

    A day means the store's own trading day, the same boundary the takings use —
    a shop open past midnight would otherwise have its evening split across two
    bonus days. A month is the calendar month, starting at that same hour so the
    two never disagree about which day a late night belonged to.
    """
    start_of_day = settings.trading_day_start(day_start_hour)
    if period == "month":
        return start_of_day.replace(day=1)
    return start_of_day


async def _close_store_session(conn, store_session_id: int, closed_by: str) -> dict:
    """Settle the till and stamp the snapshot onto the session row.

    After this returns there is no open session for the store, so every "current"
    total reads zero — which is what "the cash is handed over at close" means
    here. Nothing runs at midnight; the reset is a consequence of the schema.
    """
    open_shifts = await sessions_repo.open_shifts_in_session(conn, store_session_id)
    for shift in open_shifts:
        # A monthly wage is not settled out of the till, so closing costs nothing
        # for those workers.
        due = (
            Decimal(shift["salary_amount"]) if shift["salary_period"] == "shift" else ZERO
        )
        await _pay_and_close_shift(
            conn,
            shift,
            due,
            lat=None,
            lng=None,
            idem_key=None,
            closed_by=closed_by,
        )

    totals = await money_repo.totals_on(conn, store_session_id)
    await sessions_repo.close_store_session(
        conn,
        store_session_id,
        cash=totals["cash"],
        card=totals["card"],
        salaries=totals["salaries"],
        closed_by=closed_by,
    )
    log.info(
        "store session %s closed by %s (cash=%s card=%s salaries=%s, %d shift(s) ended)",
        store_session_id, closed_by, totals["cash"], totals["card"], totals["salaries"],
        len(open_shifts),
    )
    return {
        "cash": totals["cash"],
        "card": totals["card"],
        "salaries": totals["salaries"],
        "shifts_ended": len(open_shifts),
    }


async def end_shift(
    worker: Worker, lat: float | None, lng: float | None, idempotency_key: str
) -> dict:
    """End this worker's own shift. Closes the store if they were the last one."""
    replay = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return await _end_payload(replay, duplicate=True)

    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        salary = await _pay_and_close_shift(
            conn,
            shift,
            worker.salary_due_at_shift_end,
            lat=lat,
            lng=lng,
            idem_key=idempotency_key,
            closed_by="worker",
        )

        # Last one out closes the store. The lock taken above means no other
        # shift can start here in the meantime.
        remaining = await sessions_repo.open_shifts_in_session(
            conn, shift["store_session_id"]
        )
        store_closed = not remaining
        if store_closed:
            await _close_store_session(conn, shift["store_session_id"], "worker")

    log.info(
        "worker %s ended shift %s (salary %s, store %s)",
        worker.id, shift["id"], salary, "closed" if store_closed else "still open",
    )
    row = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    return await _end_payload(row, duplicate=False)


async def close_out_shift(
    worker: Worker,
    lines: list[dict],
    idempotency_key: str,
    lat: float | None = None,
    lng: float | None = None,
    close_store_too: bool = False,
) -> dict:
    """End a shift and record everything sold during it, in one transaction.

    This is how a sale is recorded now: the cashier serves customers all day
    without touching the bot, then writes the day up once. Either the whole
    declaration lands with the shift closed and the salary paid, or none of it
    does — a half-applied close-out would leave stock moved against a shift that
    is still open, which nothing downstream could make sense of.
    """
    replay = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return await _end_payload(replay, duplicate=True)

    # Import here: sales imports Worker from this module, so a module-level
    # import would be circular.
    from app.services import sales as sales_service

    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        for line in lines:
            line["idempotency_key"] = idempotency_key
        await sales_service.apply_closeout_lines(conn, worker, shift, lines)

        salary = await _pay_and_close_shift(
            conn,
            shift,
            worker.salary_due_at_shift_end,
            lat=lat,
            lng=lng,
            idem_key=idempotency_key,
            closed_by="worker",
        )

        remaining = await sessions_repo.open_shifts_in_session(
            conn, shift["store_session_id"]
        )
        if close_store_too and remaining:
            await _refuse_to_strand(conn, worker, remaining)
        store_closed = close_store_too or not remaining
        if store_closed:
            await _close_store_session(conn, shift["store_session_id"], "worker")

    log.info(
        "worker %s closed out with %d line(s), salary %s, store %s",
        worker.id, len(lines), salary, "closed" if store_closed else "still open",
    )
    row = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    return await _end_payload(row, duplicate=False)


async def _refuse_to_strand(conn, worker: Worker, remaining: list) -> None:
    """Stop one cashier from ending everybody else's shift.

    Closing the store force-ends every shift still open in it. For the person
    pressing the button that is what they asked for; for a colleague still
    serving customers it means their shift ends without them ever being asked
    what they sold, and that day's takings are simply never recorded. The
    close-out *is* the sales record, so skipping it does not lose a formality —
    it loses the money.

    Whoever is last out closes the shop, which is the normal path and needs no
    button. This only refuses closing it out from under somebody, and names them
    so the worker knows who to go and ask.
    """
    others = [row for row in remaining if row["worker_id"] != worker.id]
    if not others:
        return

    names = await conn.fetch(
        f"SELECT {workers_repo.DISPLAY_NAME} AS name FROM workers w WHERE w.id = ANY($1::bigint[])",
        [row["worker_id"] for row in others],
    )
    listed = "՝ " + ", ".join(row["name"] for row in names) if names else ""
    raise BotError(
        "others_on_shift",
        f"Խանութը փակել չի կարելի՝ ևս {len(others)} աշխատող հերթափոխի մեջ է{listed}։ "
        f"Ավարտեք ձեր հերթափոխը, իսկ խանութը կփակի վերջինը դուրս եկողը։",
        details={"remaining": len(others)},
    )


async def close_store(worker: Worker, idempotency_key: str) -> dict:
    """Close the whole store for everyone, from the bot."""
    replay = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return await _end_payload(replay, duplicate=True)

    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        await _refuse_to_strand(
            conn,
            worker,
            await sessions_repo.open_shifts_in_session(conn, shift["store_session_id"]),
        )

        await _pay_and_close_shift(
            conn,
            shift,
            worker.salary_due_at_shift_end,
            lat=None,
            lng=None,
            idem_key=idempotency_key,
            closed_by="worker",
        )
        await _close_store_session(conn, shift["store_session_id"], "worker")

    row = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    return await _end_payload(row, duplicate=False)


async def _end_payload(row, *, duplicate: bool) -> dict:
    sales = await sales_repo.summary_for_work_session(row["id"])
    totals = await money_repo.totals_for_session(row["store_session_id"])
    started, ended = row["started_at"], row["ended_at"]
    return {
        "ok": True,
        "duplicate": duplicate,
        "summary": {
            "session_id": row["id"],
            "store_id": row["store_id"],
            "store_name": row["store_name"],
            "started_at": started.isoformat(),
            "ended_at": ended.isoformat() if ended else None,
            "duration_minutes": int((ended - started).total_seconds() // 60) if ended else None,
            # The counter alone: what this worker actually sold. Deliveries were in
            # here too, so a shift that took four phone orders and served nobody
            # reported the worker as having sold all of it. They have their own
            # figures below, because the shop did take that money.
            "sales": {
                "receipts": sales["receipts"],
                "cash_total": f"{sales['cash_total']:.2f}",
                "card_total": f"{sales['card_total']:.2f}",
                "total": f"{sales['total']:.2f}",
            },
            "deliveries": {
                "receipts": sales["delivery_receipts"],
                "cash_total": f"{sales['delivery_cash']:.2f}",
                "card_total": f"{sales['delivery_card']:.2f}",
                "total": f"{sales['delivery_total']:.2f}",
            },
            "salary_deducted": f"{Decimal(row['salary_paid'] or 0):.2f}",
            # What the drawer could not cover, and so the owner still owes. Sent
            # separately from the wage rather than netted off it: the worker needs
            # to know both what they earned and what they are still waiting for,
            # and a single smaller number would answer neither question.
            "salary_unpaid": f"{Decimal(row['salary_unpaid'] or 0):.2f}",
            "bonus_paid": f"{Decimal(row['bonus_paid'] or 0):.2f}",
            "bonus_unpaid": f"{Decimal(row['bonus_unpaid'] or 0):.2f}",
            # So the bot can say *why* the wage is half what the worker expects.
            # A number that is quietly wrong is worse than a number with a reason
            # attached, and the reason is not something the bot should re-derive.
            "salary_halved": bool(
                Decimal(row["salary_paid"] or 0) > ZERO
                and ended is not None
                and (ended - started).total_seconds() / 3600 < FULL_SHIFT_HOURS
            ),
            "full_shift_hours": FULL_SHIFT_HOURS,
            "store_closed": row["store_closed"],
            "store_totals_after": {
                "cash": f"{totals['cash']:.2f}",
                "card": f"{totals['card']:.2f}",
            },
        },
    }


# -- owner and housekeeping --------------------------------------------------

async def close_store_session_as_owner(owner_id: int, store_session_id: int) -> dict:
    """The force-close button on the store page."""
    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM store_sessions
             WHERE id = $1 AND owner_id = $2 AND closed_at IS NULL
               FOR UPDATE
            """,
            store_session_id,
            owner_id,
        )
        if row is None:
            raise BotError("store_not_open", "Հերթափոխն արդեն փակ է։")
        return await _close_store_session(conn, store_session_id, "owner")


async def auto_close_stale() -> int:
    """Close store sessions somebody forgot about, paying every open shift.

    Without this a worker who never pressed "close" could not start tomorrow —
    ``one_open_session_per_worker`` would refuse them.
    """
    stale = await sessions_repo.stale_open_sessions(settings.auto_close_hours)
    closed = 0
    for session in stale:
        try:
            async with db.transaction() as conn:
                still_open = await conn.fetchrow(
                    "SELECT id FROM store_sessions WHERE id = $1 AND closed_at IS NULL FOR UPDATE",
                    session["id"],
                )
                if still_open is None:
                    continue  # somebody closed it while we were working
                await _close_store_session(conn, session["id"], "auto")
            closed += 1
            log.warning(
                "auto-closed store session %s (store %s), open since %s",
                session["id"], session["store_id"], session["opened_at"],
            )
        except Exception:  # noqa: BLE001 - one bad session must not stop the sweep
            log.exception("failed to auto-close store session %s", session["id"])
    return closed
