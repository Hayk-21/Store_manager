"""The correction forms on /reports and /history, driven as a browser would."""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.repo import audit as audit_repo
from app.services import corrections
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


async def _a_closed_session():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount="8000.00")
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=20, sell_price="3500.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("8000.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": 3, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-1",
        close_store_too=True,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    sale_id = await db.fetchval("SELECT id FROM sales")
    return owner_id, store_id, worker_id, item_id, session_id, sale_id


async def _csrf(client) -> str:
    from app.deps import SESSION_COOKIE
    from app.repo import users as users_repo
    from app.security import hash_token

    row = await users_repo.session_with_user(hash_token(client.cookies[SESSION_COOKIE]))
    return row["csrf_token"]


async def test_the_detail_page_offers_the_correction_forms(client):
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.get(f"/reports?store_session_id={session_id}")

    assert response.status_code == 200
    assert "Ավելացնել մոռացված վաճառք" in response.text
    assert "Ավելացնել գրառում" in response.text
    assert "Չեղարկել" in response.text


async def test_voiding_from_the_page_returns_to_the_same_report(client):
    _, _, _, item_id, session_id, sale_id = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/sales/{sale_id}/void", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/reports?store_session_id={session_id}"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20


async def test_editing_a_receipt_in_place_amends_it(client):
    """The row's quantity and price are inputs; ✓ posts them as an amendment."""
    _, _, _, item_id, session_id, sale_id = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/sales/{sale_id}/amend",
        data={
            "csrf_token": await _csrf(client),
            "item_id": str(item_id),
            "quantity": "5",
            "unit_price": "3000",
            "payment_method": "card",
        },
    )

    assert response.status_code == 303
    total = await db.fetchval(
        "SELECT total FROM sales WHERE voided_at IS NULL AND store_session_id = $1", session_id
    )
    assert total == Decimal("15000.00")
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 15


async def test_adding_a_missed_sale_from_the_form(client):
    _, _, worker_id, item_id, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/store-sessions/{session_id}/sales",
        data={
            "csrf_token": await _csrf(client),
            "worker_id": str(worker_id),
            "item_id": str(item_id),
            "quantity": "2",
            "unit_price": "",
            "payment_method": "card",
        },
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 15
    assert await db.fetchval(
        "SELECT card_at_close FROM store_sessions WHERE id = $1", session_id
    ) == Decimal("7000.00"), "the price defaulted to the shelf price"


async def test_a_receipt_offers_both_voiding_and_deleting(client):
    """They mean different things: one shows the sale was reversed, the other
    says it should never have been on the report at all."""
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Չեղարկել" in page.text
    assert "/delete" in page.text


async def test_deleting_a_receipt_from_the_page(client):
    _, _, _, item_id, session_id, sale_id = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/sales/{sale_id}/delete", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/reports?store_session_id={session_id}"
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20

    page = await client.get(f"/reports?store_session_id={session_id}")
    assert "Այս հերթափոխին վաճառք չի եղել" in page.text


async def test_a_deleted_receipt_can_be_brought_back_from_the_history(client):
    owner_id, _, _, item_id, session_id, sale_id = await _a_closed_session()
    await login(client, "@ownerhandle")
    await client.post(f"/sales/{sale_id}/delete", data={"csrf_token": await _csrf(client)})
    event = await audit_repo.newest_pending(owner_id)

    response = await client.post(
        f"/history/{event['id']}/revert", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT total FROM sales") == Decimal("10500.00")
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17


async def test_a_sale_ledger_row_says_where_to_delete_it_instead(client):
    """Removing it alone would leave the receipt and the ledger disagreeing."""
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")
    movement = await db.fetchval("SELECT id FROM cash_movements WHERE kind = 'sale'")

    response = await client.post(
        f"/movements/{movement}/delete", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 422
    assert "Չեկեր" in response.text


async def test_a_movement_without_a_purpose_is_refused(client):
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/store-sessions/{session_id}/movements",
        data={"csrf_token": await _csrf(client), "kind": "withdrawal",
              "method": "cash", "amount": "50000", "purpose": ""},
    )

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'") == 0


async def test_recording_a_payment_and_seeing_it_in_the_ledger(client):
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    await client.post(
        f"/store-sessions/{session_id}/movements",
        data={"csrf_token": await _csrf(client), "kind": "withdrawal",
              "method": "cash", "amount": "50000",
              "purpose": "բլոգերին վճարված գումար"},
    )

    page = await client.get(f"/reports?store_session_id={session_id}")
    assert "բլոգերին վճարված գումար" in page.text
    assert "-50,000.00" in page.text
    assert "/delete" in page.text, "an owner-entered row can be removed again"


async def test_the_ledgers_own_rows_have_no_delete_button(client):
    """A sale or salary row is the record of something that happened."""
    *_, session_id, _ = await _a_closed_session()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Աշխատավարձ" in page.text
    assert "/movements/" not in page.text


async def test_the_history_page_lists_corrections_and_offers_the_last_one_back(client):
    owner_id, _, _, _, session_id, sale_id = await _a_closed_session()
    await corrections.void_sale(owner_id, owner_id, sale_id, "սխալ")
    await login(client, "@ownerhandle")

    page = await client.get("/history")

    assert page.status_code == 200
    assert "Վաճառքի չեղարկում" in page.text
    assert "Հետ շրջել" in page.text


async def test_reverting_from_the_page_restores_the_books(client):
    owner_id, _, _, item_id, session_id, sale_id = await _a_closed_session()
    await corrections.void_sale(owner_id, owner_id, sale_id)
    event = await audit_repo.newest_pending(owner_id)
    await login(client, "@ownerhandle")

    response = await client.post(
        f"/history/{event['id']}/revert", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT voided_at FROM sales WHERE id = $1", sale_id) is None
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17

    page = await client.get("/history")
    assert "հետ շրջված" in page.text


async def test_a_correction_shows_up_on_the_session_it_belongs_to(client):
    owner_id, _, _, _, session_id, sale_id = await _a_closed_session()
    await corrections.void_sale(owner_id, owner_id, sale_id, "սխալ")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Այս հերթափոխի ուղղումները" in page.text


async def test_another_owner_cannot_correct_what_is_not_theirs(client):
    _, _, _, _, _, sale_id = await _a_closed_session()
    await make_owner("@ownerother")
    await login(client, "@ownerother")

    response = await client.post(
        f"/sales/{sale_id}/void", data={"csrf_token": await _csrf(client)}
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT voided_at FROM sales WHERE id = $1", sale_id) is None
