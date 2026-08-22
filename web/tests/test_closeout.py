"""Writing the day up at the end of a shift.

The cashier serves customers without touching the bot and declares everything
once, at the end. Either the whole declaration lands with the shift closed and
the salary paid, or none of it does.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
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
TG = 555000777


async def _on_shift(stock: int = 20, salary: str = "8000.00", till: str = "0.00"):
    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    if Decimal(till) > 0:
        # The drawer pays a wage as far as it reaches and the rest is owed, so a test
        # that expects one paid in cash has to put the cash there.
        await db.execute(
            "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(till)
        )
    _, telegram_id = await make_worker(
        owner_id, "Անի", telegram_id=TG, salary_amount=salary
    )
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=stock,
                              self_price="1500.00", sell_price="3500.00")
    other = await make_item(owner_id, store_id, "Elf Bar", count=stock, sell_price="4000.00")
    return owner_id, store_id, telegram_id, item_id, other


async def _open(client, headers, telegram_id):
    """Open a shift and treat it as a full day's work.

    A shift under eight hours is paid half; nothing here is about that rule, and
    every wage assertion below would otherwise have to know it.
    """
    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-01", "live_period": 900},
        headers=headers,
    )
    await worked_a_full_shift()
    return response


async def _close_out(
    client, headers, telegram_id, lines, key="idem-key-close-01", counted="0", **extra
):
    """Write the day up. ``counted`` is what is being left in the drawer.

    Closing is refused without it once this shift is the one shutting the shop, so
    the default is there to keep every test that is about something else out of that
    conversation. ``counted=None`` leaves the field off, which is what the bot's
    first attempt does.
    """
    body = {"telegram_id": telegram_id, "lines": lines, "idempotency_key": key, **extra}
    if counted is not None:
        body["counted"] = counted
    return await client.post(f"{BASE}/shift/close-out", json=body, headers=headers)


# -- what the worker sees before they write anything up -----------------------

async def test_the_shift_lists_what_is_already_recorded(client, bot_headers):
    """Shown on the way into the write-up. Two ways a sale reaches the books — rung
    up as it happened, or declared now — and the write-up is only for the second.
    Without this list the only thing telling them apart is memory, and a product
    declared twice comes off the shelf twice."""
    _, _, telegram_id, item_id, other = await _on_shift()
    await _open(client, bot_headers, telegram_id)
    for key, item, qty in (("s1", item_id, 2), ("s2", item_id, 1), ("s3", other, 3)):
        await client.post(
            f"{BASE}/sale",
            json={"telegram_id": telegram_id, "items": [{"item_id": item, "quantity": qty}],
                  "payment_method": "cash", "idempotency_key": f"idem-key-sale-{key}"},
            headers=bot_headers,
        )

    body = (await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id}, headers=bot_headers
    )).json()

    # Rolled up per product, not per receipt: the worker is checking their own day
    # against what they remember selling, and "three HQD Cuvie" is that.
    sold = {row["name"]: row for row in body["sold"]}
    assert sold["HQD Cuvie"]["quantity"] == 3
    assert sold["HQD Cuvie"]["total"] == "10500.00"
    assert sold["Elf Bar"]["quantity"] == 3
    assert body["totals"]["receipts"] == 3


async def test_a_voided_sale_is_not_in_the_list(client, bot_headers):
    """It was taken back. Showing it would invite the worker to declare it again."""
    _, _, telegram_id, item_id, _ = await _on_shift()
    await _open(client, bot_headers, telegram_id)
    await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 2}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )
    await client.post(
        f"{BASE}/sale/void", json={"telegram_id": telegram_id}, headers=bot_headers
    )

    body = (await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id}, headers=bot_headers
    )).json()

    assert body["sold"] == []


async def test_a_quiet_shift_lists_nothing(client, bot_headers):
    _, _, telegram_id, _, _ = await _on_shift()
    await _open(client, bot_headers, telegram_id)

    body = (await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id}, headers=bot_headers
    )).json()

    assert body["sold"] == []
    assert body["totals"]["total"] == "0.00"


async def test_the_list_needs_an_open_shift(client, bot_headers):
    _, _, telegram_id, _, _ = await _on_shift()

    response = await client.get(
        f"{BASE}/shift/review", params={"telegram_id": telegram_id}, headers=bot_headers
    )

    assert response.status_code == 409


async def test_one_workers_sales_are_not_in_anothers_list(client, bot_headers):
    """Each cashier checks their own day. A shared shift would otherwise have both
    of them declaring the same receipts."""
    owner_id, _, first_tg, item_id, _ = await _on_shift()
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="0.00")
    await _open(client, bot_headers, first_tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )
    await client.post(
        f"{BASE}/sale",
        json={"telegram_id": first_tg, "items": [{"item_id": item_id, "quantity": 2}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    theirs = (await client.get(
        f"{BASE}/shift/review", params={"telegram_id": second_tg}, headers=bot_headers
    )).json()

    assert theirs["sold"] == []


# -- the happy path ----------------------------------------------------------

async def test_declaring_the_day_moves_stock_money_and_closes_the_shift(client, bot_headers):
    _, _, tg, item_id, _ = await _on_shift(stock=20)
    await _open(client, bot_headers, tg)

    response = await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 3, "unit_price": "3500.00", "payment_method": "cash"},
        {"item_id": item_id, "quantity": 1, "unit_price": "3000.00", "payment_method": "card"},
    ])

    assert response.status_code == 200
    summary = response.json()["summary"]
    assert summary["sales"]["cash_total"] == "10500.00"
    assert summary["sales"]["card_total"] == "3000.00"
    assert summary["salary_deducted"] == "8000.00"
    assert summary["store_closed"] is True

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 16
    # 13500 taken in, 8000 paid out.
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("2500.00")


async def test_a_line_may_be_sold_at_any_price_the_customer_paid(client, bot_headers):
    """The reason the price is typed rather than assumed."""
    _, _, tg, item_id, _ = await _on_shift()
    await _open(client, bot_headers, tg)

    await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 1, "unit_price": "2000.00", "payment_method": "cash"},
    ])

    line = await db.fetchrow("SELECT unit_price, unit_cost, line_total FROM sale_items")
    assert line["unit_price"] == Decimal("2000.00"), "the discounted price, not the shelf one"
    assert line["unit_cost"] == Decimal("1500.00"), "cost still snapshot from the item"
    assert line["line_total"] == Decimal("2000.00")


async def test_omitting_the_price_falls_back_to_the_shelf(client, bot_headers):
    _, _, tg, item_id, _ = await _on_shift()
    await _open(client, bot_headers, tg)

    await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 2, "payment_method": "cash"},
    ])

    assert await db.fetchval("SELECT unit_price FROM sale_items") == Decimal("3500.00")


async def test_the_same_item_can_appear_twice_at_different_prices(client, bot_headers):
    """Morning at full price, afternoon discounted — one day, two lines."""
    _, _, tg, item_id, _ = await _on_shift(stock=10)
    await _open(client, bot_headers, tg)

    await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 2, "unit_price": "3500.00", "payment_method": "cash"},
        {"item_id": item_id, "quantity": 1, "unit_price": "2500.00", "payment_method": "card"},
    ])

    assert await db.fetchval("SELECT count(*) FROM sales") == 2
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 7


async def test_a_quiet_day_still_closes_the_shift(client, bot_headers):
    _, _, tg, _, _ = await _on_shift()
    await _open(client, bot_headers, tg)

    response = await _close_out(client, bot_headers, tg, [])

    assert response.status_code == 200
    assert response.json()["summary"]["sales"]["receipts"] == 0
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0
    # The salary is still owed for turning up.
    assert await db.fetchval("SELECT salary_paid FROM work_sessions") == Decimal("8000.00")


# -- all or nothing ----------------------------------------------------------

async def test_one_bad_line_rolls_back_the_whole_declaration(client, bot_headers):
    """A half-applied close-out would leave stock moved against a shift that is
    still open, which nothing downstream could make sense of."""
    _, _, tg, item_id, other = await _on_shift(stock=5)
    await _open(client, bot_headers, tg)

    response = await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 2, "payment_method": "cash"},
        {"item_id": other, "quantity": 99, "payment_method": "cash"},   # too many
    ])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_stock"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 5
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 1


async def test_an_item_from_another_owner_is_refused(client, bot_headers):
    _, _, tg, _, _ = await _on_shift()
    await _open(client, bot_headers, tg)
    other_owner = await make_owner()
    stranger = await make_item(other_owner, await make_store(other_owner), count=50)

    response = await _close_out(client, bot_headers, tg, [
        {"item_id": stranger, "quantity": 1, "payment_method": "cash"},
    ])

    assert response.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 1


async def test_closing_out_without_a_shift_is_refused(client, bot_headers):
    _, _, tg, item_id, _ = await _on_shift()

    response = await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 1, "payment_method": "cash"},
    ])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_open_session"


# -- idempotency -------------------------------------------------------------

async def test_a_replayed_close_out_does_not_sell_the_day_twice(client, bot_headers):
    """The cashier taps confirm, the connection drops, the bot retries."""
    _, _, tg, item_id, _ = await _on_shift(stock=10)
    await _open(client, bot_headers, tg)
    lines = [{"item_id": item_id, "quantity": 3, "payment_method": "cash"}]

    first = await _close_out(client, bot_headers, tg, lines)
    second = await _close_out(client, bot_headers, tg, lines)

    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True
    assert await db.fetchval("SELECT count(*) FROM sales") == 1
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 7


async def test_money_must_arrive_as_a_string_not_a_float(client, bot_headers):
    _, _, tg, item_id, _ = await _on_shift()
    await _open(client, bot_headers, tg)

    response = await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 1, "unit_price": 3500.55, "payment_method": "cash"},
    ])

    assert response.status_code == 422


# -- closing the store -------------------------------------------------------

async def test_the_last_worker_out_closes_the_store(client, bot_headers):
    _, _, tg, item_id, _ = await _on_shift()
    await _open(client, bot_headers, tg)

    body = (await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 1, "payment_method": "cash"},
    ])).json()

    assert body["summary"]["store_closed"] is True
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 0


async def test_asking_to_close_the_store_is_refused_while_a_colleague_works(
    client, bot_headers
):
    """Their close-out is the only record of what they sold, so ending their
    shift for them would throw that day's takings away."""
    owner_id, store_id, tg, item_id, _ = await _on_shift()
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="6000.00")
    await _open(client, bot_headers, tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )

    response = await _close_out(
        client, bot_headers, tg,
        [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}],
        close_store=True,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "others_on_shift"
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 2
    assert await db.fetchval("SELECT count(*) FROM sales") == 0, "nothing was recorded either"


