"""Stock that left without being sold.

The distinction under test throughout: a broken vape is not a sale at a price of
zero. Recording it as one would put it in the revenue figures, the receipt count
and the worker's bonus progress — and the last of those would pay somebody for
breaking things.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.repo import audit as audit_repo
from app.repo import money as money_repo
from app.services import shifts as shifts_service
from app.services import write_offs as write_offs_service
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


async def _on_shift(count=20, self_price="1500.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    item_id = await make_item(
        owner_id, store_id, "HQD Cuvie", count=count,
        self_price=self_price, sell_price="3500.00",
    )
    worker_id, telegram_id = await make_worker(owner_id, "Անի", salary_amount="0.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal("0.00")
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    return owner_id, store_id, item_id, worker, telegram_id


# -- taking it off the shelf --------------------------------------------------

async def test_writing_off_reduces_the_stock(client):
    _, _, item_id, worker, _ = await _on_shift(count=20)

    result = await write_offs_service.record(worker, item_id, 3, "կոտրված", "idem-wo-1")

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17
    assert result["write_off"]["remaining_count"] == 17
    assert result["write_off"]["quantity"] == 3


async def test_it_costs_what_we_paid_for_it(client):
    """Not what we would have sold it for — that profit was never made."""
    _, _, item_id, worker, _ = await _on_shift(self_price="1500.00")

    result = await write_offs_service.record(worker, item_id, 3, None, "idem-wo-1")

    assert result["write_off"]["total_cost"] == "4500.00"
    assert await db.fetchval("SELECT unit_cost FROM write_offs") == Decimal("1500.00")


async def test_the_cost_is_frozen_at_the_moment_it_broke(client):
    """Repricing the item later must not rewrite what the loss was worth."""
    _, _, item_id, worker, _ = await _on_shift(self_price="1500.00")
    await write_offs_service.record(worker, item_id, 2, None, "idem-wo-1")

    await db.execute("UPDATE items SET self_price = 9000 WHERE id = $1", item_id)

    assert await db.fetchval("SELECT total_cost FROM write_offs") == Decimal("3000.00")


async def test_it_is_not_a_sale(client):
    """The whole point. No receipt, no revenue, no money moving in the till."""
    _, _, item_id, worker, _ = await _on_shift()

    await write_offs_service.record(worker, item_id, 3, "կոտրված", "idem-wo-1")

    assert await db.fetchval("SELECT count(*) FROM sales") == 0
    assert await db.fetchval("SELECT count(*) FROM cash_movements") == 0


async def test_it_does_not_count_towards_a_bonus(client):
    """Otherwise the shop would be paying people for breaking things."""
    owner_id, _, item_id, worker, _ = await _on_shift(count=100)
    await db.execute(
        """
        UPDATE workers SET bonus_threshold = 1000, bonus_amount = 5000, bonus_period = 'day'
         WHERE id = $1
        """,
        worker.id,
    )

    await write_offs_service.record(worker, item_id, 50, "կոտրված", "idem-wo-1")
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))

    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'bonus'"
    ) == 0


async def test_writing_off_more_than_there_is_is_refused(client):
    _, _, item_id, worker, _ = await _on_shift(count=4)

    with pytest.raises(BotError) as caught:
        await write_offs_service.record(worker, item_id, 9, None, "idem-wo-1")

    assert caught.value.code == "insufficient_stock"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 4
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


async def test_writing_off_needs_an_open_shift(client):
    """The store comes from the shift, so a worker cannot write off stock in a
    shop they are not standing in."""
    _, _, item_id, worker, _ = await _on_shift()
    await shifts_service.close_out_shift(worker, [], "idem-close-1", counted=Decimal("0"))

    with pytest.raises(BotError) as caught:
        await write_offs_service.record(worker, item_id, 1, None, "idem-wo-1")

    assert caught.value.code == "no_open_session"


async def test_another_owners_item_cannot_be_written_off(client):
    _, _, _, worker, _ = await _on_shift()
    other = await make_owner("@ownerother")
    other_store = await make_store(other, "Ուրիշի խանութ")
    stranger_item = await make_item(other, other_store, "Elf Bar", count=10)

    with pytest.raises(BotError) as caught:
        await write_offs_service.record(worker, stranger_item, 1, None, "idem-wo-1")

    assert caught.value.code == "unknown_item"
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", stranger_item) == 10


async def test_a_retry_does_not_write_it_off_twice(client):
    """A flaky connection at the counter must not double the loss."""
    _, _, item_id, worker, _ = await _on_shift(count=20)

    first = await write_offs_service.record(worker, item_id, 3, "կոտրված", "idem-wo-1")
    second = await write_offs_service.record(worker, item_id, 3, "կոտրված", "idem-wo-1")

    assert second["duplicate"] is True
    assert second["write_off"]["id"] == first["write_off"]["id"]
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 1


# -- through the bot API ------------------------------------------------------

async def test_the_endpoint_records_a_write_off(client, bot_headers):
    _, _, item_id, _, telegram_id = await _on_shift(count=20)

    response = await client.post(
        f"{BASE}/write-off",
        json={"telegram_id": telegram_id, "item_id": item_id, "quantity": 2,
              "reason": "հոսում է", "idempotency_key": "idem-key-wo-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["write_off"]["name"] == "HQD Cuvie"
    assert body["write_off"]["remaining_count"] == 18
    assert await db.fetchval("SELECT reason FROM write_offs") == "հոսում է"


async def test_the_endpoint_shape_is_what_the_bot_reads(client, bot_headers):
    """Same contract test as the sale path, for the same reason."""
    _, _, item_id, _, telegram_id = await _on_shift()

    body = (await client.post(
        f"{BASE}/write-off",
        json={"telegram_id": telegram_id, "item_id": item_id, "quantity": 1,
              "idempotency_key": "idem-key-wo-01"},
        headers=bot_headers,
    )).json()

    assert set(body) >= {"ok", "duplicate", "write_off"}
    assert set(body["write_off"]) >= {
        "id", "name", "quantity", "total_cost", "remaining_count"
    }
    assert isinstance(body["write_off"]["total_cost"], str)


# -- what the owner sees ------------------------------------------------------

async def test_write_offs_show_up_on_the_report(client):
    owner_id, _, item_id, worker, _ = await _on_shift()
    await write_offs_service.record(worker, item_id, 3, "կոտրված", "idem-wo-1")
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Խոտան" in page.text
    assert "HQD Cuvie" in page.text
    assert "կոտրված" in page.text


async def test_the_loss_reaches_the_statistics(client):
    from app.config import settings
    from app.repo import write_offs as write_offs_repo

    owner_id, _, item_id, worker, _ = await _on_shift(self_price="1500.00")
    await write_offs_service.record(worker, item_id, 4, None, "idem-wo-1")
    today = settings.local_day()

    cost = await write_offs_repo.cost_between(owner_id, today, today)

    assert cost == Decimal("6000.00")


async def test_the_till_is_untouched_by_breakage(client):
    _, _, item_id, worker, _ = await _on_shift()
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await write_offs_service.record(worker, item_id, 3, None, "idem-wo-1")

    totals = await money_repo.totals_for_session(session_id)
    assert totals["cash"] == Decimal("0")
    assert totals["card"] == Decimal("0")


# -- the owner writing one off themselves -------------------------------------

async def _csrf() -> str:
    return await db.fetchval(
        "SELECT csrf_token FROM auth_sessions ORDER BY created_at DESC LIMIT 1"
    )


async def _a_shut_shop(count=20, self_price="1500.00"):
    """An evening that is over: the shift closed out and the store with it, which is
    when the owner finds the broken box and has nobody to attribute it to."""
    owner_id, _, item_id, worker, _ = await _on_shift(count=count, self_price=self_price)
    await shifts_service.close_out_shift(worker, [], "idem-close-1", close_store_too=True, counted=Decimal("0"))
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    return owner_id, item_id, session_id


async def _write_off_from_the_report(client, session_id, item_id, quantity, reason=""):
    return await client.post(
        f"/store-sessions/{session_id}/write-offs",
        data={"csrf_token": await _csrf(), "item_id": str(item_id),
              "quantity": str(quantity), "reason": reason},
    )


async def test_the_owner_can_write_off_stock_with_nobody_on_shift(client):
    """The bot path needs a worker standing in an open shift, so a box found broken
    after the shop had shut could not be recorded at all."""
    _, item_id, session_id = await _a_shut_shop(count=20)
    await login(client, "@ownerhandle")

    response = await _write_off_from_the_report(client, session_id, item_id, 3, "կոտրված")

    assert response.status_code == 303
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17
    row = await db.fetchrow("SELECT worker_id, store_session_id, reason FROM write_offs")
    assert row["worker_id"] is None, "nobody was on duty to attribute it to"
    assert row["store_session_id"] == session_id
    assert row["reason"] == "կոտրված"


async def test_the_owners_write_off_costs_what_the_shop_paid(client):
    """The same rule as the cashier's: what we paid, not what we would have sold it
    for — that profit was never made."""
    _, item_id, session_id = await _a_shut_shop(self_price="1500.00")
    await login(client, "@ownerhandle")

    await _write_off_from_the_report(client, session_id, item_id, 3)

    assert await db.fetchval("SELECT total_cost FROM write_offs") == Decimal("4500.00")


async def test_the_owner_cannot_write_off_more_than_the_shelf_holds(client):
    """Stock going negative is a lie about the shelf, and nothing on the page would
    show which count it was."""
    _, item_id, session_id = await _a_shut_shop(count=4)
    await login(client, "@ownerhandle")

    response = await _write_off_from_the_report(client, session_id, item_id, 9)

    assert response.status_code == 422
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 4
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0


async def test_the_owners_write_off_moves_no_money(client):
    """Breakage is stock lost, not money paid: the money left when it was bought."""
    _, item_id, session_id = await _a_shut_shop()
    await login(client, "@ownerhandle")
    before = await money_repo.totals_for_session(session_id)

    await _write_off_from_the_report(client, session_id, item_id, 3)

    after = await money_repo.totals_for_session(session_id)
    assert after["cash"] == before["cash"]
    assert after["card"] == before["card"]


async def test_the_owners_write_off_can_be_undone_from_the_history(client):
    """Every owner correction is reversible, and reversing this one means the vape
    did not break after all — so it goes back on the shelf."""
    owner_id, item_id, session_id = await _a_shut_shop(count=20)
    await login(client, "@ownerhandle")
    await _write_off_from_the_report(client, session_id, item_id, 3, "կոտրված")
    event = await audit_repo.newest_pending(owner_id)

    response = await client.post(
        f"/history/{event['id']}/revert", data={"csrf_token": await _csrf()}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM write_offs") == 0
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20


async def test_undoing_a_write_off_that_was_already_deleted_invents_no_stock(client):
    """Deleting one from the report restores the count and leaves no event behind it,
    so an undo of the entry afterwards must not put the same goods back twice."""
    owner_id, item_id, session_id = await _a_shut_shop(count=20)
    await login(client, "@ownerhandle")
    await _write_off_from_the_report(client, session_id, item_id, 3, "կոտրված")
    event = await audit_repo.newest_pending(owner_id)
    write_off_id = await db.fetchval("SELECT id FROM write_offs")
    await client.post(
        f"/write-offs/{write_off_id}/delete", data={"csrf_token": await _csrf()}
    )

    await client.post(
        f"/history/{event['id']}/revert", data={"csrf_token": await _csrf()}
    )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20


async def test_the_report_offers_the_form_on_an_evening_when_nothing_broke(client):
    """The section only appeared once something had been written off, which is the
    one evening the owner does not need it: they are here to record the first one."""
    _, _, session_id = await _a_shut_shop()
    await login(client, "@ownerhandle")

    page = await client.get(f"/reports?store_session_id={session_id}")

    assert "Գրանցել խոտան" in page.text
    assert "Այս հերթափոխին խոտան չի գրանցվել" in page.text


async def test_another_owner_cannot_write_off_against_a_session_that_is_not_theirs(client):
    """A session belonging to somebody else reads as missing, like every other row."""
    _, item_id, session_id = await _a_shut_shop(count=20)
    await make_owner("@ownerother")
    await login(client, "@ownerother")

    response = await _write_off_from_the_report(client, session_id, item_id, 2)

    assert response.status_code == 404
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20
