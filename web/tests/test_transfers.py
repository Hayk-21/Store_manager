"""Stock moving between two of one owner's shops.

Two shops are two people, which is the whole design. A cashier who needs a box
cannot reach into a shelf they are not standing at, so their request waits until
somebody at the other shop agrees. The owner can see both shelves and answers to
nobody, so theirs applies at once.

The tests that matter most are the ones about *when* the counts change: not on
asking, only on approving, and never twice.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import AppError, BotError
from app.repo import transfers as transfers_repo
from app.services import shifts as shifts_service
from app.services import transfers as transfers_service
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


async def _two_shops(source_count: int = 10):
    """One owner, two shops, a product on the first shelf.

    The shops are a kilometre apart on purpose. Opening a shift geofences to the
    *nearest* store, so two shops at the same coordinates would put both workers in
    the same one and every test here would be about a transfer to itself.
    """
    owner_id = await make_owner("@ownerhandle")
    source = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    target = await make_store(
        owner_id, "Խանութ 2", lat=YEREVAN_LAT + 0.01, lng=YEREVAN_LNG + 0.01
    )
    item_id = await make_item(
        owner_id, source, "HQD Cuvie", count=source_count,
        self_price="1500.00", sell_price="3500.00",
    )
    return owner_id, source, target, item_id


async def _worker_at(owner_id: int, store_id: int, name: str, key: str):
    """Somebody on shift at one particular shop."""
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("0.00")
    )
    store = await db.fetchrow("SELECT lat, lng FROM stores WHERE id = $1", store_id)
    await shifts_service.open_store(
        worker, store["lat"], store["lng"], 20, f"idem-open-{key}", 900
    )
    return worker, telegram_id


async def _count(item_id: int) -> int:
    return await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)


async def _count_at(store_id: int, name: str) -> int | None:
    return await db.fetchval(
        "SELECT count FROM items WHERE store_id = $1 AND name = $2", store_id, name
    )


# -- asking ------------------------------------------------------------------

async def test_asking_moves_nothing_yet(client):
    """The point of it being a request. Until somebody at the other shop agrees,
    both shelves are exactly as they were."""
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")

    result = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    assert result["transfer"]["status"] == "pending"
    assert await _count(item_id) == 10, "the source shelf is untouched"
    assert await _count_at(target, "HQD Cuvie") is None, "and nothing arrived"


async def test_a_worker_cannot_ask_their_own_shop(client):
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, source, "Անի", "ani")

    with pytest.raises(BotError):
        await transfers_service.request_by_worker(
            asker, source, item_id, 1, "idem-tr-01"
        )


async def test_asking_for_more_than_they_have_is_refused(client):
    owner_id, source, target, item_id = await _two_shops(source_count=3)
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")

    with pytest.raises(BotError) as caught:
        await transfers_service.request_by_worker(
            asker, source, item_id, 5, "idem-tr-01"
        )

    assert "3" in caught.value.message


async def test_a_retry_does_not_ask_twice(client):
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")

    first = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )
    second = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    assert second["duplicate"] is True
    assert second["transfer"]["id"] == first["transfer"]["id"]
    assert await db.fetchval("SELECT count(*) FROM transfers") == 1


# -- approving ---------------------------------------------------------------

async def test_approving_moves_the_stock(client):
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    result = await transfers_service.decide_by_worker(
        keeper, asked["transfer"]["id"], approve=True
    )

    assert result["transfer"]["status"] == "approved"
    assert await _count(item_id) == 6
    assert await _count_at(target, "HQD Cuvie") == 4


async def test_the_quantity_is_added_to_stock_the_other_shop_already_has(client):
    """A transfer tops a shelf up; it does not replace what is on it."""
    owner_id, source, target, item_id = await _two_shops()
    await make_item(
        owner_id, target, "HQD Cuvie", count=7,
        self_price="1500.00", sell_price="3500.00",
    )
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    await transfers_service.decide_by_worker(keeper, asked["transfer"]["id"], approve=True)

    assert await _count_at(target, "HQD Cuvie") == 11
    assert await db.fetchval(
        "SELECT count(*) FROM items WHERE store_id = $1", target
    ) == 1, "topped up, not duplicated"


async def test_the_cost_price_travels_with_it(client):
    """Valuing the box at the receiving shop's guess would quietly change what the
    business is worth."""
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    await transfers_service.decide_by_worker(keeper, asked["transfer"]["id"], approve=True)

    assert await db.fetchval(
        "SELECT self_price FROM items WHERE store_id = $1 AND name = 'HQD Cuvie'", target
    ) == Decimal("1500.00")


async def test_rejecting_moves_nothing(client):
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    result = await transfers_service.decide_by_worker(
        keeper, asked["transfer"]["id"], approve=False
    )

    assert result["transfer"]["status"] == "rejected"
    assert await _count(item_id) == 10
    assert await _count_at(target, "HQD Cuvie") is None


async def test_only_the_shop_being_asked_can_answer(client):
    """The shop that wants the box cannot approve its own request — that would be
    reaching into somebody else's shelf, which is the thing this prevents."""
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    with pytest.raises(BotError):
        await transfers_service.decide_by_worker(
            asker, asked["transfer"]["id"], approve=True
        )

    assert await _count(item_id) == 10


