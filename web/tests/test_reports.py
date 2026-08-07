"""/reports — organised by store session, because that is the accounting period."""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
)


async def _a_completed_shift():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="8000.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("8000.00")
    )
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=10, sell_price="3500.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 3}], "cash", "idem-key-sale-1"
    )
    return owner_id, store_id, worker, item_id


async def test_the_list_shows_one_row_per_time_the_store_was_open(client):
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    response = await client.get("/reports")

    assert response.status_code == 200
    assert "Խանութ 1" in response.text
    # 10500 taken in, 8000 paid out.
    assert "2,500.00" in response.text


async def test_an_open_session_shows_live_numbers_not_a_snapshot(client):
    await _a_completed_shift()
    await login(client, "@ownerhandle")

    response = await client.get("/reports")

    assert "դեռ բաց" in response.text
    assert "10,500.00" in response.text


async def test_the_detail_view_shows_shifts_receipts_and_the_ledger(client):
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    response = await client.get(f"/reports?store_session_id={session_id}")

    assert response.status_code == 200
    assert "Անի" in response.text
    assert "HQD Cuvie" in response.text
    assert "Աշխատավարձ" in response.text
    assert "8,000.00" in response.text


async def test_a_voided_receipt_stays_visible_with_who_voided_it(client):
    """A worker undoing their own slip must not be able to hide it."""
    _, _, worker, _ = await _a_completed_shift()
    await sales_service.void_last_sale(worker, "սխալ սեղմեցի")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    response = await client.get(f"/reports?store_session_id={session_id}")

    assert "չեղարկվել է" in response.text
    assert "voided" in response.text, "the row is struck through, not removed"
    assert "Չեղարկում" in response.text, "the reversing ledger entry is shown too"


async def test_another_owners_session_is_not_reachable(client):
    _, _, worker, _ = await _a_completed_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    other = await make_owner("@ownerother")
    await make_store(other)
    await login(client, "@ownerother")

    listing = await client.get("/reports")
    assert "Խանութ 1" not in listing.text

    detail = await client.get(f"/reports?store_session_id={session_id}")
    assert detail.status_code == 404
