"""A sale that went out for delivery.

It changes nothing about the money — same price, same payment method, same stock
movement — so every test here is really the same assertion twice: that the flag
is carried all the way to the owner's screen, and that carrying it moved nothing
it should not have.

A flag rather than a third payment method, because it is orthogonal to one. A
delivery can be paid in cash at the door or by card in advance, and folding the
two together would make "how much did we take in cash" unanswerable.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.config import settings
from app.db import db
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import statistics
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


async def _open_shift():
    """An owner with a store, a worker on shift, and something to sell."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=50,
                              self_price="1500.00", sell_price="3500.00")
    return owner_id, store_id, worker, item_id, telegram_id


# -- recording it -------------------------------------------------------------

async def test_a_sale_can_be_marked_as_a_delivery(client):
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1",
        is_delivery=True,
    )

    assert result["sale"]["is_delivery"] is True
    assert await db.fetchval("SELECT is_delivery FROM sales") is True


async def test_an_ordinary_sale_is_not_one(client):
    """The default has to be false, or every shop that never delivers would
    report that all of its trade went out of the door."""
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    assert result["sale"]["is_delivery"] is False
    assert await db.fetchval("SELECT is_delivery FROM sales") is False


async def test_it_changes_no_money_and_no_stock(client):
    """The whole point: the flag records where the goods went, nothing else."""
    _, _, worker, item_id, _ = await _open_shift()

    plain = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    delivered = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-2",
        is_delivery=True,
    )

    assert delivered["sale"]["total"] == plain["sale"]["total"]
    assert delivered["sale"]["payment_method"] == plain["sale"]["payment_method"]
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 46
    assert (await db.fetchval(
        "SELECT coalesce(sum(amount), 0) FROM cash_movements WHERE kind = 'sale'"
    )) == Decimal("14000.00")


async def test_a_delivery_can_be_paid_either_way(client):
    """Which is why it is a flag and not a payment method of its own."""
    _, _, worker, item_id, _ = await _open_shift()

    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1",
        is_delivery=True,
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", "idem-sale-2",
        is_delivery=True,
    )

    rows = await db.fetch("SELECT payment_method FROM sales WHERE is_delivery ORDER BY id")
    assert [row["payment_method"] for row in rows] == ["cash", "card"]


async def test_voiding_a_delivery_still_works(client):
    """It was a sale like any other, so undoing it is too. The regression: the
    void payload read a column the locking query had not selected."""
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1",
        is_delivery=True,
    )

    result = await sales_service.void_last_sale(worker)

    assert result["voided"]["is_delivery"] is True
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 50


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_takes_the_flag(client, bot_headers):
    _, _, _, item_id, telegram_id = await _open_shift()

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id,
              "items": [{"item_id": item_id, "quantity": 1}],
              "payment_method": "cash", "is_delivery": True,
              "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    assert response.json()["sale"]["is_delivery"] is True


async def test_the_endpoint_defaults_it_to_false(client, bot_headers):
    """An older bot build that does not send the field must still sell."""
    _, _, _, item_id, telegram_id = await _open_shift()

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id,
              "items": [{"item_id": item_id, "quantity": 1}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    assert response.json()["sale"]["is_delivery"] is False


async def test_a_write_up_line_can_be_a_delivery(client, bot_headers):
    """The end-of-shift write-up records the day one line at a time, and a
    delivery entered that way is the same fact as one entered live."""
    _, _, _, item_id, telegram_id = await _open_shift()

    response = await client.post(
        f"{BASE}/shift/close-out",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-key-close-01",
              "lines": [
                  {"item_id": item_id, "quantity": 1, "unit_price": "3500.00",
                   "payment_method": "cash", "is_delivery": True},
                  {"item_id": item_id, "quantity": 1, "unit_price": "3500.00",
                   "payment_method": "cash"},
              ]},
        headers=bot_headers,
    )

    assert response.status_code == 200, response.text
    flags = await db.fetch("SELECT is_delivery FROM sales ORDER BY id")
    assert [row["is_delivery"] for row in flags] == [True, False]


# -- what the owner sees ------------------------------------------------------

async def test_the_receipt_carries_it_to_the_report(client):
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1",
        is_delivery=True,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    receipts = await sales_repo.receipts_in_store_session(session_id)

    assert [row["is_delivery"] for row in receipts] == [True]


async def test_the_report_page_shows_it(client):
    owner_id, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1",
        is_delivery=True,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "առաքում" in page.text


async def test_the_statistics_split_deliveries_out(client):
    """Same money, different door. The split is not recoverable from anything
    else in the row, which is the reason it has a figure of its own."""
    owner_id, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1",
        is_delivery=True,
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-2"
    )
    today = settings.local_day()

    overview = await statistics.overview(owner_id, today - timedelta(days=6), today)

    assert Decimal(overview["summary"]["delivered"]) == Decimal("3500.00")
    assert overview["summary"]["deliveries"] == 1
    assert Decimal(overview["summary"]["revenue"]) == Decimal("10500.00"), (
        "the delivery is part of the takings, not instead of them"
    )


# -- whose sale is it, though ------------------------------------------------
#
# A delivery is money the shop took, not something this worker sold over the
# counter. Nobody stood there and sold it; somebody entered an order that arrived
# by phone. So the two are kept apart everywhere the *worker* is the subject —
# their own figures, their write-up, their bonus — and added together everywhere
# the *shop* is.

