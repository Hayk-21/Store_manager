"""A cashier correcting a stock count from the counter.

Stock drifts: a box arrives and nobody enters it, a customer brings one back, two
of something were miscounted at the last close. The cashier is the only person
standing in front of both the shelf and the screen.

So the tests are about two things. That a batch is all-or-nothing — the cashier
counted four products and confirmed once, and half of that landing would be
invisible — and that every change leaves a row with a name on it, because a count
that can be changed silently can be changed to cover a shortfall.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.repo import adjustments as adjustments_repo
from app.services import shifts as shifts_service
from app.services import stock as stock_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
)

BASE = "/api/bot/v1"


async def _on_shift(count: int = 10):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=count,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, store_id, worker, item_id, telegram_id


async def _count(item_id: int) -> int:
    return await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)


# -- applying ----------------------------------------------------------------

async def test_adding_stock_puts_the_count_up(client):
    _, _, worker, item_id, _ = await _on_shift()

    result = await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 6}], "idem-adj-01"
    )

    assert await _count(item_id) == 16
    assert result["adjusted"][0]["count_after"] == 16


async def test_removing_stock_puts_it_down(client):
    _, _, worker, item_id, _ = await _on_shift()

    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": -4}], "idem-adj-01"
    )

    assert await _count(item_id) == 6


async def test_the_correction_says_who_made_it(client):
    """The whole reason it is a log and not a bare UPDATE."""
    _, store_id, worker, item_id, _ = await _on_shift()

    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 3}], "idem-adj-01"
    )

    row = await db.fetchrow("SELECT worker_id, delta, count_after, store_id FROM stock_adjustments")
    assert row["worker_id"] == worker.id
    assert row["delta"] == 3
    assert row["count_after"] == 13
    assert row["store_id"] == store_id


async def test_several_products_in_one_go(client):
    _, store_id, worker, item_id, _ = await _on_shift()
    other = await make_item(worker.owner_id, store_id, "Waka 1800", count=2)

    await stock_service.adjust_by_worker(
        worker,
        [{"item_id": item_id, "delta": -1}, {"item_id": other, "delta": 5}],
        "idem-adj-01",
    )

    assert await _count(item_id) == 9
    assert await _count(other) == 7
    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 2


async def test_a_batch_is_all_or_nothing(client):
    """Half a correction landing is worse than none: the screen would show numbers
    the cashier believes they already fixed."""
    _, store_id, worker, item_id, _ = await _on_shift()
    other = await make_item(worker.owner_id, store_id, "Waka 1800", count=2)

    with pytest.raises(BotError):
        await stock_service.adjust_by_worker(
            worker,
            # The second line is impossible: only two on the shelf.
            [{"item_id": item_id, "delta": 4}, {"item_id": other, "delta": -9}],
            "idem-adj-01",
        )

    assert await _count(item_id) == 10, "the good line was rolled back too"
    assert await _count(other) == 2
    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 0


async def test_taking_more_off_than_there_is_is_refused(client):
    _, _, worker, item_id, _ = await _on_shift(count=3)

    with pytest.raises(BotError) as caught:
        await stock_service.adjust_by_worker(
            worker, [{"item_id": item_id, "delta": -5}], "idem-adj-01"
        )

    assert "3" in caught.value.message
    assert await _count(item_id) == 3


async def test_a_zero_is_not_a_correction(client):
    _, _, worker, item_id, _ = await _on_shift()

    with pytest.raises(BotError):
        await stock_service.adjust_by_worker(
            worker, [{"item_id": item_id, "delta": 0}], "idem-adj-01"
        )

    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 0


async def test_another_stores_product_cannot_be_corrected(client):
    """A cashier can only reach the shelf they are standing at."""
    owner_id, _, worker, _, _ = await _on_shift()
    elsewhere = await make_store(owner_id, "Խանութ 2")
    theirs = await make_item(owner_id, elsewhere, "Waka 1800", count=5)

    with pytest.raises(BotError):
        await stock_service.adjust_by_worker(
            worker, [{"item_id": theirs, "delta": -5}], "idem-adj-01"
        )

    assert await _count(theirs) == 5


async def test_correcting_needs_an_open_shift(client):
    _, _, worker, item_id, _ = await _on_shift()
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    with pytest.raises(BotError) as caught:
        await stock_service.adjust_by_worker(
            worker, [{"item_id": item_id, "delta": 1}], "idem-adj-01"
        )

    assert caught.value.code == "no_open_session"


async def test_a_retry_does_not_correct_twice(client):
    """One tap of "confirm" adjusts the shelf once, however many times a flaky
    connection resends it."""
    _, _, worker, item_id, _ = await _on_shift()

    first = await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 5}], "idem-adj-01"
    )
    second = await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 5}], "idem-adj-01"
    )

    assert second["duplicate"] is True
    assert second["adjusted"][0]["id"] == first["adjusted"][0]["id"]
    assert await _count(item_id) == 15
    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 1


async def test_it_is_neither_a_sale_nor_breakage(client):
    """A count going up is not income and a count going down is not an expense."""
    _, _, worker, item_id, _ = await _on_shift()

    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": -2}], "idem-adj-01"
    )

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_applies_a_batch(client, bot_headers):
    _, _, _, item_id, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items/adjust",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-adj-01",
              "lines": [{"item_id": item_id, "delta": 4}]},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["adjusted"][0]["delta"] == 4
    assert body["adjusted"][0]["count_after"] == 14


async def test_the_endpoint_shape_is_what_the_bot_reads(client, bot_headers):
    _, _, _, item_id, telegram_id = await _on_shift()

    body = (await client.post(
        f"{BASE}/items/adjust",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-adj-01",
              "lines": [{"item_id": item_id, "delta": 4}]},
        headers=bot_headers,
    )).json()

    assert set(body) >= {"ok", "duplicate", "adjusted"}
    assert set(body["adjusted"][0]) >= {"id", "item_id", "name", "delta", "count_after"}


async def test_an_empty_batch_is_refused(client, bot_headers):
    _, _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items/adjust",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-adj-01", "lines": []},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- what the owner sees ------------------------------------------------------

async def test_it_shows_on_the_report_with_who_did_it(client):
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 6}], "idem-adj-01"
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Պահեստի ուղղումներ" in page.text
    assert "HQD Cuvie" in page.text
    assert "Անի" in page.text
    assert "+6" in page.text


async def test_the_session_rows_are_readable_on_their_own(client):
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": -3}], "idem-adj-01"
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    rows = await adjustments_repo.for_session(session_id)

    assert len(rows) == 1
    assert rows[0]["name"] == "HQD Cuvie"
    assert rows[0]["delta"] == -3
    assert rows[0]["worker_name"] == "Անի"
