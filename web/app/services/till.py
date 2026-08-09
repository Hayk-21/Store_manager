"""Counting the drawer, and carrying what is in it to the next shift.

Two facts that are easy to confuse. What the books say is in the till is a sum over
``cash_movements`` for the open session, and it is right only if every sale was
entered. What is *in the drawer* is what somebody counted. Keeping them apart is
the whole point: the gap between them is the thing an owner wants to know about,
and it only exists as long as neither figure is allowed to overwrite the other.

The money itself does not move at close. A shop that keeps 40,000 in the drawer
overnight still has it in the morning, and the till of the new session has to start
there — otherwise the first sale of the day makes the drawer look 40,000 heavy. So
opening a session carries the last count over as an ordinary deposit, and the
arriving worker counts again to confirm it. A drawer that is short is then noticed
at the start of a shift by somebody who did not cause it, rather than at the end by
whoever gets blamed.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import sessions as sessions_repo
from app.repo import till as till_repo

log = logging.getLogger("storemanager.till")

ZERO = Decimal("0.00")

# A drawer holding more than this is a mistyped figure, not a float. Generous
# enough for a shop that banks weekly.
MAX_COUNT = Decimal("10000000.00")

KINDS = {"open", "close"}

# What the carried-over deposit is called in the ledger. A constant so the note the
# owner reads on the report and the note the tests assert on cannot drift apart.
NOTE_CARRIED_OVER = "Նախորդ հերթափոխից մնացած կանխիկ"


async def carry_over_float(conn, owner_id: int, store_id: int, store_session_id: int):
    """Seed a new session's till with the cash the last shift left behind.

    Called as a session is created, inside that transaction. An ordinary deposit
    rather than a column of its own, so "how much is in the till" stays a sum over
    one table and every existing figure keeps working.

    Silent when nobody has ever counted here — that is "unknown", not zero, and
    inventing an opening balance would be worse than starting empty.
    """
    left = await till_repo.last_close_for_store(conn, owner_id, store_id)
    if left is None or Decimal(left) <= ZERO:
        return ZERO

    amount = Decimal(left)
    await money_repo.insert_movement(
        conn,
        owner_id=owner_id,
        store_id=store_id,
        store_session_id=store_session_id,
        method="cash",
        kind="deposit",
        amount=amount,
        note=NOTE_CARRIED_OVER,
        created_by="system",
    )
    log.info("store %s opened with %s carried over", store_id, amount)
    return amount


async def declare(
    worker, kind: str, counted: Decimal, idem_key: str, note: str | None = None
) -> dict:
    """Record a hand count of the drawer.

    ``close`` is allowed after the worker's own shift has ended — that is when it is
    asked for — so this deliberately does not require an open shift. It does require
    the shop, which comes from their most recent shift there.
    """
    if kind not in KINDS:
        raise BotError("validation_error", "Անհայտ գործողություն։")
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

            # What the books said at this moment, frozen beside the count. A sale
            # amended next week must not rewrite whether the till balanced tonight.
            totals = await money_repo.totals_on(conn, shift["store_session_id"])
            expected = Decimal(totals["cash"])

            count_id = await till_repo.insert(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                store_session_id=shift["store_session_id"],
                work_session_id=shift["id"],
                worker_id=worker.id,
                kind=kind,
                counted=counted,
                expected=expected,
                note=note,
                external_id=idem_key,
            )
    except asyncpg.exceptions.UniqueViolationError:
        original = await till_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s counted %s in the drawer of store %s (books said %s)",
        worker.id, counted, shift["store_id"], expected,
    )
    return {
        "ok": True,
        "duplicate": False,
        "count": {
            "id": count_id,
            "kind": kind,
            "counted": f"{counted:.2f}",
            "expected": f"{expected:.2f}",
            "difference": f"{counted - expected:.2f}",
        },
    }


def _payload(row, *, duplicate: bool) -> dict:
    counted, expected = Decimal(row["counted"]), Decimal(row["expected"])
    return {
        "ok": True,
        "duplicate": duplicate,
        "count": {
            "id": row["id"],
            "kind": row["kind"],
            "counted": f"{counted:.2f}",
            "expected": f"{expected:.2f}",
            "difference": f"{counted - expected:.2f}",
        },
    }