async def _a_counter_sale_and_a_delivery(worker, item_id):
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-counter-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 4}], "card", "idem-delivery-1",
        is_delivery=True,
    )


async def test_the_workers_own_total_leaves_deliveries_out(client):
    _, _, worker, item_id, _ = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)
    shift_id = await db.fetchval("SELECT id FROM work_sessions")

    sold = await sales_repo.summary_for_work_session(shift_id)

    assert sold["total"] == Decimal("7000.00"), "two at 3,500, sold at the counter"
    assert sold["receipts"] == 1
    assert sold["delivery_total"] == Decimal("14000.00"), "four at 3,500, delivered"
    assert sold["delivery_receipts"] == 1


async def test_each_half_keeps_its_own_cash_and_card(client):
    """A delivery is paid at the door or in advance, so the split is a real
    question about each of them separately."""
    _, _, worker, item_id, _ = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)
    shift_id = await db.fetchval("SELECT id FROM work_sessions")

    sold = await sales_repo.summary_for_work_session(shift_id)

    assert (sold["cash_total"], sold["card_total"]) == (Decimal("7000.00"), Decimal("0"))
    assert (sold["delivery_cash"], sold["delivery_card"]) == (
        Decimal("0"), Decimal("14000.00")
    )


async def test_the_bot_shows_the_two_apart(client, bot_headers):
    """The write-up is what a cashier checks their day against. Running the two
    together told them they had sold six of something when they handed over two."""
    _, _, worker, item_id, telegram_id = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)

    review = await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id},
        headers=bot_headers,
    )
    body = review.json()

    assert [row["quantity"] for row in body["sold"]] == [2]
    assert [row["quantity"] for row in body["delivered"]] == [4]
    assert body["totals"]["total"] == "7000.00"
    assert body["delivery_totals"]["total"] == "14000.00"


async def test_the_status_screen_keeps_them_apart_too(client, bot_headers):
    _, _, worker, item_id, telegram_id = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)

    me = await client.get(
        f"{BASE}/me", params={"telegram_id": telegram_id}, headers=bot_headers
    )
    session = me.json()["session"]

    assert session["sales"]["total"] == "7000.00"
    assert session["deliveries"]["total"] == "14000.00"


async def test_a_delivery_does_not_earn_a_bonus(client):
    """A bonus rewards selling. An order that arrived by phone and was typed in is
    money the shop took without anybody selling anything at the counter — and the
    bonus is paid out of the same till the shift is settled from."""
    owner_id, _, worker, item_id, _ = await _open_shift()
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 10000, bonus_amount = 2000,
               bonus_period = 'day' WHERE id = $1
        """,
        worker.id,
    )
    # 14,000 of deliveries, comfortably past a 10,000 target, and 7,000 over the
    # counter, comfortably short of it.
    await _a_counter_sale_and_a_delivery(worker, item_id)

    await shifts_service.close_out_shift(worker, [], "idem-close-1", close_store_too=True)

    # Null rather than zero: the column is only written when a bonus is earned.
    assert await db.fetchval(
        "SELECT coalesce(bonus_paid, 0) FROM work_sessions"
    ) == Decimal("0")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'bonus'"
    ) == 0


async def test_the_counter_alone_still_earns_one(client):
    """The other half of the rule: nothing about deliveries makes a bonus harder
    to earn on sales the worker did make."""
    owner_id, _, worker, item_id, _ = await _open_shift()
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 5000, bonus_amount = 2000,
               bonus_period = 'day' WHERE id = $1
        """,
        worker.id,
    )
    await _a_counter_sale_and_a_delivery(worker, item_id)

    await shifts_service.close_out_shift(worker, [], "idem-close-1", close_store_too=True)

    assert await db.fetchval("SELECT bonus_paid FROM work_sessions") == Decimal("2000.00")


async def test_the_shop_still_counts_all_of_it(client):
    """The owner's total is unchanged. Only the question «what did this worker
    sell» has a different answer than it used to."""
    _, _, worker, item_id, _ = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    totals = await money_repo.totals_for_session(session_id)

    assert totals["net_sales"] == Decimal("21000.00"), "the shop took all of it"
    assert totals["counter_sales"] == Decimal("7000.00")
    assert totals["delivery_sales"] == Decimal("14000.00")
    assert totals["counter_sales"] + totals["delivery_sales"] == totals["net_sales"]


async def test_a_voided_delivery_leaves_the_delivery_half(client):
    """The reversing ledger row carries the id of the sale it reverses, so it nets
    out in the half that sale landed in rather than against the counter."""
    _, _, worker, item_id, _ = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    delivery_id = await db.fetchval("SELECT id FROM sales WHERE is_delivery")
    await sales_service.void_last_sale(worker, "սխալ", sale_id=delivery_id)

    totals = await money_repo.totals_for_session(session_id)

    assert totals["delivery_sales"] == Decimal("0")
    assert totals["counter_sales"] == Decimal("7000.00")
    assert totals["net_sales"] == Decimal("7000.00")


async def test_the_report_says_which_half_is_which(client):
    _, _, worker, item_id, _ = await _open_shift()
    await _a_counter_sale_and_a_delivery(worker, item_id)
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Վաճառքից՝ խանութում" in page.text
    assert "21,000.00" in page.text, "the shop's total is unchanged"
    assert "7,000.00" in page.text and "14,000.00" in page.text
