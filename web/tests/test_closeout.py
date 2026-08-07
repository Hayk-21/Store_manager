"""Writing the day up at the end of a shift.

The cashier serves customers without touching the bot and declares everything
once, at the end. Either the whole declaration lands with the shift closed and
the salary paid, or none of it does.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from tests.factories import YEREVAN_LAT, YEREVAN_LNG, make_item, make_owner, make_store, make_worker

BASE = "/api/bot/v1"
TG = 555000777


async def _on_shift(stock: int = 20, salary: str = "8000.00"):
    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    _, telegram_id = await make_worker(
        owner_id, "Անի", telegram_id=TG, salary_amount=salary
    )
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=stock,
                              self_price="1500.00", sell_price="3500.00")
    other = await make_item(owner_id, store_id, "Elf Bar", count=stock, sell_price="4000.00")
    return owner_id, store_id, telegram_id, item_id, other


async def _open(client, headers, telegram_id):
    return await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-01", "live_period": 900},
        headers=headers,
    )


async def _close_out(client, headers, telegram_id, lines, key="idem-key-close-01", **extra):
    return await client.post(
        f"{BASE}/shift/close-out",
        json={"telegram_id": telegram_id, "lines": lines, "idempotency_key": key, **extra},
        headers=headers,
    )


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
    owner_id, store_id, tg, item_id, _ = await _on_shift()
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="6000.00")
    await _open(client, bot_headers, tg)
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": second_tg, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "idem-key-open-02", "live_period": 900},
        headers=bot_headers,
    )
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
