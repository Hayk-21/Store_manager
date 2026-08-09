"""What the bot is given to read a shift back before it ends.

``GET /shift/review``. It used to be the sales alone, which is not a review of a
shift: a cashier who wrote off a broken pod, put eight units back on the shelf and
took 500 for a delivery driver saw none of it, and this is the last moment any of it
can be corrected without going to the owner.

The drawer is here too. Without it the count the worker is asked for a few messages
later is a guess, and a guess is the one thing a count must not be.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.services import money as money_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import stock as stock_service
from app.services import transfers as transfers_service
from app.services import write_offs as write_offs_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    make_item,
    make_owner,
    make_store,
    make_worker,
    worked_a_full_shift,
)

BASE = "/api/bot/v1"


async def _on_shift(float_: str = "1000.00", salary: str = "7000.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Նուբարաշեն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute(
        "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(float_)
    )
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3276.00",
    )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount=salary)
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900
    )
    return worker, telegram_id, item_id, store_id


async def _review(client, bot_headers, telegram_id: int) -> dict:
    response = await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id},
        headers=bot_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_quiet_shift_reports_everything_empty(client, bot_headers):
    worker, telegram_id, _, _ = await _on_shift()

    review = await _review(client, bot_headers, telegram_id)

    assert review["sold"] == []
    assert review["written_off"] == []
    assert review["stock_fixed"] == []
    assert review["taken_out"] == []
    assert review["transfers"] == []


async def test_the_sales_already_rung_up_are_listed(client, bot_headers):
    worker, telegram_id, item_id, _ = await _on_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "card", "idem-sale-1"
    )

    review = await _review(client, bot_headers, telegram_id)

    assert review["sold"] == [
        {"name": "HQD Cuvie", "quantity": 2, "total": "6552.00"}
    ]
    assert review["totals"]["receipts"] == 1
    assert review["totals"]["card"] == "6552.00"


async def test_breakage_is_listed_with_its_reason(client, bot_headers):
    worker, telegram_id, item_id, _ = await _on_shift()
    await write_offs_service.record(
        worker, item_id, 1, "ընկավ", "idem-defect-1"
    )

    review = await _review(client, bot_headers, telegram_id)

    assert review["written_off"] == [
        {"name": "HQD Cuvie", "quantity": 1, "reason": "ընկավ"}
    ]


async def test_breakage_never_carries_its_cost(client, bot_headers):
    """What the shop paid for a vape is the owner's business. This payload is read
    aloud to the cashier, so the figure must not be in it to leak."""
    worker, telegram_id, item_id, _ = await _on_shift()
    await write_offs_service.record(worker, item_id, 1, "ընկավ", "idem-defect-1")

    review = await _review(client, bot_headers, telegram_id)

    assert "unit_cost" not in review["written_off"][0]
    assert "1500" not in str(review["written_off"])


async def test_shelf_corrections_are_listed_with_the_count_they_left(client, bot_headers):
    worker, telegram_id, item_id, _ = await _on_shift()
    await stock_service.adjust_by_worker(
        worker, [{"item_id": item_id, "delta": 8}], "idem-adjust-1", None
    )

    review = await _review(client, bot_headers, telegram_id)

    assert review["stock_fixed"] == [
        {"name": "HQD Cuvie", "delta": 8, "count_after": 58}
    ]


async def test_cash_taken_out_is_listed_with_what_it_was_for(client, bot_headers):
    worker, telegram_id, item_id, _ = await _on_shift(float_="5000.00")
    await money_service.withdraw_by_worker(
        worker, Decimal("500"), "առաքիչին", "idem-cash-1"
    )

    review = await _review(client, bot_headers, telegram_id)

    assert review["taken_out"] == [{"amount": "500.00", "purpose": "առաքիչին"}]


async def test_stock_that_arrived_from_another_shop_is_reported(client, bot_headers):
    """Decided by somebody at the other shop rather than by this worker, and still
    theirs to check: it changed the shelf they are about to be held to."""
    receiver, receiver_tg, _, receiving_store = await _on_shift()
    other_store = await make_store(
        receiver.owner_id, "Կենտրոն", lat=YEREVAN_LAT, lng=YEREVAN_LNG
    )
    their_item = await make_item(receiver.owner_id, other_store, "Elf Bar", count=20)
    sender_id, _ = await make_worker(receiver.owner_id, "Գոռ", salary_amount="0.00")
    sender = shifts_service.Worker(
        id=sender_id, owner_id=receiver.owner_id, name="Գոռ",
        salary_amount=Decimal("0.00"),
    )
    await db.execute("UPDATE stores SET lat = null, lng = null WHERE id = $1", receiving_store)
    await shifts_service.open_store(
        sender, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900
    )
    await db.execute(
        "UPDATE stores SET lat = $2, lng = $3 WHERE id = $1",
        receiving_store, YEREVAN_LAT, YEREVAN_LNG,
    )
    asked = await transfers_service.request_by_worker(
        receiver, other_store, their_item, 8, "idem-transfer-1"
    )
    await transfers_service.decide_by_worker(sender, asked["transfer"]["id"], True)

    review = await _review(client, bot_headers, receiver_tg)

    assert review["transfers"] == [
        {"name": "Elf Bar", "quantity": 8, "incoming": True, "other_store": "Կենտրոն"}
    ]


async def test_the_drawer_is_reported_as_it_stands(client, bot_headers):
    worker, telegram_id, item_id, _ = await _on_shift(float_="40000.00")
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    review = await _review(client, bot_headers, telegram_id)

    assert review["till"]["cash"] == "46552.00"
    assert review["store_float"] == "40000.00", "and what the shop keeps"


async def test_the_wage_is_worked_out_here_and_not_on_the_phone(client, bot_headers):
    """A shift under eight hours pays half, and that rule lives on this side. A bot
    reimplementing it would be a second answer to the same question."""
    worker, telegram_id, _, _ = await _on_shift(salary="7000.00")

    review = await _review(client, bot_headers, telegram_id)

    assert review["salary"] == {
        "due": "3500.00", "full": "7000.00", "full_shift_hours": 8
    }


async def test_a_full_shift_is_due_the_whole_wage(client, bot_headers):
    worker, telegram_id, _, _ = await _on_shift(salary="7000.00")
    await worked_a_full_shift(worker.id)

    review = await _review(client, bot_headers, telegram_id)

    assert review["salary"]["due"] == "7000.00"


async def test_a_worker_not_on_shift_is_refused(client, bot_headers):
    owner_id = await make_owner("@ownerhandle")
    _, telegram_id = await make_worker(owner_id, "Անի")

    response = await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id},
        headers=bot_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_open_session"


async def test_one_workers_review_is_their_own_shift(client, bot_headers):
    """Two people share a drawer but not a shift. The sales are theirs; the till is
    the shop's, which is why one is per-shift and the other is not."""
    first, first_tg, item_id, _ = await _on_shift(float_="1000.00")
    second_id, second_tg = await make_worker(
        first.owner_id, "Գոռ", salary_amount="7000.00"
    )
    second = shifts_service.Worker(
        id=second_id, owner_id=first.owner_id, name="Գոռ",
        salary_amount=Decimal("7000.00"),
    )
    await shifts_service.open_store(
        second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-2", 900
    )
    await sales_service.record_sale(
        first, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )

    theirs = await _review(client, bot_headers, second_tg)

    assert theirs["sold"] == [], "not their sale"
    assert theirs["till"]["cash"] == "7552.00", "but the same drawer"
