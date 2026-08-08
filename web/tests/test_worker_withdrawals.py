"""A cashier taking cash out of the till.

It happens whether or not the system knows — paying a delivery, buying bags,
sending somebody for change. So the choice is not between allowing it and
forbidding it; it is between an unexplained shortfall at close and a row saying
where the money went. That is why the purpose is required: an amount with no
reason *is* the shortfall, just with a number attached.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.services import money as money_service
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

BASE = "/api/bot/v1"


async def _till_with(cash: str = "20000.00"):
    """A worker on shift with money already taken today."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=100, self_price="0.00", sell_price=cash
    )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    if Decimal(cash) > 0:
        from app.services import sales as sales_service

        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-key-sale-01"
        )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    return owner_id, store_id, worker, telegram_id, session_id


# -- taking it ----------------------------------------------------------------

async def test_taking_cash_reduces_the_till(client):
    _, _, worker, _, session_id = await _till_with("20000.00")

    result = await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )

    assert result["store_totals"]["cash"] == "19200.00"
    totals = await money_repo.totals_for_session(session_id)
    assert totals["cash"] == Decimal("19200.00")
    assert totals["withdrawn"] == Decimal("800.00")


async def test_the_purpose_is_recorded_with_it(client):
    """The reason is the entire point of the row."""
    _, _, worker, _, _ = await _till_with()

    await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին վճարված", "idem-key-cash-01"
    )

    row = await db.fetchrow(
        "SELECT note, created_by, worker_id FROM cash_movements WHERE kind = 'withdrawal'"
    )
    assert row["note"] == "առաքիչին վճարված"
    assert row["created_by"] == "worker", "told apart from what the owner typed"
    assert row["worker_id"] == worker.id


async def test_an_amount_with_no_purpose_is_refused(client):
    _, _, worker, _, _ = await _till_with()

    with pytest.raises(BotError):
        await money_service.withdraw_by_worker(
            worker, Decimal("800"), "   ", "idem-key-cash-01"
        )

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 0


async def test_taking_more_than_the_till_holds_is_refused(client):
    """Not a rule about permission — you cannot take more notes than are in the
    drawer, so a bigger number is a typo."""
    _, _, worker, _, _ = await _till_with("500.00")

    with pytest.raises(BotError) as caught:
        await money_service.withdraw_by_worker(
            worker, Decimal("900"), "առաքիչին", "idem-key-cash-01"
        )

    assert "500" in caught.value.message
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 0


async def test_taking_more_than_the_limit_is_refused(client):
    """A cashier may cover a delivery driver, not bank the takings. Anything
    larger is the owner's decision, made where they can see the whole shop."""
    _, _, worker, _, _ = await _till_with("20000.00")

    with pytest.raises(BotError) as caught:
        await money_service.withdraw_by_worker(
            worker, Decimal("5000"), "առաքիչին", "idem-key-cash-01"
        )

    assert "1,000" in caught.value.message
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 0


async def test_exactly_the_limit_is_allowed(client):
    _, _, worker, _, session_id = await _till_with("20000.00")

    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-key-cash-01"
    )

    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("19000.00")


async def test_the_limit_is_for_the_whole_shift_not_for_one_tap(client):
    """Otherwise it is not a limit: you take 1,000 four times."""
    _, _, worker, _, session_id = await _till_with("20000.00")
    await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )

    with pytest.raises(BotError) as caught:
        await money_service.withdraw_by_worker(
            worker, Decimal("800"), "տոպրակներ", "idem-key-cash-02"
        )

    assert "200" in caught.value.message, "what is left of the allowance"
    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("19200.00")


async def test_what_is_left_of_the_allowance_can_still_be_taken(client):
    _, _, worker, _, session_id = await _till_with("20000.00")
    await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )

    await money_service.withdraw_by_worker(
        worker, Decimal("200"), "մանրադրամ", "idem-key-cash-02"
    )

    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("19000.00")


async def test_the_allowance_starts_again_next_shift(client):
    """It is a limit on a working day, not a lifetime one."""
    _, _, worker, _, _ = await _till_with("20000.00")
    item_id = await db.fetchval("SELECT id FROM items")
    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-key-cash-01"
    )
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    # A new session starts with an empty till — the old one was settled and
    # handed over — so there has to be a sale before there is anything to take.
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900
    )
    from app.services import sales as sales_service

    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-key-sale-02"
    )

    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-key-cash-02"
    )

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 2