async def test_a_second_approval_is_refused(client):
    """Two workers at the source shop can be looking at the same request."""
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )
    await transfers_service.decide_by_worker(keeper, asked["transfer"]["id"], approve=True)

    with pytest.raises(BotError):
        await transfers_service.decide_by_worker(
            keeper, asked["transfer"]["id"], approve=True
        )

    assert await _count(item_id) == 6, "moved once, not twice"


async def test_stock_sold_between_asking_and_approving_is_refused(client):
    """The shelf at approval time is the only one that counts."""
    owner_id, source, target, item_id = await _two_shops(source_count=5)
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 5, "idem-tr-01"
    )
    await db.execute("UPDATE items SET count = 2 WHERE id = $1", item_id)

    with pytest.raises(BotError):
        await transfers_service.decide_by_worker(
            keeper, asked["transfer"]["id"], approve=True
        )

    assert await _count(item_id) == 2
    assert await _count_at(target, "HQD Cuvie") is None


async def test_it_touches_no_money(client):
    """The same box on a different shelf earns nothing and costs nothing."""
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    keeper, _ = await _worker_at(owner_id, source, "Գոռ", "gor")
    asked = await transfers_service.request_by_worker(
        asker, source, item_id, 4, "idem-tr-01"
    )

    await transfers_service.decide_by_worker(keeper, asked["transfer"]["id"], approve=True)

    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_lists_the_other_shops_only(client, bot_headers):
    owner_id, source, target, _ = await _two_shops()
    _, telegram_id = await _worker_at(owner_id, target, "Անի", "ani")

    body = (await client.get(
        f"{BASE}/transfers/stores", params={"telegram_id": telegram_id},
        headers=bot_headers,
    )).json()

    assert [s["id"] for s in body["stores"]] == [source]


async def test_the_other_shops_item_list_carries_no_prices(client, bot_headers):
    """Choosing a box to ask for is not selling it, and what a sister shop charges
    is not part of the job."""
    owner_id, source, target, _ = await _two_shops()
    _, telegram_id = await _worker_at(owner_id, target, "Անի", "ani")

    body = (await client.get(
        f"{BASE}/transfers/items",
        params={"telegram_id": telegram_id, "store_id": source},
        headers=bot_headers,
    )).json()

    assert body["items"][0]["name"] == "HQD Cuvie"
    assert body["items"][0]["count"] == 10
    assert "sell_price" not in body["items"][0]
    assert "self_price" not in body["items"][0]


async def test_another_owners_shop_cannot_be_browsed(client, bot_headers):
    owner_id, _, target, _ = await _two_shops()
    _, telegram_id = await _worker_at(owner_id, target, "Անի", "ani")
    stranger = await make_owner("@stranger")
    theirs = await make_store(stranger, "Ուրիշի խանութ")

    response = await client.get(
        f"{BASE}/transfers/items",
        params={"telegram_id": telegram_id, "store_id": theirs},
        headers=bot_headers,
    )

    assert response.status_code == 404