async def test_the_last_one_out_closing_the_store_pays_everybody(client, bot_headers):
    owner_id, store_id, tg, item_id, _ = await _on_shift(till="50000.00")
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="6000.00")
    await _open(client, bot_headers, tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )
    # The second worker opened after _open backdated the first, so they need it
    # too — both wages below are the full figures.
    await worked_a_full_shift()
    await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": second_tg, "idempotency_key": "idem-key-end-02"},
        headers=bot_headers,
    )

    body = (await _close_out(
        client, bot_headers, tg,
        [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}],
        close_store=True,
    )).json()

    assert body["summary"]["store_closed"] is True
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0
    paid = await db.fetchval("SELECT -sum(amount) FROM cash_movements WHERE kind = 'salary'")
    assert paid == Decimal("14000.00"), "both workers were paid"


async def test_a_colleague_still_working_keeps_the_store_open(client, bot_headers):
    owner_id, _, tg, item_id, _ = await _on_shift()
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="0.00")
    await _open(client, bot_headers, tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )

    body = (await _close_out(client, bot_headers, tg, [
        {"item_id": item_id, "quantity": 1, "payment_method": "cash"},
    ])).json()

    assert body["summary"]["store_closed"] is False
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 1


# -- the drawer, which is what shuts the shop ---------------------------------

