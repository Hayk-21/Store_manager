"""Races, run against a real connection pool.

Every other test file binds one connection and rolls it back, which is fast but
serialises everything by construction. These tests deliberately do not: they
open the real pool and fire genuinely simultaneous requests, because the
properties being checked here — "exactly one sale", "exactly one shift" — are
enforced by Postgres indexes and locks that a single-connection test can never
exercise.

They commit, so each one truncates on the way out.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from tests.factories import YEREVAN_LAT, YEREVAN_LNG

TABLES = (
    "cash_movements, sale_items, sales, work_sessions, store_sessions, "
    "items, workers, stores, auth_sessions, login_codes, login_links, users"
)


@pytest.fixture
async def pooled(migrated):
    """The real pool, with the tables emptied before and after."""
    db.bind(None)
    await db.connect()
    await db.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    try:
        yield db
    finally:
        await db.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
        await db.close()


async def _world(salary: str = "0.00", stock: int = 100):
    owner_id = await db.fetchval(
        "INSERT INTO users (telegram_username) VALUES ('raceowner') RETURNING id"
    )
    store_id = await db.fetchval(
        """
        INSERT INTO stores (owner_id, name, lat, lng, radius_m)
        VALUES ($1, 'Խանութ', $2, $3, 120) RETURNING id
        """,
        owner_id, YEREVAN_LAT, YEREVAN_LNG,
    )
    worker_id = await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_id, salary_amount)
        VALUES ($1, 'Անի', 555000111, $2) RETURNING id
        """,
        owner_id, Decimal(salary),
    )
    item_id = await db.fetchval(
        """
        INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price)
        VALUES ($1, $2, 'HQD', $3, 1500.00, 3500.00) RETURNING id
        """,
        owner_id, store_id, stock,
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    return owner_id, store_id, worker, item_id


async def test_ten_simultaneous_retries_of_one_tap_sell_exactly_once(pooled):
    """The property the whole idempotency design exists for.

    A cashier on a flaky connection taps once; the bot retries. If two of those
    retries land at the same instant, the pre-check in record_sale sees nothing
    in both — only the unique index can arbitrate.
    """
    _, _, worker, item_id = await _world()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)

    results = await asyncio.gather(
        *(
            sales_service.record_sale(
                worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-key-same-0001"
            )
            for _ in range(10)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"a retry raised instead of returning the original: {failures}"
    assert await db.fetchval("SELECT count(*) FROM sales") == 1
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 98
    assert await db.fetchval("SELECT sum(amount) FROM cash_movements") == Decimal("7000.00")
    # All ten callers were told the same thing.
    assert len({r["sale"]["id"] for r in results}) == 1
    assert sum(1 for r in results if not r["duplicate"]) == 1


async def test_distinct_simultaneous_sales_all_apply(pooled):
    """The opposite property: real concurrency must not be serialised away."""
    _, _, worker, item_id = await _world()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)

    results = await asyncio.gather(
        *(
            sales_service.record_sale(
                worker, [{"item_id": item_id, "quantity": 1}], "cash", f"idem-key-{n:08d}"
            )
            for n in range(8)
        )
    )

    assert len({r["sale"]["id"] for r in results}) == 8
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 92
    assert await db.fetchval("SELECT sum(amount) FROM cash_movements") == Decimal("28000.00")


async def test_stock_cannot_be_oversold_by_simultaneous_sales(pooled):
    """Five units, five people asking for two each: at most two can win."""
    _, _, worker, item_id = await _world(stock=5)
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)

    results = await asyncio.gather(
        *(
            sales_service.record_sale(
                worker, [{"item_id": item_id, "quantity": 2}], "cash", f"idem-key-{n:08d}"
            )
            for n in range(5)
        ),
        return_exceptions=True,
    )

    sold = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, BotError)]
    assert len(sold) == 2 and len(refused) == 3
    assert all(r.code == "insufficient_stock" for r in refused)
    remaining = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)
    assert remaining == 1, "the count must never go negative or lose an update"


async def test_two_simultaneous_opens_produce_one_shift(pooled):
    """A double tap on "open" must not create two shifts."""
    _, _, worker, _ = await _world()

    results = await asyncio.gather(
        *(
            shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-same", 900)
            for _ in range(6)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, Exception)]
    assert not failures, f"a retry raised instead of returning the original: {failures}"
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 1
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1


async def test_two_workers_opening_at_once_share_one_store_session(pooled):
    """ON CONFLICT DO NOTHING against one_open_session_per_store: exactly one
    insert wins the till, the other joins it."""
    owner_id, store_id, first, _ = await _world()
    second_id = await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_id, salary_amount)
        VALUES ($1, 'Բ', 555000222, 0) RETURNING id
        """,
        owner_id,
    )
    second = shifts_service.Worker(
        id=second_id, owner_id=owner_id, name="Բ", salary_amount=Decimal("0.00")
    )

    a, b = await asyncio.gather(
        shifts_service.open_store(first, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-aaaa-01", 900),
        shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-bbbb-02", 900),
    )

    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1
    assert a["session"]["store_session_id"] == b["session"]["store_session_id"]
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 2


async def test_a_sale_cannot_land_after_the_shift_closed(pooled):
    """The FOR UPDATE on the work session is what orders these two."""
    _, _, worker, item_id = await _world(salary="1000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)

    outcomes = await asyncio.gather(
        sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-key-sale-01"
        ),
        shifts_service.end_shift(worker, None, None, "idem-key-end-01"),
        return_exceptions=True,
    )

    sale_outcome, end_outcome = outcomes
    # Either the sale got in before the close, or it was refused outright. What
    # must never happen is a sale attached to a shift that is already finished.
    if not isinstance(sale_outcome, Exception):
        orphan = await db.fetchval(
            """
            SELECT count(*) FROM sales sa
              JOIN work_sessions ws ON ws.id = sa.work_session_id
             WHERE ws.ended_at IS NOT NULL AND sa.sold_at > ws.ended_at
            """
        )
        assert orphan == 0
    else:
        assert isinstance(sale_outcome, BotError)
        assert sale_outcome.code == "no_open_session"
    assert not isinstance(end_outcome, Exception)