async def test_the_endpoints_ask_and_then_answer(client, bot_headers):
    owner_id, source, target, item_id = await _two_shops()
    _, asker_tg = await _worker_at(owner_id, target, "Անի", "ani")
    _, keeper_tg = await _worker_at(owner_id, source, "Գոռ", "gor")

    asked = (await client.post(
        f"{BASE}/transfers",
        json={"telegram_id": asker_tg, "from_store_id": source,
              "item_id": item_id, "quantity": 4, "idempotency_key": "idem-tr-01"},
        headers=bot_headers,
    )).json()

    waiting = (await client.get(
        f"{BASE}/transfers/pending", params={"telegram_id": keeper_tg},
        headers=bot_headers,
    )).json()
    assert [row["id"] for row in waiting["incoming"]] == [asked["transfer"]["id"]]

    decided = await client.post(
        f"{BASE}/transfers/{asked['transfer']['id']}/decide",
        json={"telegram_id": keeper_tg, "approve": True},
        headers=bot_headers,
    )

    assert decided.status_code == 200
    assert decided.json()["transfer"]["status"] == "approved"
    assert await _count(item_id) == 6


# -- the owner's side --------------------------------------------------------

async def test_the_owner_moves_stock_with_no_approval(client):
    """They can see both shelves at once and there is nobody to ask."""
    owner_id, source, target, item_id = await _two_shops()

    await transfers_service.move_as_owner(owner_id, item_id, source, target, 4)

    assert await _count(item_id) == 6
    assert await _count_at(target, "HQD Cuvie") == 4
    row = await db.fetchrow("SELECT status, decided_by_owner FROM transfers")
    assert row["status"] == "approved"
    assert row["decided_by_owner"] is True


async def test_the_owner_cannot_move_stock_to_the_same_shop(client):
    owner_id, source, _, item_id = await _two_shops()

    with pytest.raises(AppError):
        await transfers_service.move_as_owner(owner_id, item_id, source, source, 1)


async def test_the_owner_cannot_move_more_than_there_is(client):
    owner_id, source, target, item_id = await _two_shops(source_count=2)

    with pytest.raises(BotError):
        await transfers_service.move_as_owner(owner_id, item_id, source, target, 5)

    assert await _count(item_id) == 2


async def test_the_owner_cannot_move_another_owners_stock(client):
    owner_id, source, target, _ = await _two_shops()
    stranger = await make_owner("@stranger")
    their_store = await make_store(stranger, "Ուրիշի խանութ")
    their_item = await make_item(stranger, their_store, "Ուրիշի ապրանք", count=9)

    with pytest.raises(AppError):
        await transfers_service.move_as_owner(owner_id, their_item, source, target, 1)

    assert await _count(their_item) == 9


async def test_the_page_shows_the_history(client):
    owner_id, source, target, item_id = await _two_shops()
    await transfers_service.move_as_owner(owner_id, item_id, source, target, 4)
    await login(client, "@ownerhandle")

    page = await client.get("/transfers")

    assert page.status_code == 200
    assert "HQD Cuvie" in page.text
    assert "Խանութ 1" in page.text
    assert "Խանութ 2" in page.text


async def test_the_form_moves_stock(client):
    owner_id, source, target, item_id = await _two_shops()
    await login(client, "@ownerhandle")
    csrf = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )

    response = await client.post(
        "/transfers",
        data={"csrf_token": csrf, "from_store_id": str(source),
              "to_store_id": str(target), "item_id": str(item_id), "quantity": "3"},
    )

    assert response.status_code == 303
    assert await _count(item_id) == 7
    assert await _count_at(target, "HQD Cuvie") == 3


async def test_the_item_options_are_the_chosen_shops_own(client):
    """An item belongs to one shop, so a single list of everything would offer
    boxes that are not on the shelf being moved from."""
    owner_id, source, target, _ = await _two_shops()
    await make_item(owner_id, target, "Waka 1800", count=5)
    await login(client, "@ownerhandle")

    page = await client.get(f"/partials/store-items?from_store_id={source}")

    assert "HQD Cuvie" in page.text
    assert "Waka 1800" not in page.text


async def test_a_blank_shop_asks_for_one_rather_than_erroring(client):
    """The select starts empty and its first change event is what asks for this."""
    await _two_shops()
    await login(client, "@ownerhandle")

    page = await client.get("/partials/store-items?from_store_id=")

    assert page.status_code == 200


async def test_pending_requests_show_on_the_page(client):
    owner_id, source, target, item_id = await _two_shops()
    asker, _ = await _worker_at(owner_id, target, "Անի", "ani")
    await transfers_service.request_by_worker(asker, source, item_id, 4, "idem-tr-01")
    await login(client, "@ownerhandle")

    page = await client.get("/transfers")

    assert "սպասում է" in page.text
    assert await transfers_repo.recent_for_owner(owner_id)