async def test_the_shop_does_not_shut_until_the_drawer_is_counted(client, bot_headers):
    """The order the whole thing turns on.

    Counting used to be offered afterwards, as a button on the message saying the
    shift had ended — and a button handed to somebody who has just been told they can
    go home is a button most people do not press. The reading went missing on the
    evenings it mattered and the next shift opened against a float nobody had counted.
    So the close-out is refused without it.
    """
    _, _, telegram_id, item_id, _ = await _on_shift()
    await _open(client, bot_headers, telegram_id)

    response = await _close_out(
        client, bot_headers, telegram_id,
        [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}],
        counted=None,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "till_count_required"


async def test_the_refused_close_out_writes_nothing_at_all(client, bot_headers):
    """The refusal comes from inside the transaction, after the sale has been applied
    and the wage paid, so the whole thing has to come back out. A shift left open with
    its stock already moved is the one state nothing downstream could make sense of."""
    _, _, telegram_id, item_id, _ = await _on_shift(salary="8000.00", till="20000.00")
    await _open(client, bot_headers, telegram_id)

    await _close_out(
        client, bot_headers, telegram_id,
        [{"item_id": item_id, "quantity": 3, "payment_method": "cash"}],
        counted=None,
    )

    assert await db.fetchval("SELECT count FROM items") == 20, "nothing came off the shelf"
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 1, "and the shift is still open, to be closed properly"
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'salary'"
    ) == 0, "no wage paid out of a shift that did not end"
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 0


