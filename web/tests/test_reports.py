"""/reports — organised by store session, because that is the accounting period."""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import till as till_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
    worked_a_full_shift,
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
    # A whole day, so the shift wage is the full figure: a shift under eight
    # hours is paid half, and these numbers are not about that rule.
    await worked_a_full_shift(worker_id)
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
    # Three at 3,500, all of it cash.
    assert "10,500.00" in response.text


async def test_an_open_session_shows_live_numbers_not_a_snapshot(client):
    await _a_completed_shift()
    await login(client, "@ownerhandle")

    response = await client.get("/reports")

    assert "դեռ բաց" in response.text
    assert "10,500.00" in response.text


# -- what a row says ---------------------------------------------------------

async def test_the_row_names_the_worker_rather_than_counting_them(client):
    """One person opens a shop and closes it, so «1» answered nothing."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert ">Աշխատող</th>" in page.text
    assert ">Աշխատողներ</th>" not in page.text
    assert "Անի" in page.text


async def test_the_row_splits_the_takings_the_way_the_report_does(client):
    """«Վաճառք · Կանխիկ վաճառք · Քարտով վաճառք», not the drawer balances that were
    here — a wage has already come off those, so they never split the takings."""
    _, _, worker, item_id = await _a_completed_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-key-sale-2"
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "Կանխիկ վաճառք" in page.text and "Քարտով վաճառք" in page.text
    assert "14,000.00" in page.text, "four at 3,500"
    assert "10,500.00" in page.text, "three of them cash"
    assert "3,500.00" in page.text, "one on a card"


async def test_the_row_shows_what_was_delivered(client):
    _, store_id, worker, item_id = await _a_completed_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-key-sale-2",
        is_delivery=True,
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "Առաքում" in page.text
    assert "7,000.00" in page.text, "two at 3,500 went out by delivery"
    assert "1 պատվեր" in page.text


async def test_the_rows_profit_is_the_reports_own_profit(client):
    """The column and the header it opens are one subtraction, not two."""
    _, _, worker, _ = await _a_completed_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    listing = await client.get("/reports")
    detail = await client.get(f"/reports?store_session_id={session_id}")

    # Margin 6,000 (three at 3,500 that cost 1,500) less an 8,000 wage.
    assert "-2,000.00" in listing.text
    assert "-2,000.00" in detail.text


async def test_the_row_says_what_stayed_in_the_shop(client):
    """The evening's last count of the drawer, by the rule the report itself uses."""
    _, _, worker, _ = await _a_completed_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await till_service.declare_close(worker, Decimal("1500"), "idem-till-1")
    await login(client, "@ownerhandle")

    listing = await client.get("/reports")
    detail = await client.get(f"/reports?store_session_id={session_id}")

    assert "Մնաց խանութում" in listing.text
    assert "1,500.00" in listing.text
    assert "1,500.00" in detail.text
    assert "հաշվարկով" not in listing.text, "somebody counted, so nothing is inferred"


async def test_an_uncounted_drawer_leaves_the_float_not_nothing(client):
    """A worker who goes home without counting has not emptied the shop: the float
    stays where it is, because nothing but a count moves it."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute("UPDATE stores SET till_balance = 4000 WHERE id = $1", store_id)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await shifts_service.end_shift(worker, None, None, "idem-end-1")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "4,000.00" in page.text, "the float it opened with, not nothing"
    assert "հաշվարկով" in page.text, "and the row says the figure was worked out"


async def test_an_open_sessions_wage_is_not_zero_in_the_list(client):
    """It is read from the ledger, not from the snapshot taken at closing time —
    which is written when the shop shuts, and is nothing until then."""
    owner_id, store_id, first, _ = await _a_completed_shift()
    second_id, _ = await make_worker(owner_id, "Բաբկեն", salary_amount="5000.00")
    second = shifts_service.Worker(
        id=second_id, owner_id=owner_id, name="Բաբկեն",
        salary_amount=Decimal("5000.00"),
    )
    await shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900)
    # The first one goes home; the shop stays open behind them, so nothing has
    # been snapshotted yet.
    await shifts_service.end_shift(first, None, None, "idem-key-end-01")
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "դեռ բաց" in page.text
    assert "8,000.00" in page.text, "the wage the drawer has already paid"
    assert "Անի · Բաբկեն" in page.text or "Բաբկեն · Անի" in page.text


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
