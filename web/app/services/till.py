"""The money that stays in the shop, and the money that goes to the owner.

Each store keeps a float in its drawer — its casa. It might be nothing, it might be
forty thousand, and it differs from shop to shop. It is not takings and it is not
the owner's yet: it is the change the next person needs to open up with. It lives on
``stores.till_balance``, which is the one place that answers "how much is in this
shop's drawer".

One person sets it, once a day. At the end of their shift the worker counts what
they are leaving and says so; that becomes the store's balance, and everything else
in the drawer goes to the owner. Nobody is asked at the *start* of a shift — that
asked a worker to answer for a drawer somebody else had filled, and the answer told
you nothing you could hold anyone to.

    the owner's share  =  what was in the till  −  what was left behind
                       =  (yesterday's float + today's takings − wages − petty
                          cash)  −  the new float

**Shown, not booked.** The subtraction is a figure on the report and nothing else: no
`withdrawal` row, no adjustment when the count disagrees with the books. The owner
asked for it that way — the ledger is the shop's record of what it took and spent, and
handing the day's money over is not another expense in it.

So the closing cash on a session is the day's cash as the shop earned it, and the
owner's share is worked out from it against what the worker left. The one figure that
does persist is ``stores.till_balance``: what stays on the premises, and what the next
session opens with.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import AppError, BotError
from app.repo import audit as audit_repo
from app.repo import money as money_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import till as till_repo

log = logging.getLogger("storemanager.till")

ZERO = Decimal("0.00")

# A drawer holding more than this is a mistyped figure, not a float. Generous enough
# for a shop that banks weekly.
MAX_COUNT = Decimal("10000000.00")

# The one movement this module still writes, plus the note an owner's correction
# carries. Constants so what the owner reads and what the tests assert cannot drift.
NOTE_CARRIED_OVER = "Նախորդ հերթափոխից մնացած կանխիկ"
NOTE_OWNER_SET = "Դրամարկղի մնացորդը ուղղվեց ղեկավարի կողմից"


async def carry_over_float(conn, owner_id: int, store_id: int, store_session_id: int):
    """Seed a new session's till with the shop's own float.

    Called as a session is created, inside that transaction. An ordinary deposit
    rather than a column of its own, so "how much is in the till" stays a sum over
    one table and every existing figure keeps working.

    A shop whose balance is nothing contributes nothing, which is the honest answer
    for a drawer that is empty.
    """
    balance = Decimal(await stores_repo.till_balance(conn, owner_id, store_id) or 0)
    if balance <= ZERO:
        return ZERO

    await money_repo.insert_movement(
        conn,
        owner_id=owner_id,
        store_id=store_id,
        store_session_id=store_session_id,
        method="cash",
        kind="deposit",
        amount=balance,
        note=NOTE_CARRIED_OVER,
        created_by="system",
    )
    log.info("store %s opened with a float of %s", store_id, balance)
    return balance


async def declare_close(
    worker, counted: Decimal, idem_key: str, note: str | None = None
) -> dict:
    """The worker's count at the end of their shift.

    Two things happen, in one transaction: the count is recorded, and the shop's
    balance becomes what was left. The owner's share is worked out and stored beside
    the count, but nothing is taken out of the till — that figure is reported, not
    booked.

    Only once the shop is shut, and that restriction is the whole ordering. What the
    owner is owed is the till less the float, so anything still to come out of the till
    has to come out first. A worker who counted up at 21:07 mid-shift was quoted a
    handover of 82,000 and then paid 6,000 at 21:10, leaving the owner's figure 6,000
    too high. A mid-shift count also goes stale on the very next sale.

    It is the *session* that has to be shut, not just this worker's shift. The drawer is
    shared — one of two cashiers going home cannot settle the change their colleague
    needs for the next four hours.
    """
    if counted < ZERO or counted > MAX_COUNT:
        raise BotError("validation_error", "Սխալ գումար։")

    replay = await till_repo.by_external_id(worker.owner_id, idem_key)
    if replay is not None:
        return _payload(replay, duplicate=True)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.latest_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")
            if await sessions_repo.is_open(conn, shift["store_session_id"]):
                raise BotError("store_still_open")

            # What the books said at this moment, frozen beside the count. A sale
            # amended next week must not rewrite what the owner was owed tonight.
            totals = await money_repo.totals_on(conn, shift["store_session_id"])
            in_the_till = Decimal(totals["cash"])
            handed_over = _owners_share(in_the_till, counted)

            count_id = await till_repo.insert(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                work_session_id=shift["id"],
                worker_id=worker.id,
                kind="close",
                counted=counted,
                expected=in_the_till,
                handed_over=handed_over,
                note=note,
                external_id=idem_key,
            )
            await stores_repo.set_till_balance(
                conn, worker.owner_id, shift["store_id"], counted
            )
    except asyncpg.exceptions.UniqueViolationError:
        original = await till_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s left %s in store %s; the owner is owed %s",
        worker.id, counted, shift["store_id"], handed_over,
    )
    return {
        "ok": True,
        "duplicate": False,
        "count": {
            "id": count_id,
            "kind": "close",
            "counted": f"{counted:.2f}",
            "expected": f"{in_the_till:.2f}",
            "handed_over": f"{handed_over:.2f}",
        },
    }


def _owners_share(in_the_till: Decimal, left_behind: Decimal) -> Decimal:
    """What the owner is owed, which is never less than nothing.

    Normally the till less the float. When the worker leaves *more* than the books say
    the drawer held — an unrecorded sale, a miscount, somebody topping it up — the
    answer is that the owner gets nothing from this shop today, not that they owe it
    money. «-4,500 ֏» beside "hand to the owner" is not a figure anybody can act on.

    The gap is not swallowed: the count keeps ``expected`` beside ``counted``, so the
    two readings are both on the report and the difference is there to be seen.
    """
    return max(ZERO, in_the_till - left_behind)


async def set_by_owner(
    owner_id: int, store_id: int, amount: Decimal, note: str | None = None
) -> Decimal:
    """The owner correcting a shop's float, from the store page or a report.

    Needed because the balance is a real quantity somebody can be wrong about: a
    count typed with an extra nought, a drawer topped up in person, a shop set up
    before anybody counted anything. Recorded as their own correction, so the history
    says who moved it and not just that it moved.

    Touches no ledger. The balance is what stays on the premises and the till is what
    the shop took, and since the handover stopped being booked the two are no longer
    the same quantity — a correction to one is not a movement in the other.
    """
    if amount < ZERO or amount > MAX_COUNT:
        raise AppError("validation_error", "Սխալ գումար։")
    if await stores_repo.get(owner_id, store_id) is None:
        raise AppError("not_found", "Խանութը չի գտնվել։")

    async with db.transaction() as conn:
        session = await sessions_repo.lock_open_for_store(conn, owner_id, store_id)
        expected = Decimal(
            (await money_repo.totals_on(conn, session["id"]))["cash"]
            if session is not None
            else await stores_repo.till_balance(conn, owner_id, store_id) or 0
        )
        await till_repo.insert(
            conn,
            owner_id=owner_id,
            store_id=store_id,
            store_session_id=session["id"] if session else None,
            work_session_id=None,
            worker_id=None,
            kind="owner",
            counted=amount,
            expected=expected,
            handed_over=_owners_share(expected, amount) if session is not None else None,
            note=note or NOTE_OWNER_SET,
            external_id=None,
        )
        await stores_repo.set_till_balance(conn, owner_id, store_id, amount)

    log.info("owner %s set the float of store %s to %s", owner_id, store_id, amount)
    return amount


async def correct_a_count(
    owner_id: int, count_id: int, counted: Decimal, expected: Decimal | None = None
) -> asyncpg.Record:
    """Change what a count says: what was left in the shop, and what the till held.

    Both are readings rather than claims about what happened to any money — one taken
    off a drawer at the door, one off the books at the same moment — so a wrong one is
    worth replacing rather than stacking a correction beside, which is what a wage or a
    sale would get.

    ``counted`` is the figure most often wrong: a nought too many, a bundle counted
    twice, a number typed while locking up. ``expected`` needs fixing less often but does
    need it, because it was frozen at the moment of counting and a sale voided afterwards
    leaves it describing books that have since changed.

    The owner's share is the difference, so it moves with either. The shop's float moves
    with ``counted``, but only when this is the count the float came from.
    """
    if counted < ZERO or counted > MAX_COUNT:
        raise AppError("validation_error", "Սխալ գումար։")
    if expected is not None and abs(expected) > MAX_COUNT:
        raise AppError("validation_error", "Սխալ գումար։")

    async with db.transaction() as conn:
        row = await till_repo.lock(conn, owner_id, count_id)
        if row is None:
            raise AppError("not_found", "Հաշվարկը չի գտնվել։")

        # Not floored at zero, unlike the count. The till genuinely could hold less than
        # nothing under the old rules, and a row saying so is a record of that evening;
        # the *share* is what must never be negative, and that is floored below.
        if expected is None:
            expected = Decimal(row["expected"])
        await till_repo.set_reading(
            conn, count_id, counted, expected, _owners_share(expected, counted)
        )
        # Only if this is still the shop's latest word on its drawer. An older count
        # being fixed for the record must not overwrite what a later one established.
        latest = await till_repo.latest_for_store(conn, owner_id, row["store_id"])
        if latest is not None and latest["id"] == count_id:
            await stores_repo.set_till_balance(conn, owner_id, row["store_id"], counted)

    log.info(
        "owner %s corrected count %s to counted=%s expected=%s",
        owner_id, count_id, counted, expected,
    )
    return row


async def delete_count(owner_id: int, user_id: int, count_id: int) -> asyncpg.Record:
    """Remove a count from the record.

    Not a correction of a figure but a statement that the reading should never have
    been there — a count entered against the wrong shop, a duplicate, a row left over
    from a rule that has since changed. Nothing is booked from a count, so there is no
    ledger to unwind.

    The shop's float then falls back to whatever count is now the latest, or to nothing
    if that was the only one. Leaving it where the deleted row put it would keep a
    figure whose only justification has just been thrown away.
    """
    async with db.transaction() as conn:
        row = await till_repo.lock(conn, owner_id, count_id)
        if row is None:
            raise AppError("not_found", "Հաշվարկը չի գտնվել։")

        float_was = Decimal(
            await stores_repo.till_balance(conn, owner_id, row["store_id"]) or 0
        )
        await till_repo.delete(conn, owner_id, count_id)
        previous = await till_repo.latest_for_store(conn, owner_id, row["store_id"])
        await stores_repo.set_till_balance(
            conn,
            owner_id,
            row["store_id"],
            Decimal(previous["counted"]) if previous is not None else ZERO,
        )
        await audit_repo.record(
            conn, owner_id, user_id, "delete_till_count",
            f"Ջնջվեց դրամարկղի հաշվարկ՝ {Decimal(row['counted']):,.0f} ֏",
            store_session_id=row["store_session_id"],
            # The whole row, not a handful of ids. Deleting one leaves nothing to
            # rebuild it from anywhere else, and every other deletion here is undoable.
            payload={"count": _as_payload(row), "float_was": str(float_was)},
        )

    log.info("owner %s deleted till count %s", owner_id, count_id)
    return row


def _as_payload(row) -> dict:
    """A count as JSON, for the history to rebuild it from."""
    return {
        "store_id": row["store_id"],
        "store_session_id": row["store_session_id"],
        "work_session_id": row["work_session_id"],
        "worker_id": row["worker_id"],
        "kind": row["kind"],
        "counted": str(row["counted"]),
        "expected": str(row["expected"]),
        "handed_over": None if row["handed_over"] is None else str(row["handed_over"]),
        "note": row["note"],
        "external_id": row["external_id"],
        "created_at": row["created_at"].isoformat(),
    }


async def restore_count(conn, owner_id: int, payload: dict) -> None:
    """Put a deleted count back, and the shop's float with it.

    Called by the history's undo. The float is restored to what it was rather than to
    this count's figure: a later count may have moved it since, and the undo's job is to
    put things back as they were before the delete, not before the count.
    """
    row = dict(payload["count"])
    row["created_at"] = datetime.fromisoformat(row["created_at"])
    await till_repo.restore(conn, owner_id, row)
    await stores_repo.set_till_balance(
        conn, owner_id, row["store_id"], Decimal(payload["float_was"])
    )


def _payload(row, *, duplicate: bool) -> dict:
    counted, expected = Decimal(row["counted"]), Decimal(row["expected"])
    handed = row["handed_over"]
    return {
        "ok": True,
        "duplicate": duplicate,
        "count": {
            "id": row["id"],
            "kind": row["kind"],
            "counted": f"{counted:.2f}",
            "expected": f"{expected:.2f}",
            "handed_over": f"{Decimal(handed):.2f}" if handed is not None else None,
        },
    }