async def test_the_same_call_again_with_the_figure_goes_through(client, bot_headers):
    """What the bot does with the refusal: it asks the worker, and sends the same
    close-out again under the same key."""
    _, store_id, telegram_id, item_id, _ = await _on_shift(salary="0.00")
    await _open(client, bot_headers, telegram_id)
    lines = [{"item_id": item_id, "quantity": 2, "payment_method": "cash"}]
    await _close_out(client, bot_headers, telegram_id, lines, counted=None)

    body = (await _close_out(
        client, bot_headers, telegram_id, lines, counted="3000"
    )).json()

    assert body["summary"]["store_closed"] is True
    assert body["till_count"]["counted"] == "3000.00"
    assert body["till_count"]["expected"] == "7000.00", "two sold at 3,500"
    assert body["till_count"]["handed_over"] == "4000.00", "and the rest goes to the owner"
    assert await db.fetchval(
        "SELECT till_balance FROM stores WHERE id = $1", store_id
    ) == Decimal("3000.00"), "which is what tomorrow opens with"


async def test_the_reading_is_taken_after_the_wage_is_out_of_the_drawer(client, bot_headers):
    """The ordering the counting rules were built for, now that both happen in one
    transaction. A worker paid 8,000 as they close cannot also hand that 8,000 to the
    owner — the till the reading is measured against is the one the wage has left."""
    _, _, telegram_id, item_id, _ = await _on_shift(salary="8000.00", till="20000.00")
    await _open(client, bot_headers, telegram_id)

    body = (await _close_out(
        client, bot_headers, telegram_id,
        [{"item_id": item_id, "quantity": 2, "payment_method": "cash"}],
        counted="5000",
    )).json()

    # 20,000 carried in + 7,000 sold − an 8,000 wage.
    assert body["till_count"]["expected"] == "19000.00"
    assert body["till_count"]["handed_over"] == "14000.00"


async def test_a_colleague_still_working_is_never_asked_for_the_drawer(client, bot_headers):
    """The drawer belongs to the shop, not to a shift. One of two cashiers going home
    at six cannot settle the change the evening still needs, so nothing is asked of
    them and nothing is written down."""
    owner_id, _, tg, item_id, _ = await _on_shift()
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="0.00")
    await _open(client, bot_headers, tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )

    body = (await _close_out(
        client, bot_headers, tg,
        [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}],
        counted=None,
    )).json()

    assert body["summary"]["store_closed"] is False
    assert "till_count" not in body
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 0


async def test_a_replayed_close_out_counts_the_drawer_once(client, bot_headers):
    """A flaky connection at the very end must not write a second reading — and the
    replay has to answer with the one it did write, or the bot reports the evening
    without the figure the worker just gave it."""
    _, _, telegram_id, item_id, _ = await _on_shift(salary="0.00")
    await _open(client, bot_headers, telegram_id)
    lines = [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}]
    first = (await _close_out(client, bot_headers, telegram_id, lines, counted="1000")).json()

    again = (await _close_out(client, bot_headers, telegram_id, lines, counted="1000")).json()

    assert again["duplicate"] is True
    assert again["till_count"] == first["till_count"]
    assert await db.fetchval("SELECT count(*) FROM till_counts") == 1


async def test_more_than_the_drawer_holds_still_goes_through(client, bot_headers):
    """A count is a reading of a real drawer, and the drawer can be ahead of the books —
    a sale entered late, change put back. Refusing it left somebody locking up unable to
    shut the shop over a gap they could not fix from the door, so the figure is taken and
    the gap is kept beside it."""
    _, _, telegram_id, item_id, _ = await _on_shift(salary="0.00", till="2000.00")
    await _open(client, bot_headers, telegram_id)

    response = await _close_out(
        client, bot_headers, telegram_id,
        [{"item_id": item_id, "quantity": 2, "payment_method": "cash"}],
        counted="12000",
    )

    assert response.status_code == 200
    count = response.json()["till_count"]
    assert count["counted"] == "12000.00"
    assert count["expected"] == "9000.00", "2,000 carried in and 7,000 sold"
    assert count["handed_over"] == "0.00", "the owner is owed nothing, never a negative"


async def test_a_figure_the_till_could_never_hold_is_refused(client, bot_headers):
    """And the close-out with it: a mistyped nought must not shut the shop with a
    float of ten million behind it."""
    _, _, telegram_id, item_id, _ = await _on_shift()
    await _open(client, bot_headers, telegram_id)

    response = await _close_out(
        client, bot_headers, telegram_id,
        [{"item_id": item_id, "quantity": 1, "payment_method": "cash"}],
        counted="99999999999",
    )

    assert response.status_code == 422
    assert await db.fetchval(
        "SELECT count(*) FROM work_sessions WHERE ended_at IS NULL"
    ) == 1
