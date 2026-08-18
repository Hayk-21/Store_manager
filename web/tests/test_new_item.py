"""A cashier putting a new product on the shelf, from the bot.

A delivery arrives mid-shift and the cashier is the one holding it. Making them
wait for the owner to add it on the website means either the box sits unsellable
or it gets sold as something else, and the second is worse.

The store is never named in the request: it comes from the open shift, like every
other bot write. A cashier cannot stock a shop they are not standing in.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    make_item,
    make_owner,
    make_store,
    make_worker,
)

BASE = "/api/bot/v1"


async def _on_shift():
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    return owner_id, store_id, worker, telegram_id


def _body(telegram_id: int, **overrides) -> dict:
    return {
        "telegram_id": telegram_id,
        "name": "HQD Cuvie",
        "count": 20,
        "self_price": "1500.00",
        "sell_price": "3500.00",
    } | overrides


# -- adding it ----------------------------------------------------------------

async def test_it_lands_on_the_shelf_of_the_store_being_worked(client, bot_headers):
    _, store_id, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )

    assert response.status_code == 201, response.text
    row = await db.fetchrow("SELECT store_id, name, count, self_price, sell_price FROM items")
    assert row["store_id"] == store_id, "the shop they are standing in, not one they named"
    assert row["name"] == "HQD Cuvie"
    assert row["count"] == 20
    assert row["self_price"] == Decimal("1500.00")
    assert row["sell_price"] == Decimal("3500.00")


async def test_the_reply_is_what_the_bot_reads(client, bot_headers):
    _, _, _, telegram_id = await _on_shift()

    body = (await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )).json()

    assert set(body["item"]) >= {"id", "name", "count", "sell_price", "wholesale_price"}
    assert body["item"]["sell_price"] == "3500.00"
    assert isinstance(body["item"]["sell_price"], str), "money is never a float"


# -- the two selling prices ---------------------------------------------------

async def test_a_wholesale_price_can_be_given_with_it(client, bot_headers):
    """Without one the «Մեծածախ» button never appears when selling the item, and
    the cashier who just added it is the one who would need it."""
    _, _, _, telegram_id = await _on_shift()

    body = (await client.post(
        f"{BASE}/items",
        json=_body(telegram_id, wholesale_price="3000.00"),
        headers=bot_headers,
    )).json()

    assert body["item"]["wholesale_price"] == "3000.00"
    assert await db.fetchval("SELECT wholesale_price FROM items") == Decimal("3000.00")


async def test_leaving_it_out_means_not_sold_wholesale_not_sold_free(client, bot_headers):
    """The owner's price sheet has a dash there. Storing 0 would read as free,
    and the sell flow would offer it as a price."""
    _, _, _, telegram_id = await _on_shift()

    body = (await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )).json()

    assert body["item"]["wholesale_price"] is None
    assert await db.fetchval("SELECT wholesale_price FROM items") is None


async def test_the_retail_price_is_the_one_a_sale_uses(client, bot_headers):
    """Both prices are stored, and an ordinary sale takes the retail one."""
    _, _, worker, telegram_id = await _on_shift()
    item_id = (await client.post(
        f"{BASE}/items",
        json=_body(telegram_id, wholesale_price="3000.00"),
        headers=bot_headers,
    )).json()["item"]["id"]

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    assert result["sale"]["total"] == "3500.00"


async def test_a_wholesale_price_never_arrives_as_a_float(client, bot_headers):
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items",
        json=_body(telegram_id, wholesale_price=3000.0),
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_a_negative_wholesale_price_is_refused(client, bot_headers):
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items",
        json=_body(telegram_id, wholesale_price="-1.00"),
        headers=bot_headers,
    )

    assert response.status_code == 422


async def test_it_can_be_sold_straight_away(client, bot_headers):
    """The point of adding it at the counter is the customer waiting at it."""
    _, _, worker, telegram_id = await _on_shift()
    item_id = (await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )).json()["item"]["id"]

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )

    assert result["sale"]["total"] == "3500.00"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 19


async def test_adding_none_of_it_yet_is_allowed(client, bot_headers):
    """Registering the product now and counting the box afterwards is a normal
    order of doing things, and zero is a truthful answer to "how many"."""
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id, count=0), headers=bot_headers
    )

    assert response.status_code == 201
    assert await db.fetchval("SELECT count FROM items") == 0


# -- what is refused ----------------------------------------------------------

async def test_a_name_already_on_the_list_is_refused(client, bot_headers):
    """Saying so beats silently rewriting a colleague's prices."""
    owner_id, store_id, _, telegram_id = await _on_shift()
    await make_item(owner_id, store_id, "HQD Cuvie", count=5,
                    self_price="1000.00", sell_price="3000.00")

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )

    assert response.status_code == 422
    assert "HQD Cuvie" in response.json()["error"]["message"]
    prices = await db.fetch("SELECT sell_price FROM items")
    assert [row["sell_price"] for row in prices] == [Decimal("3000.00")], "untouched"


async def test_adding_without_an_open_shift_is_refused(client, bot_headers):
    _, _, worker, telegram_id = await _on_shift()
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id), headers=bot_headers
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_open_session"
    assert await db.fetchval("SELECT count(*) FROM items") == 0


async def test_a_blank_name_is_refused(client, bot_headers):
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id, name=""), headers=bot_headers
    )

    assert response.status_code == 422


async def test_a_negative_count_is_refused(client, bot_headers):
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id, count=-1), headers=bot_headers
    )

    assert response.status_code == 422


async def test_money_never_arrives_as_a_float(client, bot_headers):
    """A binary float cannot hold 1500.10, and a price that drifts by a luma is
    a price nobody can reconcile."""
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(
        f"{BASE}/items", json=_body(telegram_id, sell_price=3500.0), headers=bot_headers
    )

    assert response.status_code == 422


async def test_it_needs_the_shared_secret(client):
    _, _, _, telegram_id = await _on_shift()

    response = await client.post(f"{BASE}/items", json=_body(telegram_id))

    assert response.status_code == 401
    assert await db.fetchval("SELECT count(*) FROM items") == 0
