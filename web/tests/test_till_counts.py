"""The money that stays in the shop, and the money that goes to the owner.

Each store keeps a float in its drawer. At the end of a shift the worker says how much
they are leaving; that becomes the store's balance, and everything else in the till
goes to the owner:

    handed over  =  what the till held  −  what was left behind

Three things have to happen together for that to hold — the count is recorded, the
balance is updated, the handover is booked — so most of these tests are about the
three agreeing rather than about any one of them.

Nobody is asked at the start of a shift. That asked a worker to answer for a drawer
somebody else had filled.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import AppError, BotError
from app.repo import money as money_repo
from app.repo import till as till_repo
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

BASE = "/api/bot/v1"


async def _a_shop(balance: str = "0.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    if Decimal(balance) > 0:
        await db.execute(
            "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(balance)
        )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, store_id, item_id


async def _worker(owner_id: int, name: str = "Անի"):
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="0.00")
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("0.00")
    ), telegram_id


async def _open(worker, key: str):
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, key, 900)


async def _till(session_id: int) -> Decimal:
    return Decimal((await money_repo.totals_for_session(session_id))["cash"])


async def _session() -> int:
    """The newest session, by id and not by time: a test runs in one transaction, so
    ``now()`` is frozen and two sessions share an ``opened_at``."""
    return await db.fetchval("SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1")


async def _balance(store_id: int) -> Decimal:
    return await db.fetchval("SELECT till_balance FROM stores WHERE id = $1", store_id)


# -- the store's own float ----------------------------------------------------

async def test_a_new_shop_starts_with_nothing_in_its_drawer(client):
    _, store_id, _ = await _a_shop()

    assert await _balance(store_id) == Decimal("0.00")


async def test_a_session_opens_with_the_shops_float(client):
    """The cash was in the drawer overnight and still is in the morning."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    assert await _till(await _session()) == Decimal("40000.00")


async def test_the_float_arrives_as_an_ordinary_deposit(client):
    """So "how much is in the till" stays a sum over one table."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    row = await db.fetchrow("SELECT kind, method, amount, note FROM cash_movements")
    assert row["kind"] == "deposit"
    assert row["amount"] == Decimal("40000.00")
    assert row["note"] == till_service.NOTE_CARRIED_OVER


async def test_an_empty_shop_opens_with_no_movement_at_all(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0


async def test_joining_a_running_session_does_not_add_the_float_again(client):
    """The float belongs to the session, not to whoever walks in."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    first, _ = await _worker(owner_id, "Անի")
    second, _ = await _worker(owner_id, "Գոռ")

    await _open(first, "idem-open-1")
    await _open(second, "idem-open-2")

    assert await _till(await _session()) == Decimal("40000.00")


# -- the handover -------------------------------------------------------------

