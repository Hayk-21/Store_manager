"""Counting the drawer, and carrying what is in it to the next shift.

Two facts that must not be allowed to overwrite each other: what the books say is
in the till, and what somebody counted. The gap between them is the only figure
here with no other home, so the tests care most about it surviving.

And the money does not vanish overnight. A shop that keeps cash in the drawer still
has it in the morning, so the next session's till has to start there — otherwise the
first sale of the day makes the drawer look that much heavy.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
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


async def _a_shop():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
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
    """The newest store session, by id and not by time.

    A test runs inside one transaction, so ``now()`` returns the same instant for
    every statement in it and two sessions opened a few lines apart carry an
    identical ``opened_at``. Ordering by that would pick either one.
    """
    return await db.fetchval("SELECT id FROM store_sessions ORDER BY id DESC LIMIT 1")


# -- the count itself --------------------------------------------------------

async def test_a_matching_count_records_no_difference(client):
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    result = await till_service.declare(worker, "close", Decimal("3500"), "idem-till-1")

    assert result["count"]["counted"] == "3500.00"
    assert result["count"]["expected"] == "3500.00"
    assert result["count"]["difference"] == "0.00"


async def test_a_short_drawer_is_recorded_as_short(client):
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    result = await till_service.declare(worker, "close", Decimal("3000"), "idem-till-1")

    assert result["count"]["difference"] == "-500.00"


async def test_a_heavy_drawer_is_recorded_too(client):
    """Usually an unrecorded sale, and worth knowing for exactly that reason."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    result = await till_service.declare(worker, "close", Decimal("4000"), "idem-till-1")

    assert result["count"]["difference"] == "500.00"


async def test_the_count_never_touches_the_ledger(client):
    """The gap between the books and the drawer is the point of counting. Letting
    the count become the balance would erase the only record of it."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    session_id = await _session()

    await till_service.declare(worker, "close", Decimal("1"), "idem-till-1")

    assert await _till(session_id) == Decimal("3500.00")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'adjustment'"
    ) == 0


async def test_what_the_books_said_is_frozen_beside_the_count(client):
    """A sale amended next week must not rewrite whether the till balanced tonight."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    await till_service.declare(worker, "close", Decimal("3500"), "idem-till-1")

    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-2"
    )

    assert await db.fetchval("SELECT expected FROM till_counts") == Decimal("3500.00")


