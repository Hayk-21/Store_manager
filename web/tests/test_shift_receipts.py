"""``GET /shift/receipts`` — the list a cashier corrects their own mistakes from.

The bot's «Ուղղել գրառումը» screen. Until it existed, a mis-rung receipt was
reversible for as long as the undo button under the confirmation stayed on screen —
about one more sale — and after that it belonged to the owner.

The list is deliberately narrow, and these hold it there: one worker's own receipts,
from the shift they are on, whole rather than rolled up per product, carrying the ids
that make a correction land on the receipt that was pointed at.
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


async def _on_shift(handle: str = "@ownerhandle", name: str = "Անի"):
    owner_id = await make_owner(handle)
    store_id = await make_store(owner_id, "Նուբարաշեն", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=50,
        self_price="1500.00", sell_price="3500.00",
    )
    worker_id, telegram_id = await make_worker(owner_id, name, salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name=name, salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(
        worker, YEREVAN_LAT, YEREVAN_LNG, 20, f"idem-open-{handle}", 900
    )
    return worker, telegram_id, item_id, owner_id, store_id


async def _receipts(client, bot_headers, telegram_id: int, **params) -> dict:
    response = await client.get(
        f"{BASE}/shift/receipts",
        params={"telegram_id": telegram_id, **params},
        headers=bot_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_every_receipt_of_the_shift_comes_back_whole(client, bot_headers):
    """Whole, not rolled up per product. «three HQD Cuvie» is the right answer to
    "what have I sold today" and cannot be pointed at and cancelled."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    for n in range(3):
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 2}], "cash", f"idem-sale-{n}"
        )

    body = await _receipts(client, bot_headers, telegram_id)

    assert len(body["receipts"]) == 3
    assert body["total_receipts"] == 3
    assert all(r["id"] for r in body["receipts"]), "each one carries its id"
    assert body["receipts"][0]["total"] == "7000.00"
    assert "HQD Cuvie ×2" in body["receipts"][0]["lines"]


async def test_the_newest_receipt_is_first(client, bot_headers):
    """A cashier correcting a slip is almost always after something they did a
    minute ago, and scrolling a keyboard on a phone is the thing to avoid."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    first = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-a"
    )
    last = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 4}], "card", "idem-sale-b"
    )

    body = await _receipts(client, bot_headers, telegram_id)

    assert [r["id"] for r in body["receipts"]] == [
        last["sale"]["id"], first["sale"]["id"]
    ]


async def test_a_cancelled_receipt_is_marked_rather_than_dropped(client, bot_headers):
    """Dropping it leaves a cashier unable to tell a receipt they already cancelled
    from one that was never there, and the next thing they do is cancel another."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await sales_service.void_last_sale(worker, "սխալ")

    body = await _receipts(client, bot_headers, telegram_id)

    assert len(body["receipts"]) == 1
    assert body["receipts"][0]["voided"] is True


async def test_a_quiet_shift_lists_nothing_rather_than_failing(client, bot_headers):
    _, telegram_id, _, _, _ = await _on_shift()

    body = await _receipts(client, bot_headers, telegram_id)

    assert body["receipts"] == []
    assert body["total_receipts"] == 0


# -- whose receipts ----------------------------------------------------------

