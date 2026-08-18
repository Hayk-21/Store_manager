"""Deleting a report, and deleting one write-off.

Two deletions that look alike and mean different things, which is the point of
testing them together.

Deleting a report says "this record should not exist". It does not say the goods
came back, so the stock counts are left alone — putting them up would invent stock
the shop does not have on its shelves. Reversing a sale is what the void button on
each receipt is for.

Deleting a write-off says "the claim was wrong". If the vape did not break then it
is still on the shelf, so the count *does* go back up.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.services import shifts as shifts_service
from app.services import write_offs as write_offs_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_item,
    make_owner,
    make_store,
    make_worker,
)


async def _csrf() -> str:
    return await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )


async def _a_traded_day(close: bool = True):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await write_offs_service.record(worker, item_id, 2, "կոտրված", "idem-wo-01")
    if close:
        await shifts_service.close_out_shift(
            worker,
            [{"item_id": item_id, "quantity": 3, "unit_price": "3500.00",
              "payment_method": "cash"}],
            "idem-close-1",
            close_store_too=True,
            counted=Decimal("0"),
        )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    return owner_id, store_id, item_id, session_id


# -- the report --------------------------------------------------------------

async def test_deleting_a_report_takes_its_sales_and_ledger_with_it(client):
    _, _, _, session_id = await _a_traded_day()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/reports/{session_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 0
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 0
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count(*) FROM sale_items") == 0
    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


async def test_deleting_a_report_leaves_the_stock_alone(client):
    """It says the record should not exist, not that the goods came back. Putting
    the count up would invent stock the shop does not have."""
    _, _, item_id, session_id = await _a_traded_day()
    before = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)
    await login(client, "@ownerhandle")

    await client.post(f"/reports/{session_id}/delete", data={"csrf_token": await _csrf()})

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == before
    assert before == 45, "50 less 3 sold and 2 written off"


async def test_an_open_report_cannot_be_deleted(client):
    """Deleting one that is still running would take the ground out from under the
    workers standing in it."""
    _, _, _, session_id = await _a_traded_day(close=False)
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/reports/{session_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1


async def test_another_owners_report_reads_as_missing(client):
    _, _, _, session_id = await _a_traded_day()
    await make_owner("@stranger")
    await login(client, "@stranger")

    response = await client.post(
        f"/reports/{session_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1


async def test_the_delete_button_is_on_a_closed_report_only(client):
    await _a_traded_day(close=False)
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "/delete" not in page.text.split("Դրամարկղի շարժ")[0]


# -- one write-off -----------------------------------------------------------

async def test_deleting_a_write_off_puts_the_stock_back(client):
    """If the vape did not break, it is still on the shelf."""
    _, _, item_id, _ = await _a_traded_day()
    write_off_id = await db.fetchval("SELECT id FROM write_offs")
    before = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/write-offs/{write_off_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == before + 2
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


async def test_deleting_a_write_off_touches_no_money(client):
    """Breakage never went through the till, so there is nothing to reverse."""
    _, _, _, _ = await _a_traded_day()
    write_off_id = await db.fetchval("SELECT id FROM write_offs")
    before = await db.fetchval("SELECT count(*) FROM cash_movements")
    await login(client, "@ownerhandle")

    await client.post(
        f"/write-offs/{write_off_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == before


async def test_the_deleted_breakage_stops_counting_as_a_loss(client):
    _, _, _, _ = await _a_traded_day()
    write_off_id = await db.fetchval("SELECT id FROM write_offs")
    await login(client, "@ownerhandle")

    await client.post(
        f"/write-offs/{write_off_id}/delete", data={"csrf_token": await _csrf()}
    )

    from app.config import settings
    from app.repo import write_offs as write_offs_repo

    today = settings.local_day()
    assert await write_offs_repo.cost_between(
        await db.fetchval("SELECT id FROM users"), today, today
    ) == Decimal("0")


async def test_another_owners_write_off_reads_as_missing(client):
    _, _, item_id, _ = await _a_traded_day()
    write_off_id = await db.fetchval("SELECT id FROM write_offs")
    before = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)
    await make_owner("@stranger")
    await login(client, "@stranger")

    response = await client.post(
        f"/write-offs/{write_off_id}/delete", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == before


async def test_the_write_off_table_offers_the_delete(client):
    _, _, _, session_id = await _a_traded_day()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "/write-offs/" in page.text


# -- the income column -------------------------------------------------------

async def test_the_list_shows_what_the_shop_took_in(client):
    """Not cash + card: those are till balances with wages and withdrawals already
    taken off them."""
    _, _, _, _ = await _a_traded_day()
    await login(client, "@ownerhandle")

    page = await client.get("/reports")

    assert "Վաճառք" in page.text
    assert "10,500.00" in page.text, "three at 3,500"