async def test_a_retry_does_not_record_it_twice(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    first = await till_service.declare(worker, "close", Decimal("500"), "idem-till-1")
    second = await till_service.declare(worker, "close", Decimal("500"), "idem-till-1")

    assert second["duplicate"] is True
    assert second["count"]["id"] == first["count"]["id"]
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 1


async def test_a_negative_count_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await till_service.declare(worker, "close", Decimal("-1"), "idem-till-1")


async def test_an_unknown_kind_is_refused(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    with pytest.raises(BotError):
        await till_service.declare(worker, "sideways", Decimal("100"), "idem-till-1")


async def test_it_can_be_counted_after_the_shift_has_ended(client):
    """Which is when it is asked for. Requiring an open shift would refuse the
    question at the moment it is being answered."""
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")

    result = await till_service.declare(worker, "close", Decimal("4000"), "idem-till-1")

    assert result["count"]["counted"] == "4000.00"


# -- carrying it over --------------------------------------------------------

async def test_the_next_session_opens_with_what_was_left(client):
    """The money stayed in the shop overnight, so the till has to start there."""
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await till_service.declare(worker, "close", Decimal("40000"), "idem-till-1")

    await _open(worker, "idem-open-2")

    assert await _till(await _session()) == Decimal("40000.00")


async def test_the_carried_over_cash_is_an_ordinary_deposit(client):
    """So "how much is in the till" stays a sum over one table and every figure
    that already existed keeps working."""
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await till_service.declare(worker, "close", Decimal("40000"), "idem-till-1")

    await _open(worker, "idem-open-2")

    row = await db.fetchrow(
        """
        SELECT kind, method, amount, note FROM cash_movements
         WHERE store_session_id = $1
        """,
        await _session(),
    )
    assert row["kind"] == "deposit"
    assert row["method"] == "cash"
    assert row["amount"] == Decimal("40000.00")
    assert row["note"] == till_service.NOTE_CARRIED_OVER


async def test_a_shop_nobody_has_ever_counted_starts_empty(client):
    """Never counted is "unknown", not zero, and inventing an opening balance would
    be worse than starting at nothing."""
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)

    await _open(worker, "idem-open-1")

    assert await _till(await _session()) == Decimal("0")
    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0


async def test_joining_a_running_session_does_not_add_the_float_again(client):
    """Two workers, one drawer. The float belongs to the session, not to whoever
    walks in."""
    owner_id, _, _ = await _a_shop()
    first, _ = await _worker(owner_id, "Անի")
    second, _ = await _worker(owner_id, "Գոռ")
    await _open(first, "idem-open-1")
    await worked_a_full_shift(first.id)
    await shifts_service.close_out_shift(first, [], "idem-close-1")
    await till_service.declare(first, "close", Decimal("40000"), "idem-till-1")

    await _open(first, "idem-open-2")
    await _open(second, "idem-open-3")

    assert await _till(await _session()) == Decimal("40000.00")


async def test_only_the_most_recent_count_carries_over(client):
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await till_service.declare(worker, "close", Decimal("10000"), "idem-till-1")
    await till_service.declare(worker, "close", Decimal("25000"), "idem-till-2")

    await _open(worker, "idem-open-2")

    assert await _till(await _session()) == Decimal("25000.00")


async def test_an_opening_count_is_measured_against_the_float(client):
    """A drawer that is short is then found at the start of a shift by somebody who
    did not cause it, rather than at the end by whoever gets blamed."""
    owner_id, _, _ = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await till_service.declare(worker, "close", Decimal("40000"), "idem-till-1")
    await _open(worker, "idem-open-2")

    result = await till_service.declare(worker, "open", Decimal("38000"), "idem-till-2")

    assert result["count"]["expected"] == "40000.00"
    assert result["count"]["difference"] == "-2000.00"


async def test_a_sale_after_the_handover_adds_to_the_float(client):
    """The whole reason for carrying it: without it the first sale of the day makes
    the drawer look that much heavy."""
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await worked_a_full_shift(worker.id)
    await shifts_service.close_out_shift(worker, [], "idem-close-1")
    await till_service.declare(worker, "close", Decimal("40000"), "idem-till-1")
    await _open(worker, "idem-open-2")

    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-2"
    )

    assert await _till(await _session()) == Decimal("43500.00")


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_records_a_count(client, bot_headers):
    owner_id, _, _ = await _a_shop()
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "kind": "close",
              "counted": "40000.00", "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["count"]["counted"] == "40000.00"
    assert set(body["count"]) >= {"id", "kind", "counted", "expected", "difference"}


async def test_money_never_arrives_as_a_float(client, bot_headers):
    owner_id, _, _ = await _a_shop()
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "kind": "close",
              "counted": 40000.0, "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_an_unknown_kind_is_refused_by_the_schema(client, bot_headers):
    owner_id, _, _ = await _a_shop()
    worker, telegram_id = await _worker(owner_id)
    await _open(worker, "idem-open-1")

    response = await client.post(
        f"{BASE}/shift/till",
        json={"telegram_id": telegram_id, "kind": "sideways",
              "counted": "1.00", "idempotency_key": "idem-till-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- what the owner sees ------------------------------------------------------

async def test_both_counts_show_on_the_report(client):
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await till_service.declare(worker, "open", Decimal("0"), "idem-till-1")
    await till_service.declare(worker, "close", Decimal("3200"), "idem-till-2")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Դրամարկղի հաշվարկ" in page.text
    assert "3,200.00" in page.text
    assert "-300.00" in page.text, "the difference, spelled out"


async def test_the_session_rows_carry_the_difference(client):
    owner_id, _, item_id = await _a_shop()
    worker, _ = await _worker(owner_id)
    await _open(worker, "idem-open-1")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    session_id = await _session()
    await till_service.declare(worker, "close", Decimal("3200"), "idem-till-1")

    rows = await till_repo.for_session(session_id)

    assert len(rows) == 1
    assert rows[0]["difference"] == Decimal("-300.00")
    assert rows[0]["worker_name"] == "Անի"
