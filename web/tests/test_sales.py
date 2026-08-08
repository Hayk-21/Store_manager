"""Recording and voiding a sale — requirement 6.

Stock and money move together here, so these are the tests that decide whether
the numbers on the owner's screen can be trusted.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from tests.factories import YEREVAN_LAT, YEREVAN_LNG, make_item, make_owner, make_store, make_worker

SALE_KEY = "idem-key-sale-0001"


async def _open_shift(salary: str = "0.00"):
    """An owner with a store, a worker already on shift, and one item in stock."""
    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    worker_id, telegram_id = await make_worker(owner_id, salary_amount=salary)
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=10,
                              self_price="1500.00", sell_price="3500.00")
    return owner_id, store_id, worker, item_id, telegram_id


async def _count(item_id: int) -> int:
    return await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)


async def _cash() -> Decimal:
    return await db.fetchval(
        "SELECT coalesce(sum(amount) FILTER (WHERE method = 'cash'), 0) FROM cash_movements"
    )


# -- the happy path ----------------------------------------------------------

async def test_a_sale_moves_stock_and_money_together(client):
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", SALE_KEY
    )

    assert result["sale"]["total"] == "7000.00"
    assert result["sale"]["lines"][0]["remaining_count"] == 8
    assert await _count(item_id) == 8
    assert await _cash() == Decimal("7000.00")
    assert result["store_totals"]["cash"] == "7000.00"


async def test_prices_are_snapshotted_onto_the_line(client):
    """Repricing an item later must not rewrite what was already sold."""
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY)

    await db.execute("UPDATE items SET sell_price = 9999.00, self_price = 9.00 WHERE id = $1",
                     item_id)

    line = await db.fetchrow("SELECT unit_price, unit_cost, line_total FROM sale_items")
    assert line["unit_price"] == Decimal("3500.00")
    assert line["unit_cost"] == Decimal("1500.00"), "cost snapshot makes realised profit knowable"
    assert await db.fetchval("SELECT total FROM sales") == Decimal("3500.00")


async def test_card_money_lands_in_the_card_column(client):
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "card", SALE_KEY
    )

    assert result["store_totals"] == {"cash": "0.00", "card": "3500.00"}


async def test_a_per_line_discount_is_honoured(client):
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker,
        [{"item_id": item_id, "quantity": 2, "unit_price": Decimal("3000.00")}],
        "cash",
        SALE_KEY,
    )

    assert result["sale"]["total"] == "6000.00"


async def test_a_multi_line_basket_is_one_receipt(client):
    owner_id, store_id, worker, first, _ = await _open_shift()
    second = await make_item(owner_id, store_id, "Elf Bar", count=5, sell_price="4000.00")

    result = await sales_service.record_sale(
        worker,
        [{"item_id": first, "quantity": 1}, {"item_id": second, "quantity": 2}],
        "cash",
        SALE_KEY,
    )

    assert result["sale"]["total"] == "11500.00"
    assert await db.fetchval("SELECT count(*) FROM sales") == 1
    assert await db.fetchval("SELECT count(*) FROM sale_items") == 2


async def test_repeating_an_item_in_one_basket_merges_into_one_line(client):
    """UNIQUE (sale_id, item_id) would reject two rows, so they have to merge."""
    _, _, worker, item_id, _ = await _open_shift()

    result = await sales_service.record_sale(
        worker,
        [{"item_id": item_id, "quantity": 1}, {"item_id": item_id, "quantity": 2}],
        "cash",
        SALE_KEY,
    )

    assert await db.fetchval("SELECT count(*) FROM sale_items") == 1
    assert result["sale"]["lines"][0]["quantity"] == 3
    assert await _count(item_id) == 7


# -- idempotency -------------------------------------------------------------

async def test_a_replayed_sale_sells_nothing_twice(client):
    _, _, worker, item_id, _ = await _open_shift()

    first = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", SALE_KEY
    )
    second = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 2}], "cash", SALE_KEY
    )

    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["sale"]["id"] == second["sale"]["id"]
    assert first["sale"]["total"] == second["sale"]["total"]
    assert await _count(item_id) == 8
    assert await db.fetchval("SELECT count(*) FROM sales") == 1


async def test_two_different_keys_are_two_different_sales(client):
    _, _, worker, item_id, _ = await _open_shift()

    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash",
                                    "idem-key-aaaa-1")
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash",
                                    "idem-key-bbbb-2")

    assert await db.fetchval("SELECT count(*) FROM sales") == 2
    assert await _count(item_id) == 8


# -- refusals, and what they leave behind ------------------------------------

async def test_selling_more_than_there_is_changes_nothing(client):
    _, _, worker, item_id, _ = await _open_shift()

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 50}], "cash", SALE_KEY
        )

    error = caught.value
    assert error.code == "insufficient_stock"
    assert error.details == {"item_id": item_id, "name": "HQD Cuvie",
                             "requested": 50, "available": 10}
    assert await _count(item_id) == 10
    assert await db.fetchval("SELECT count(*) FROM sales") == 0


async def test_a_failure_late_in_a_basket_rolls_back_the_whole_basket(client):
    """No partial fulfilment: the first two lines must not stay taken."""
    owner_id, store_id, worker, first, _ = await _open_shift()
    second = await make_item(owner_id, store_id, "Elf Bar", count=5)
    third = await make_item(owner_id, store_id, "Lost Mary", count=1)

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(
            worker,
            [
                {"item_id": first, "quantity": 1},
                {"item_id": second, "quantity": 1},
                {"item_id": third, "quantity": 99},   # this one cannot be filled
            ],
            "cash",
            SALE_KEY,
        )

    assert caught.value.code == "insufficient_stock"
    assert await _count(first) == 10
    assert await _count(second) == 5
    assert await _count(third) == 1
    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await _cash() == Decimal("0")


async def test_an_item_from_another_store_is_unknown(client):
    owner_id, _, worker, _, _ = await _open_shift()
    elsewhere = await make_store(owner_id, "Երկրորդ", lat=None, lng=None)
    stranger = await make_item(owner_id, elsewhere, "Այլ խանութի ապրանք")

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(
            worker, [{"item_id": stranger, "quantity": 1}], "cash", SALE_KEY
        )

    assert caught.value.code == "unknown_item"


async def test_another_owners_item_is_unknown(client):
    _, _, worker, _, _ = await _open_shift()
    other_owner = await make_owner()
    other_store = await make_store(other_owner)
    other_item = await make_item(other_owner, other_store)

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(
            worker, [{"item_id": other_item, "quantity": 1}], "cash", SALE_KEY
        )

    assert caught.value.code == "unknown_item"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", other_item) == 10


async def test_selling_without_an_open_shift_is_refused(client):
    _, _, worker, item_id, _ = await _open_shift()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(
            worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY
        )

    assert caught.value.code == "no_open_session"


async def test_an_empty_basket_is_refused(client):
    _, _, worker, _, _ = await _open_shift()

    with pytest.raises(BotError) as caught:
        await sales_service.record_sale(worker, [], "cash", SALE_KEY)

    assert caught.value.code == "empty_basket"


# -- voiding -----------------------------------------------------------------

async def test_voiding_puts_the_stock_and_the_money_back(client):
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 2}], "cash", SALE_KEY)

    result = await sales_service.void_last_sale(worker, "սխալ սեղմեցի")

    assert result["voided"]["total"] == "7000.00"
    assert await _count(item_id) == 10
    assert await _cash() == Decimal("0.00")
    assert result["store_totals"]["cash"] == "0.00"


async def test_a_voided_receipt_is_kept_with_who_voided_it(client):
    """Nothing is deleted: the owner must be able to see that it happened."""
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY)

    await sales_service.void_last_sale(worker, "սխալ")

    sale = await db.fetchrow("SELECT voided_at, voided_by_worker_id, void_reason FROM sales")
    assert sale["voided_at"] is not None
    assert sale["voided_by_worker_id"] == worker.id
    assert sale["void_reason"] == "սխալ"


async def test_voiding_walks_backwards_one_receipt_at_a_time(client):
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash",
                                    "idem-key-aaaa-1")
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 3}], "cash",
                                    "idem-key-bbbb-2")

    first_void = await sales_service.void_last_sale(worker)
    assert first_void["voided"]["total"] == "10500.00"   # the 3-unit one

    second_void = await sales_service.void_last_sale(worker)
    assert second_void["voided"]["total"] == "3500.00"

    assert await _count(item_id) == 10
    with pytest.raises(BotError) as caught:
        await sales_service.void_last_sale(worker)
    assert caught.value.code == "nothing_to_void"


async def test_a_named_sale_is_the_one_reversed(client):
    """The undo button under a confirmation carries its own sale id. Tapped
    three sales later it must still reverse the receipt it belongs to, not
    whatever happens to be most recent by then."""
    _, _, worker, item_id, _ = await _open_shift()
    first = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", "idem-key-aaaa-1"
    )
    await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 3}], "cash", "idem-key-bbbb-2"
    )

    result = await sales_service.void_last_sale(worker, sale_id=first["sale"]["id"])

    assert result["voided"]["sale_id"] == first["sale"]["id"]
    assert result["voided"]["total"] == "3500.00", "the older, named one"
    assert await _count(item_id) == 7, "only its unit came back"
    assert await db.fetchval(
        "SELECT count(*) FROM sales WHERE voided_at IS NULL"
    ) == 1, "the later sale stands"


async def test_naming_an_already_voided_sale_is_refused(client):
    """Tapping undo twice must not reverse something else instead."""
    _, _, worker, item_id, _ = await _open_shift()
    sale = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY
    )
    await sales_service.void_last_sale(worker, sale_id=sale["sale"]["id"])

    with pytest.raises(BotError) as caught:
        await sales_service.void_last_sale(worker, sale_id=sale["sale"]["id"])

    assert caught.value.code == "nothing_to_void"


async def test_another_workers_sale_cannot_be_named(client):
    """The lookup is scoped to the caller's own open shift, so an id from
    somebody else's till resolves to nothing."""
    owner_id, store_id, worker, item_id, _ = await _open_shift()
    sale = await sales_service.record_sale(
        worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY
    )
    other_id, _ = await make_worker(owner_id, "Գոռ", salary_amount="0.00")
    other = shifts_service.Worker(
        id=other_id, owner_id=owner_id, name="Գոռ", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(other, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-2", 900)

    with pytest.raises(BotError) as caught:
        await sales_service.void_last_sale(other, sale_id=sale["sale"]["id"])

    assert caught.value.code == "nothing_to_void"
    assert await db.fetchval("SELECT voided_at FROM sales") is None


async def test_a_sale_from_an_earlier_shift_cannot_be_reached(client):
    """Undo is for your own slip just now, not for editing history."""
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY)
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-2", 900)

    with pytest.raises(BotError) as caught:
        await sales_service.void_last_sale(worker)

    assert caught.value.code == "nothing_to_void"
    assert await _count(item_id) == 9


async def test_voiding_with_no_shift_open_is_refused(client):
    _, _, worker, item_id, _ = await _open_shift()
    await sales_service.record_sale(worker, [{"item_id": item_id, "quantity": 1}], "cash", SALE_KEY)
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    with pytest.raises(BotError) as caught:
        await sales_service.void_last_sale(worker)

    assert caught.value.code == "no_open_session"
