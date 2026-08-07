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
from decimal import Decimal

import asyncpg

from app.config import settings
from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.services.geofence import require_store

log = logging.getLogger("storemanager.shifts")

ZERO = Decimal("0.00")


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

async def open_store(
    worker: Worker,
    lat: float,
    lng: float,
    accuracy_m: float | None,
    idempotency_key: str,
) -> dict:
    """Requirement 8: located, matched to a store, attached to it."""
    replay = await sessions_repo.by_start_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return _open_payload(worker, replay, duplicate=True)

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

    Returns what was actually paid.
    """
    await sessions_repo.close_work_session(
        conn, shift["id"], salary, lat, lng, idem_key, closed_by
    )
    if salary <= ZERO:
        return ZERO
    try:
        await money_repo.insert_movement(
            conn,
            owner_id=shift["owner_id"],
            store_id=shift["store_id"],
            store_session_id=shift["store_session_id"],
            method="cash",
            kind="salary",
            amount=-salary,
            work_session_id=shift["id"],
            worker_id=shift["worker_id"],
        )
    except asyncpg.exceptions.UniqueViolationError:
        # one_salary_per_work_session fired: a concurrent close already paid this
        # shift. Nothing to do, and nothing wrong.
        log.warning("shift %s was already paid; skipping duplicate salary", shift["id"])
        return ZERO
    return salary


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
    return await _end_payload(row, duplicate=False, store_closed=store_closed)


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
        store_closed = close_store_too or not remaining
        if store_closed:
            await _close_store_session(conn, shift["store_session_id"], "worker")

    log.info(
        "worker %s closed out with %d line(s), salary %s, store %s",
        worker.id, len(lines), salary, "closed" if store_closed else "still open",
    )
    row = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    return await _end_payload(row, duplicate=False, store_closed=store_closed)


async def close_store(worker: Worker, idempotency_key: str) -> dict:
    """Close the whole store for everyone, from the bot."""
    replay = await sessions_repo.by_end_idem(worker.owner_id, idempotency_key)
    if replay is not None:
        return await _end_payload(replay, duplicate=True, store_closed=True)

    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

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
    return await _end_payload(row, duplicate=False, store_closed=True)


async def _end_payload(row, *, duplicate: bool, store_closed: bool = True) -> dict:
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
            "sales": {
                "receipts": sales["receipts"],
                "cash_total": f"{sales['cash_total']:.2f}",
                "card_total": f"{sales['card_total']:.2f}",
                "total": f"{sales['total']:.2f}",
            },
            "salary_deducted": f"{Decimal(row['salary_paid'] or 0):.2f}",
            "store_closed": store_closed,
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
