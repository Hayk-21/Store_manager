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
from app.errors import AppError, BotError
from app.repo import adjustments as adjustments_repo
from app.services import corrections
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


# -- taking one back ---------------------------------------------------------
#
# Two doors, deliberately different. The cashier's undo keeps their mistake on the
# record and writes the opposite correction beside it — a worker undoing their own
# slip must not be able to make it disappear, which is the rule a voided sale
# already follows. The owner's delete removes the row outright, because the owner
# is who the log is *for*.

async def test_a_cashier_can_take_back_what_they_just_corrected(client):
    """39 where they meant 37, and no way to say so: the fix was another correction,
    which needs the number the shelf had before — and the bot had just replaced it
    with the new one on screen."""
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 2}], "idem-adj-01"
    )

    await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")

    assert await _count(item_id) == 10, "back where the shelf started"


async def test_taking_one_back_keeps_the_mistake_on_the_record(client):
    """A worker undoing their own slip must not be able to hide it — the same rule
    a voided sale follows, where the receipt stays and is struck through."""
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 2}], "idem-adj-01"
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")
    rows = await adjustments_repo.for_session(session_id)

    assert [row["delta"] for row in rows] == [-2, 2], "both, newest first"
    assert rows[0]["note"] == stock_service.UNDO_NOTE


async def test_the_whole_batch_comes_back_together(client):
    """One tap of confirm corrected four products; undoing it undoes the tap."""
    owner_id, store_id, worker, first, _ = await _on_shift()
    second = await make_item(owner_id, store_id, "Elf Bar", count=4, sell_price="3000.00")
    await stock_service.adjust_by_worker(
        worker,
        [{"item_id": first, "delta": 5}, {"item_id": second, "delta": -2}],
        "idem-adj-01",
    )

    await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")

    assert await _count(first) == 10
    assert await _count(second) == 4


async def test_undoing_twice_does_not_double_it(client):
    """A retried tap is the same tap. Its own key, checked under the shift lock."""
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 3}], "idem-adj-01"
    )

    await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")
    await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")

    assert await _count(item_id) == 10


async def test_stock_already_sold_cannot_be_taken_back(client):
    """A correction that added ten cannot be reversed once eight have been sold —
    saying so beats letting a count go negative."""
    _, _, worker, item_id, _ = await _on_shift(count=0)
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 10}], "idem-adj-01"
    )
    await db.execute("UPDATE items SET count = 2 WHERE id = $1", item_id)

    with pytest.raises(BotError):
        await stock_service.undo_by_worker(worker, "idem-adj-01", "idem-undo-01")

    assert await _count(item_id) == 2, "nothing moved"


async def test_one_cashier_cannot_undo_anothers_correction(client):
    owner_id, store_id, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 4}], "idem-adj-01"
    )
    other_id, _ = await make_worker(owner_id, "Բաբկեն", salary_amount="0.00")
    other = shifts_service.Worker(
        id=other_id, owner_id=owner_id, name="Բաբկեն", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(other, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900)

    with pytest.raises(BotError):
        await stock_service.undo_by_worker(other, "idem-adj-01", "idem-undo-01")

    assert await _count(item_id) == 14


async def test_the_bot_endpoint_takes_it_back(client, bot_headers):
    _, _, worker, item_id, telegram_id = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 7}], "idem-adj-01"
    )

    response = await client.post(
        f"{BASE}/items/adjust/undo",
        json={"telegram_id": telegram_id, "external_id": "idem-adj-01",
              "idempotency_key": "idem-undo-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201, response.text
    assert await _count(item_id) == 10


# -- the owner removing one --------------------------------------------------

async def test_the_owner_can_delete_a_correction_and_the_count_returns(client):
    """The report has always shown these and never let the owner do anything about
    them, so a cashier's mistake could only be answered with a second correction."""
    owner_id, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 6}], "idem-adj-01"
    )
    row_id = await db.fetchval("SELECT id FROM stock_adjustments")

    await corrections.delete_adjustment(owner_id, owner_id, row_id)

    assert await _count(item_id) == 10
    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 0


async def test_deleting_a_correction_is_undoable(client):
    owner_id, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 6}], "idem-adj-01"
    )
    row_id = await db.fetchval("SELECT id FROM stock_adjustments")
    await corrections.delete_adjustment(owner_id, owner_id, row_id)

    event = await db.fetchrow(
        "SELECT id FROM audit_events WHERE action = 'delete_adjustment'"
    )
    await corrections.revert(owner_id, owner_id, event["id"])

    assert await _count(item_id) == 16, "the correction is back"
    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 1


async def test_a_correction_whose_goods_have_sold_cannot_be_deleted(client):
    """Removing it would leave a count below zero, which no shelf can hold."""
    owner_id, _, worker, item_id, _ = await _on_shift(count=0)
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 10}], "idem-adj-01"
    )
    row_id = await db.fetchval("SELECT id FROM stock_adjustments")
    await db.execute("UPDATE items SET count = 2 WHERE id = $1", item_id)

    with pytest.raises(AppError):
        await corrections.delete_adjustment(owner_id, owner_id, row_id)

    assert await db.fetchval("SELECT count(*) FROM stock_adjustments") == 1


async def test_the_report_offers_the_delete(client):
    _, _, worker, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 6}], "idem-adj-01"
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "/adjustments/" in page.text