async def test_what_the_owner_took_does_not_eat_the_cashiers_allowance(client):
    """The owner banking the takings is a different act with a different reason,
    and it is not the cashier's 1,000."""
    owner_id, store_id, worker, _, session_id = await _till_with("20000.00")
    await money_service.record_movement(
        owner_id, store_id, "cash", "withdrawal", Decimal("5000"), note="բանկ"
    )

    await money_service.withdraw_by_worker(
        worker, Decimal("1000"), "առաքիչին", "idem-key-cash-01"
    )

    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("14000.00")


async def test_a_negative_amount_is_refused(client):
    _, _, worker, _, _ = await _till_with()

    with pytest.raises(BotError):
        await money_service.withdraw_by_worker(
            worker, Decimal("-100"), "առաքիչին", "idem-key-cash-01"
        )


async def test_taking_cash_needs_an_open_shift(client):
    _, _, worker, _, _ = await _till_with("0.00")
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    with pytest.raises(BotError) as caught:
        await money_service.withdraw_by_worker(
            worker, Decimal("100"), "առաքիչին", "idem-key-cash-01"
        )

    assert caught.value.code == "no_open_session"


async def test_a_retry_does_not_take_the_money_twice(client):
    """A flaky connection at the counter must not empty the till twice."""
    _, _, worker, _, session_id = await _till_with("20000.00")

    first = await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )
    second = await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )

    assert second["duplicate"] is True
    assert second["withdrawal"]["id"] == first["withdrawal"]["id"]
    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("19200.00")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'withdrawal'"
    ) == 1


async def test_it_does_not_look_like_a_sale(client):
    """It is money leaving, not money earned."""
    _, _, worker, _, session_id = await _till_with("20000.00")

    await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին", "idem-key-cash-01"
    )

    totals = await money_repo.totals_for_session(session_id)
    assert totals["sales"] == Decimal("20000.00"), "unchanged by the withdrawal"
    assert await db.fetchval("SELECT count(*) FROM sales") == 1


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_records_a_withdrawal(client, bot_headers):
    _, _, _, telegram_id, _ = await _till_with("20000.00")

    response = await client.post(
        f"{BASE}/cash/withdraw",
        json={"telegram_id": telegram_id, "amount": "800.00",
              "purpose": "առաքիչին", "idempotency_key": "idem-key-cash-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["withdrawal"]["amount"] == "800.00"
    assert body["store_totals"]["cash"] == "19200.00"


async def test_the_endpoint_shape_is_what_the_bot_reads(client, bot_headers):
    _, _, _, telegram_id, _ = await _till_with("20000.00")

    body = (await client.post(
        f"{BASE}/cash/withdraw",
        json={"telegram_id": telegram_id, "amount": "800.00",
              "purpose": "առաքիչին", "idempotency_key": "idem-key-cash-01"},
        headers=bot_headers,
    )).json()

    assert set(body) >= {"ok", "duplicate", "withdrawal", "store_totals"}
    assert set(body["withdrawal"]) >= {"id", "amount", "purpose"}
    assert isinstance(body["withdrawal"]["amount"], str)


async def test_the_endpoint_refuses_a_blank_purpose(client, bot_headers):
    _, _, _, telegram_id, _ = await _till_with("20000.00")

    response = await client.post(
        f"{BASE}/cash/withdraw",
        json={"telegram_id": telegram_id, "amount": "800.00",
              "purpose": "", "idempotency_key": "idem-key-cash-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_money_never_arrives_as_a_float(client, bot_headers):
    _, _, _, telegram_id, _ = await _till_with("20000.00")

    response = await client.post(
        f"{BASE}/cash/withdraw",
        json={"telegram_id": telegram_id, "amount": 800.0,
              "purpose": "առաքիչին", "idempotency_key": "idem-key-cash-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- what the owner sees ------------------------------------------------------

async def test_it_shows_on_the_report_with_who_took_it_and_why(client):
    _, _, worker, _, session_id = await _till_with("20000.00")
    await money_service.withdraw_by_worker(
        worker, Decimal("800"), "առաքիչին վճարված", "idem-key-cash-01"
    )
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "առաքիչին վճարված" in page.text
    assert "Անի" in page.text
    assert "-800.00" in page.text
