"""The HTTP contract the bot speaks.

The services are tested directly elsewhere; what is checked here is the envelope,
the status codes, and the fact that a worker's telegram_id is the only thing
resolving a request to an owner.
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

FAR_LAT = YEREVAN_LAT + 0.0072
BASE = "/api/bot/v1"


async def _world(salary: str = "8000.00", stock: int = 10, till: str = "0.00"):
    """A shop and somebody who works there.

    ``till`` is the shop's own float, opt-in because most of these tests assert an
    exact cash figure and a float would move all of them. It matters for the wage: the
    drawer pays as far as it reaches and the rest is owed, so a shop with nothing in it
    settles a shift without paying any cash. That behaviour has its own file.
    """
    owner_id = await make_owner()
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG,
                                radius_m=120)
    if Decimal(till) > 0:
        await db.execute(
            "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(till)
        )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount=salary)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=stock,
                              self_price="1500.00", sell_price="3500.00")
    return owner_id, store_id, worker_id, telegram_id, item_id


async def _open(client, headers, telegram_id, key="idem-key-open-01", live_period=900):
    """Opening a shift needs a live location; 900s is Telegram's shortest span.

    The shift is then backdated to a full day, so a wage in a later assertion is the
    whole figure. A shift under eight hours is paid half, and nothing in this file is
    about that rule.
    """
    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": key, "live_period": live_period},
        headers=headers,
    )
    await worked_a_full_shift()
    return response


# -- authentication ----------------------------------------------------------

async def test_no_secret_is_rejected(client):
    _, _, _, telegram_id, _ = await _world()

    response = await client.get(f"{BASE}/me", params={"telegram_id": telegram_id})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_a_wrong_secret_is_rejected(client):
    _, _, _, telegram_id, _ = await _world()

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": telegram_id}, headers={"X-Bot-Secret": "nope"}
    )

    assert response.status_code == 401


async def test_a_bearer_token_is_accepted_too(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()

    response = await client.get(
        f"{BASE}/me",
        params={"telegram_id": telegram_id},
        headers={"Authorization": f"Bearer {bot_headers['X-Bot-Secret']}"},
    )

    assert response.status_code == 200


async def test_an_unregistered_telegram_id_is_a_404(client, bot_headers):
    response = await client.get(f"{BASE}/me", params={"telegram_id": 999999999},
                                headers=bot_headers)

    assert response.status_code == 404
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "unknown_worker"
    assert "գրանցված չեք" in body["error"]["message"], "the message must be printable Armenian"


async def test_a_deactivated_worker_is_told_so(client, bot_headers):
    owner_id = await make_owner()
    _, telegram_id = await make_worker(owner_id, is_active=False)

    response = await client.get(f"{BASE}/me", params={"telegram_id": telegram_id},
                                headers=bot_headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "worker_inactive"


# -- /me and /checkin --------------------------------------------------------

async def test_me_reports_no_session_before_the_shift_starts(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()

    body = (await client.get(f"{BASE}/me", params={"telegram_id": telegram_id},
                             headers=bot_headers)).json()

    assert body["worker"]["name"] == "Անի"
    assert body["worker"]["salary_amount"] == "8000.00"
    assert body["session"] is None


async def test_checkin_reports_distance_without_writing_anything(client, bot_headers):
    _, store_id, _, telegram_id, _ = await _world()

    response = await client.post(
        f"{BASE}/checkin",
        json={"telegram_id": telegram_id, "lat": FAR_LAT, "lng": YEREVAN_LNG},
        headers=bot_headers,
    )

    assert response.status_code == 200, "out of range is information, not an error"
    body = response.json()
    assert body["matched_store"] is None
    assert body["candidates"][0]["id"] == store_id
    assert body["candidates"][0]["within_geofence"] is False
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 0


# -- opening -----------------------------------------------------------------

async def test_opening_attaches_the_worker_to_the_matched_store(client, bot_headers):
    _, store_id, _, telegram_id, _ = await _world()

    response = await _open(client, bot_headers, telegram_id)

    assert response.status_code == 201
    body = response.json()
    assert body["ok"] is True and body["duplicate"] is False
    assert body["session"]["store_id"] == store_id
    assert body["session"]["store_name"] == "Խանութ 1"


async def test_opening_out_of_range_says_how_far(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": FAR_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20,
              "idempotency_key": "idem-key-open-01", "live_period": 900},
        headers=bot_headers,
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "no_store_in_range"
    assert "Խանութ 1" in error["message"]
    assert error["details"]["nearest"]["distance_m"] > 700


async def test_the_bot_cannot_name_its_own_store(client, bot_headers):
    """extra="forbid": a store_id in the payload is a loud 422, not a way in."""
    _, store_id, _, telegram_id, _ = await _world()

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "store_id": store_id, "idempotency_key": "idem-key-open-01", "live_period": 900},
        headers=bot_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_a_short_idempotency_key_is_refused(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": telegram_id, "lat": YEREVAN_LAT, "lng": YEREVAN_LNG,
              "accuracy_m": 20, "idempotency_key": "short"},
        headers=bot_headers,
    )

    assert response.status_code == 422


# -- items and selling -------------------------------------------------------

async def test_items_needs_an_open_shift(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()

    response = await client.get(f"{BASE}/items", params={"telegram_id": telegram_id},
                                headers=bot_headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_open_session"


async def test_item_search_finds_by_part_of_the_name(client, bot_headers):
    owner_id, store_id, _, telegram_id, _ = await _world()
    await make_item(owner_id, store_id, "Elf Bar BC5000", count=4, sell_price="4000.00")
    await _open(client, bot_headers, telegram_id)

    body = (await client.get(f"{BASE}/items",
                             params={"telegram_id": telegram_id, "q": "elf"},
                             headers=bot_headers)).json()

    assert [i["name"] for i in body["items"]] == ["Elf Bar BC5000"]
    assert body["items"][0]["sell_price"] == "4000.00"
    assert body["store_name"] == "Խանութ 1"


async def test_a_sale_returns_what_changed(client, bot_headers):
    _, _, _, telegram_id, item_id = await _world()
    await _open(client, bot_headers, telegram_id)

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 2}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["sale"]["total"] == "7000.00"
    assert body["sale"]["lines"][0]["remaining_count"] == 8
    assert body["store_totals"]["cash"] == "7000.00"


async def test_item_search_sends_the_wholesale_price(client, bot_headers):
    """Without it the bot's «Մեծածախ» button is unreachable for every product.

    The keyboard only offers wholesale when there is a price to offer, so
    leaving this out of the payload did not break anything visibly — it just
    meant no cashier could ever sell wholesale, and nothing said so.
    """
    owner_id, store_id, _, telegram_id, item_id = await _world()
    await db.execute("UPDATE items SET wholesale_price = 2500 WHERE id = $1", item_id)
    plain = await make_item(owner_id, store_id, "Elf Bar", count=5, sell_price="4000.00")
    await _open(client, bot_headers, telegram_id)

    body = (await client.get(
        f"{BASE}/items", params={"telegram_id": telegram_id}, headers=bot_headers
    )).json()

    by_id = {row["id"]: row for row in body["items"]}
    assert by_id[item_id]["wholesale_price"] == "2500.00"
    assert by_id[plain]["wholesale_price"] is None, "not sold wholesale is an answer"


async def test_the_sale_response_keeps_the_shape_the_bot_reads(client, bot_headers):
    """A contract test, written after breaking it.

    The bot builds its confirmation from these exact keys. When it read one that
    did not exist, the sale committed and *then* the bot crashed rendering the
    reply — so the cashier was told it had failed and would have entered it
    again. Renaming any of these should fail here, on this side, first.
    """
    _, _, _, telegram_id, item_id = await _world()
    await _open(client, bot_headers, telegram_id)

    body = (await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 2}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )).json()

    assert set(body) >= {"ok", "duplicate", "sale", "store_totals"}
    assert set(body["store_totals"]) >= {"cash", "card"}
    line = body["sale"]["lines"][0]
    assert set(line) >= {"name", "quantity", "line_total", "remaining_count"}
    # Money as decimal strings, so nothing has been through a float.
    assert isinstance(line["line_total"], str)
    assert isinstance(body["store_totals"]["cash"], str)


async def test_money_never_arrives_as_a_float(client, bot_headers):
    """A float has already lost digits by the time we see it, so it is refused
    rather than quietly rounded."""
    _, _, _, telegram_id, item_id = await _world()
    await _open(client, bot_headers, telegram_id)

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id,
              "items": [{"item_id": item_id, "quantity": 1, "unit_price": 3500.55}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_an_unknown_payment_method_is_refused(client, bot_headers):
    _, _, _, telegram_id, item_id = await _world()
    await _open(client, bot_headers, telegram_id)

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 1}],
              "payment_method": "bitcoin", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_selling_more_than_there_is_explains_itself(client, bot_headers):
    _, _, _, telegram_id, item_id = await _world(stock=3)
    await _open(client, bot_headers, telegram_id)

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 5}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "insufficient_stock"
    assert error["details"]["available"] == 3
    assert "3" in error["message"] and "HQD Cuvie" in error["message"]


async def test_a_worker_cannot_reach_another_owners_stock(client, bot_headers):
    """The whole tenancy story, end to end through HTTP."""
    _, _, _, telegram_id, _ = await _world()
    other_owner = await make_owner()
    other_store = await make_store(other_owner)
    other_item = await make_item(other_owner, other_store, count=99)
    await _open(client, bot_headers, telegram_id)

    response = await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": other_item, "quantity": 1}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_item"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", other_item) == 99


# -- voiding and closing -----------------------------------------------------

async def test_voiding_the_last_sale_puts_everything_back(client, bot_headers):
    _, _, _, telegram_id, item_id = await _world()
    await _open(client, bot_headers, telegram_id)
    await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 2}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    response = await client.post(
        f"{BASE}/sale/void", json={"telegram_id": telegram_id, "reason": "սխալ"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert response.json()["store_totals"]["cash"] == "0.00"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 10


async def test_ending_a_shift_reports_the_salary_and_the_takings(client, bot_headers):
    _, _, _, telegram_id, item_id = await _world(salary="8000.00")
    await _open(client, bot_headers, telegram_id)
    await client.post(
        f"{BASE}/sale",
        json={"telegram_id": telegram_id, "items": [{"item_id": item_id, "quantity": 3}],
              "payment_method": "cash", "idempotency_key": "idem-key-sale-01"},
        headers=bot_headers,
    )

    body = (await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": telegram_id, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )).json()

    summary = body["summary"]
    assert summary["sales"]["receipts"] == 1
    assert summary["sales"]["cash_total"] == "10500.00"
    assert summary["salary_deducted"] == "8000.00"
    assert summary["store_closed"] is True
    # Snapshot on the closed session: 10500 taken in, 8000 paid out.
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("2500.00")


async def test_a_replayed_end_returns_the_same_summary(client, bot_headers):
    _, _, _, telegram_id, _ = await _world()
    await _open(client, bot_headers, telegram_id)
    payload = {"telegram_id": telegram_id, "idempotency_key": "idem-key-end-01"}

    first = (await client.post(f"{BASE}/shift/end", json=payload, headers=bot_headers)).json()
    second = (await client.post(f"{BASE}/shift/end", json=payload, headers=bot_headers)).json()

    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["summary"]["session_id"] == second["summary"]["session_id"]
    assert first["summary"]["salary_deducted"] == second["summary"]["salary_deducted"]


async def test_closing_the_store_is_refused_while_a_colleague_is_on_shift(client, bot_headers):
    """Their shift would end without them declaring what they sold."""
    owner_id, _, _, first_tg, _ = await _world(salary="8000.00")
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="6000.00")
    await _open(client, bot_headers, first_tg, "idem-key-open-aa")
    await _open(client, bot_headers, second_tg, "idem-key-open-bb")

    response = await client.post(
        f"{BASE}/store/close",
        json={"telegram_id": first_tg, "idempotency_key": "idem-key-close-1"},
        headers=bot_headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "others_on_shift"
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 2


async def test_the_last_one_out_closes_the_store_for_everyone(client, bot_headers):
    owner_id, _, _, first_tg, _ = await _world(salary="8000.00", till="50000.00")
    _, second_tg = await make_worker(owner_id, "Բ", salary_amount="6000.00")
    await _open(client, bot_headers, first_tg, "idem-key-open-aa")
    await _open(client, bot_headers, second_tg, "idem-key-open-bb")
    await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": second_tg, "idempotency_key": "idem-key-end-bb"},
        headers=bot_headers,
    )

    response = await client.post(
        f"{BASE}/store/close",
        json={"telegram_id": first_tg, "idempotency_key": "idem-key-close-1"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0
    assert await db.fetchval("SELECT salaries_at_close FROM store_sessions") == Decimal("14000.00")
