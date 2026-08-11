"""Two cashiers reaching into the same drawer at the same instant.

Every other test in this suite binds one connection into a transaction and rolls
it back — fast, and correct for testing what one request does. It cannot test what
two *concurrent* requests do to each other, because both "requests" would really be
two awaits taking turns on the one connection, never actually overlapping.

A race between two workers is a race between two physical connections, so this file
opens a real pool against the test database instead — the same shape the app runs
in in production — fires two withdrawals at once from two different cashiers on one
store session, and checks the drawer never went negative. It cleans up every row it
creates; nothing here is rolled back for it.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest import mock

import pytest

from app.db import Database, db
from app.errors import BotError
from app.repo import money as money_repo
from app.services import money as money_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from tests.factories import YEREVAN_LAT, YEREVAN_LNG, make_item, make_owner, make_store, make_worker

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@asynccontextmanager
async def _widened_race_window():
    """Force the two withdrawals to genuinely overlap instead of leaving it to
    scheduling luck.

    Two local Postgres round trips are fast enough that ``asyncio.gather`` can
    happen to run one coroutine to completion before the other's first query even
    reaches the server — a real race exists whether or not that happens, but a
    test that only sometimes exercises it is a test that only sometimes means
    anything. Pausing every ``totals_on`` read for a beat guarantees both
    withdrawals are inside the vulnerable window — read, not yet written — at the
    same time, every run. It does not favor the fix: a caller correctly blocked
    on the store session's lock stays blocked for the *entire* wait, sleep
    included, and only reads once the first withdrawal has actually committed.
    """
    real = money_repo.totals_on

    async def slow(conn, store_session_id):
        result = await real(conn, store_session_id)
        await asyncio.sleep(0.1)
        return result

    with mock.patch.object(money_repo, "totals_on", slow):
        yield


@pytest.fixture
async def pooled(migrated):
    """A real connection pool against the test database, for the one test in this
    suite that needs actual concurrent connections rather than one shared
    transaction. Torn down, and the pool closed, whether or not the test passes.
    """
    real = Database()
    await real.connect()
    db._pool = real._pool  # noqa: SLF001 - the one legitimate reason to reach in
    db.bind(None)
    try:
        yield db
    finally:
        await real.close()
        db._pool = None  # noqa: SLF001


async def _a_shared_drawer(pooled, cash: str):
    """A shop with two cashiers on the same open session, and ``cash`` already in
    the till. Committed for real — there is no transaction here to roll back."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=1000, self_price="0.00", sell_price=cash
    )
    a_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    b_id, _ = await make_worker(owner_id, "Գոռ", salary_amount="0.00")
    worker_a = shifts_service.Worker(
        id=a_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    worker_b = shifts_service.Worker(
        id=b_id, owner_id=owner_id, name="Գոռ", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker_a, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-a", 900)
    await shifts_service.open_store(worker_b, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-b", 900)
    if Decimal(cash) > 0:
        await sales_service.record_sale(
            worker_a, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
        )
    return owner_id, worker_a, worker_b


async def _forget(owner_id: int) -> None:
    """Undo every row this test committed. Cascades from ``users`` take the rest."""
    await db.execute("DELETE FROM users WHERE id = $1", owner_id)


async def test_two_cashiers_cannot_jointly_overdraw_a_shared_till(pooled):
    """1,500 in the drawer; two cashiers each try to take 900 at once — each well
    under the per-shift cap on its own, but the two together are more than the
    till holds. At most one can succeed: taking both would leave the till at
    -300, which is not a shortfall anybody caused, it is a number the software
    allowed to exist.
    """
    owner_id, worker_a, worker_b = await _a_shared_drawer(pooled, cash="1500.00")
    try:
        async with _widened_race_window():
            results = await asyncio.gather(
                money_service.withdraw_by_worker(worker_a, Decimal("900"), "a", "idem-cash-a"),
                money_service.withdraw_by_worker(worker_b, Decimal("900"), "b", "idem-cash-b"),
                return_exceptions=True,
            )

        refused = [r for r in results if isinstance(r, BotError)]
        succeeded = [r for r in results if not isinstance(r, Exception)]
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, BotError):
                raise r

        assert len(succeeded) == 1, (
            f"{len(succeeded)} withdrawals of 900 each succeeded against a "
            f"1,500 till — {results}"
        )
        assert len(refused) == 1

        remaining = await db.fetchval(
            "SELECT sum(amount) FROM cash_movements WHERE owner_id = $1", owner_id
        )
        assert remaining >= 0, f"the till went negative: {remaining}"
    finally:
        await _forget(owner_id)


async def test_two_cashiers_who_can_both_be_paid_both_are(pooled):
    """The lock is there to stop an overdraw, not to serialise every withdrawal
    into a queue that refuses the second one on principle — plenty of room for
    both should mean both succeed."""
    owner_id, worker_a, worker_b = await _a_shared_drawer(pooled, cash="20000.00")
    try:
        results = await asyncio.gather(
            money_service.withdraw_by_worker(worker_a, Decimal("500"), "a", "idem-cash-a2"),
            money_service.withdraw_by_worker(worker_b, Decimal("500"), "b", "idem-cash-b2"),
        )

        assert all(r["ok"] for r in results)
        remaining = await db.fetchval(
            "SELECT sum(amount) FROM cash_movements WHERE owner_id = $1", owner_id
        )
        assert remaining == Decimal("19000.00")
    finally:
        await _forget(owner_id)
