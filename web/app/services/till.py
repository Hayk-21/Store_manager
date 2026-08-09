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

    handed to the owner  =  what was in the till  −  what was left behind
                         =  (yesterday's float + today's takings − wages − petty
                            cash)  −  the new float

The subtraction is booked, not just displayed. The cash genuinely left the shop, so
the ledger says so, and the session's closing figure then matches what is actually
in the drawer. Reading it back out of arithmetic would have left every report
describing money that was no longer there.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import AppError, BotError
from app.repo import money as money_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import till as till_repo

log = logging.getLogger("storemanager.till")

ZERO = Decimal("0.00")

# A drawer holding more than this is a mistyped figure, not a float. Generous enough
# for a shop that banks weekly.
MAX_COUNT = Decimal("10000000.00")

# What these movements are called in the ledger. Constants so the note the owner
# reads on the report and the note the tests assert on cannot drift apart.
NOTE_CARRIED_OVER = "Նախորդ հերթափոխից մնացած կանխիկ"
NOTE_HANDED_OVER = "Հանձնված ղեկավարին"
NOTE_FOUND_EXTRA = "Դրամարկղում սպասվածից ավելի կանխիկ"
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

    Three things happen together, and they have to be one transaction: the count is
    recorded, the shop's balance becomes what was left, and the rest is booked out
    of the till as handed to the owner. Any two of those without the third leaves the
    drawer, the ledger and the balance telling different stories.

    Allowed after the shift has already ended, which is when it is asked for. The
    shop comes from the worker's most recent shift there.
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

            # What the books said at this moment, frozen beside the count. A sale
            # amended next week must not rewrite what was handed over tonight.
            totals = await money_repo.totals_on(conn, shift["store_session_id"])
            in_the_till = Decimal(totals["cash"])
            handed_over = in_the_till - counted

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
            await _book_the_handover(conn, worker, shift, handed_over)
            await stores_repo.set_till_balance(
                conn, worker.owner_id, shift["store_id"], counted
            )
    except asyncpg.exceptions.UniqueViolationError:
        original = await till_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s left %s in store %s and handed over %s",
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


async def _book_the_handover(conn, worker, shift, handed_over: Decimal) -> None:
    """Take the owner's share out of the till, so the ledger matches the drawer.

    Positive is the normal case: the worker keeps the float and hands the rest over.
    Negative means the drawer holds more than the books expected — an unrecorded sale,
    or somebody put money in — and that is booked as found cash rather than as a
    negative handover, because nothing was handed anywhere.
    """
    if handed_over == ZERO:
        return

    going_out = handed_over > ZERO
    await money_repo.insert_movement(
        conn,
        owner_id=worker.owner_id,
        store_id=shift["store_id"],
        store_session_id=shift["store_session_id"],
        method="cash",
        kind="withdrawal" if going_out else "adjustment",
        amount=-handed_over,
        work_session_id=shift["id"],
        worker_id=worker.id,
        note=NOTE_HANDED_OVER if going_out else NOTE_FOUND_EXTRA,
        created_by="worker",
    )


async def set_by_owner(
    owner_id: int, store_id: int, amount: Decimal, note: str | None = None
) -> Decimal:
    """The owner correcting a shop's float from the website.

    Needed because the balance is a real quantity somebody can be wrong about: a
    count typed with an extra nought, a drawer topped up in person, a shop set up
    before anybody counted anything. Recorded as their correction, so the history
    says who moved it and not just that it moved.

    While a session is open the balance *is* the till, so the correction is booked
    there too — otherwise the figure on the page and the figure the shop is working
    from would disagree until closing time.
    """
    if amount < ZERO or amount > MAX_COUNT:
        raise AppError("validation_error", "Սխալ գումար։")
    if await stores_repo.get(owner_id, store_id) is None:
        raise AppError("not_found", "Խանութը չի գտնվել։")

    async with db.transaction() as conn:
        session = await sessions_repo.lock_open_for_store(conn, owner_id, store_id)
        expected = ZERO
        if session is not None:
            totals = await money_repo.totals_on(conn, session["id"])
            expected = Decimal(totals["cash"])
            difference = amount - expected
            if difference != ZERO:
                await money_repo.insert_movement(
                    conn,
                    owner_id=owner_id,
                    store_id=store_id,
                    store_session_id=session["id"],
                    method="cash",
                    kind="adjustment",
                    amount=difference,
                    note=note or NOTE_OWNER_SET,
                    created_by="owner",
                )
        else:
            expected = Decimal(
                await stores_repo.till_balance(conn, owner_id, store_id) or 0
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
            handed_over=None,
            note=note or NOTE_OWNER_SET,
            external_id=None,
        )
        await stores_repo.set_till_balance(conn, owner_id, store_id, amount)

    log.info("owner %s set the float of store %s to %s", owner_id, store_id, amount)
    return amount


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