async def test_a_colleagues_receipts_are_not_in_the_list(client, bot_headers):
    """The one property that decides whether this feature is safe. A work session
    belongs to one worker, so there is no argument by which this returns another
    cashier's takings — and no way to cancel them."""
    worker, telegram_id, item_id, owner_id, store_id = await _on_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-mine"
    )

    mate_id, mate_telegram = await make_worker(owner_id, "Գոռ", salary_amount="0.00")
    mate = shifts_service.Worker(
        id=mate_id, owner_id=owner_id, name="Գոռ", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(
        mate, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-mate", 900
    )
    await sales_service.record_sale(
        mate, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-theirs"
    )

    mine = await _receipts(client, bot_headers, telegram_id)
    theirs = await _receipts(client, bot_headers, mate_telegram)

    assert [r["total"] for r in mine["receipts"]] == ["7000.00"]
    assert [r["total"] for r in theirs["receipts"]] == ["3500.00"]


async def test_the_list_needs_an_open_shift(client, bot_headers):
    """Off shift there is nothing to correct: the way back to a closed day is the
    owner, and it says so rather than returning an empty list that looks broken."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-1"
    )
    await shifts_service.end_shift(worker, YEREVAN_LAT, YEREVAN_LNG, "idem-end-1")

    response = await client.get(
        f"{BASE}/shift/receipts", params={"telegram_id": telegram_id},
        headers=bot_headers,
    )

    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "no_open_session"


async def test_the_list_needs_the_bot_secret(client):
    _, telegram_id, _, _, _ = await _on_shift()

    response = await client.get(
        f"{BASE}/shift/receipts", params={"telegram_id": telegram_id}
    )

    assert response.status_code in (401, 403)


# -- the window --------------------------------------------------------------

async def test_a_long_shift_is_capped_but_says_how_many_there_are(client, bot_headers):
    """Twenty rows is already a tall keyboard on a phone. The count is what lets the
    bot tell "the one I want is not here" from "there are none"."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    for n in range(6):
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 1}], "cash", f"idem-sale-{n}"
        )

    body = await _receipts(client, bot_headers, telegram_id, limit=4)

    assert len(body["receipts"]) == 4
    assert body["total_receipts"] == 6


# -- and the correction itself -----------------------------------------------

async def test_a_receipt_from_the_list_can_be_cancelled_by_id(client, bot_headers):
    """The whole point. Not "the last one": a cashier correcting the second of three
    must not lose the third."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    first = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", "idem-sale-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-2"
    )

    response = await client.post(
        f"{BASE}/sale/void",
        json={"telegram_id": telegram_id, "sale_id": first["sale"]["id"],
              "reason": "ուղղում աշխատողի կողմից"},
        headers=bot_headers,
    )

    assert response.status_code == 200, response.text
    body = await _receipts(client, bot_headers, telegram_id)
    by_id = {r["id"]: r for r in body["receipts"]}
    assert by_id[first["sale"]["id"]]["voided"] is True
    assert sum(not r["voided"] for r in body["receipts"]) == 1, "the other one stands"


async def test_cancelling_from_the_list_puts_the_goods_back(client, bot_headers):
    """A correction that reversed the money and left the shelf short would make the
    count at closing come out wrong, which is the thing this must not do."""
    worker, telegram_id, item_id, _, _ = await _on_shift()
    sale = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 3}], "cash", "idem-sale-1"
    )
    after_sale = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)

    await client.post(
        f"{BASE}/sale/void",
        json={"telegram_id": telegram_id, "sale_id": sale["sale"]["id"]},
        headers=bot_headers,
    )

    assert after_sale == 47
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 50


async def test_a_colleagues_receipt_cannot_be_cancelled_from_here(client, bot_headers):
    """The list will not show it, and the server will not take it either — the two
    are independent, and only the second is a rule."""
    worker, telegram_id, item_id, owner_id, _ = await _on_shift()
    mate_id, mate_telegram = await make_worker(owner_id, "Գոռ", salary_amount="0.00")
    mate = shifts_service.Worker(
        id=mate_id, owner_id=owner_id, name="Գոռ", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(
        mate, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-mate", 900
    )
    theirs = await sales_service.record_sale(
        mate, [{"item_id": item_id, "quantity": 1}], "cash", "idem-sale-theirs"
    )

    response = await client.post(
        f"{BASE}/sale/void",
        json={"telegram_id": telegram_id, "sale_id": theirs["sale"]["id"]},
        headers=bot_headers,
    )

    assert response.status_code >= 400
    assert response.json()["error"]["code"] == "nothing_to_void"