async def test_what_is_left_becomes_the_shops_balance(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert await _balance(store_id) == Decimal("30000.00")


async def test_the_rest_goes_to_the_owner(client):
    """40,000 float plus 7,000 taken, less 30,000 left, is 17,000 handed over."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    result = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert result["count"]["expected"] == "47000.00", "what the till held"
    assert result["count"]["counted"] == "30000.00", "what stays in the shop"
    assert result["count"]["handed_over"] == "17000.00"


async def test_the_handover_is_booked_so_the_ledger_matches_the_drawer(client):
    """The cash genuinely left the shop. Reading it back out of arithmetic would leave
    every report describing money that is no longer there."""
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()

    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    row = await db.fetchrow(
        "SELECT kind, amount, note, created_by FROM cash_movements WHERE kind = 'withdrawal'"
    )
    assert row["amount"] == Decimal("-17000.00")
    assert row["note"] == till_service.NOTE_HANDED_OVER
    assert row["created_by"] == "worker"
    assert await _till(session_id) == Decimal("30000.00"), "the till now says the drawer"


async def test_the_days_takings_are_untouched_by_the_handover(client):
    """Handing cash to the owner is not un-selling anything."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    day = await money_repo.day_totals_for_store(owner_id, store_id)
    assert day["day_cash"] == Decimal("7000.00")


async def test_leaving_everything_hands_over_nothing(client):
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    result = await till_service.declare_close(worker, Decimal("40000"), "idem-till-1")

    assert result["count"]["handed_over"] == "0.00"
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 0


async def test_leaving_more_than_the_till_held_is_recorded_as_found_cash(client):
    """Usually an unrecorded sale. Nothing was handed anywhere, so it is not booked as
    a handover — and the money stays put."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    result = await till_service.declare_close(worker, Decimal("45000"), "idem-till-1")

    assert result["count"]["handed_over"] == "-5000.00"
    row = await db.fetchrow(
        "SELECT amount, note FROM cash_movements WHERE kind = 'adjustment'"
    )
    assert row["amount"] == Decimal("5000.00")
    assert row["note"] == till_service.NOTE_FOUND_EXTRA
    assert await _balance(store_id) == Decimal("45000.00")


async def test_tomorrow_opens_with_what_was_left(client):
    """The whole cycle, which is the thing worth holding in place."""
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    await _open(worker, "idem-open-2")

    assert await _till(await _session()) == Decimal("30000.00")


async def test_a_later_count_replaces_an_earlier_one(client):
    """Which is what makes counting early safe rather than something to undo."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await till_service.declare_close(worker, Decimal("25000"), "idem-till-2")

    assert await _balance(store_id) == Decimal("25000.00")


# -- refusals and retries -----------------------------------------------------

async def test_a_negative_count_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await till_service.declare_close(worker, Decimal("-1"), "idem-till-1")


async def test_an_absurd_count_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await till_service.declare_close(
            worker, Decimal("99999999999"), "idem-till-1"
        )


async def test_it_can_be_counted_after_the_shift_has_ended(client):
    """Which is when it is asked for. Requiring an open shift would refuse the question
    at the moment it is being answered."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    await till_service.declare_close(worker, Decimal("35000"), "idem-till-1")

    assert await _balance(store_id) == Decimal("35000.00")


async def test_a_retry_hands_over_once(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    first = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    second = await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    assert second["duplicate"] is True
    assert second["count"]["handed_over"] == first["count"]["handed_over"]
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 1
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 1


# -- the owner's correction ---------------------------------------------------

async def test_the_owner_can_set_a_shops_float(client):
    """The balance is a real quantity somebody can be wrong about, and only the owner
    can settle it — a worker cannot be asked about a drawer they are not standing at."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await _balance(store_id) == Decimal("25000.00")


async def test_the_owners_correction_is_recorded_as_theirs(client):
    owner_id, store_id, _ = await _a_shop()

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"), "վերահաշվարկ")

    row = await db.fetchrow("SELECT kind, counted, worker_id, note FROM till_counts")
    assert row["kind"] == "owner"
    assert row["counted"] == Decimal("25000.00")
    assert row["worker_id"] is None, "not attributed to a worker"
    assert row["note"] == "վերահաշվարկ"


async def test_correcting_an_open_shop_moves_its_till_too(client):
    """Otherwise the figure on the page and the figure the shop is working from
    disagree until closing time."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await _till(await _session()) == Decimal("25000.00")


async def test_correcting_a_closed_shop_touches_no_ledger(client):
    """There is no session for the movement to belong to, and the balance alone is the
    truth about a shut shop's drawer."""
    owner_id, store_id, _ = await _a_shop(balance="40000.00")

    await till_service.set_by_owner(owner_id, store_id, Decimal("25000"))

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0
    assert await _balance(store_id) == Decimal("25000.00")


async def test_a_negative_correction_is_refused(client):
    owner_id, store_id, _ = await _a_shop()

    with pytest.raises(AppError):
        await till_service.set_by_owner(owner_id, store_id, Decimal("-1"))


async def test_another_owners_shop_cannot_be_corrected(client):
    owner_id, store_id, _ = await _a_shop()
    stranger = await make_owner("@stranger")

    with pytest.raises(AppError):
        await till_service.set_by_owner(stranger, store_id, Decimal("100"))


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_records_a_handover(client, bot_headers):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": "30000.00",
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["count"]["handed_over"] == "10000.00"
    assert set(body["count"]) >= {"id", "counted", "expected", "handed_over"}
    assert await _balance(store_id) == Decimal("30000.00")


async def test_the_endpoint_no_longer_takes_a_kind(client, bot_headers):
    """There is one count now, so ``kind`` is refused rather than ignored — the house
    rule for this API, and the thing that tells you a stale bot is still deployed
    instead of letting it quietly record the wrong count."""
    owner_id, _, _ = await _a_shop(balance="40000.00")
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": "30000.00", "kind": "close",
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_money_never_arrives_as_a_float(client, bot_headers):
    owner_id, _, _ = await _a_shop()
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "counted": 30000.0,
              "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- what the owner sees ------------------------------------------------------

async def test_the_footer_shows_a_closed_shops_float(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    await login(client, "@ownerhandle")

    page = await client.get("/partials/footer")

    assert "40,000.00" in page.text


async def test_the_store_page_shows_the_float_and_who_set_it(client):
    owner_id, store_id, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/stores/{store_id}")

    assert "Խանութի դրամարկղը" in page.text
    assert "30,000.00" in page.text
    assert "Անի" in page.text


async def test_the_owner_can_edit_it_from_the_store_page(client):
    owner_id, store_id, _ = await _a_shop(balance="40000.00")
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        f"/stores/{store_id}/till-balance",
        data={"csrf_token": csrf, "amount": "25000", "note": "վերահաշվարկ"},
    )

    assert response.status_code == 200
    assert await _balance(store_id) == Decimal("25000.00")


async def test_the_handover_shows_on_the_report(client):
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Դրամարկղի հանձնում" in page.text
    assert "17,000.00" in page.text, "what the owner should have received"
    assert "30,000.00" in page.text, "and what stayed in the shop"


async def test_the_session_rows_carry_the_handover(client):
    owner_id, _, item_id = await _a_shop(balance="40000.00")
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await till_service.declare_close(worker, Decimal("30000"), "idem-till-1")

    rows = await till_repo.for_session(session_id)

    assert len(rows) == 1
    assert rows[0]["handed_over"] == Decimal("17000.00")
    assert rows[0]["counted"] == Decimal("30000.00")
    assert rows[0]["worker_name"] == "Անի"
