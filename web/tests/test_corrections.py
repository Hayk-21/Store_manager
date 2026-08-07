"""Correcting the books, and undoing the corrections.

The property under test throughout: a correction never destroys anything, so
every one of them can be put back. The ledger stays the source of truth and a
closed session's snapshot is kept in step with it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import AppError
from app.repo import audit as audit_repo
from app.repo import money as money_repo
from app.services import corrections
from app.services import shifts as shifts_service
from tests.factories import YEREVAN_LAT, YEREVAN_LNG, make_item, make_owner, make_store, make_worker

BASE = "/api/bot/v1"
TG = 555000777


async def _a_closed_shift(stock: int = 20, sold: int = 3, salary: str = "8000.00"):
    """A finished shift with one sale on it, ready to be corrected."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    worker_id, _ = await make_worker(owner_id, "Անի", telegram_id=TG, salary_amount=salary)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=stock,
                              self_price="1500.00", sell_price="3500.00")
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)
    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": sold, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-key-close-1",
        close_store_too=True,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    sale_id = await db.fetchval("SELECT id FROM sales")
    shift_id = await db.fetchval("SELECT id FROM work_sessions")
    return owner_id, store_id, worker_id, item_id, session_id, sale_id, shift_id


async def _snapshot(session_id: int) -> Decimal:
    return await db.fetchval(
        "SELECT cash_at_close FROM store_sessions WHERE id = $1", session_id
    )


# -- voiding -----------------------------------------------------------------

async def test_voiding_a_sale_puts_stock_and_money_back(client):
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift(stock=20, sold=3)
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17

    await corrections.void_sale(owner_id, owner_id, sale_id, "սխալ")

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 20
    # 10500 in, 8000 salary out, then the 10500 reversed.
    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("-8000.00")


async def test_a_closed_sessions_snapshot_is_brought_back_into_line(client):
    """The frozen figure on the row would otherwise disagree with the ledger."""
    owner_id, _, _, _, session_id, sale_id, _ = await _a_closed_shift()
    assert await _snapshot(session_id) == Decimal("2500.00")

    await corrections.void_sale(owner_id, owner_id, sale_id)

    assert await _snapshot(session_id) == Decimal("-8000.00")


async def test_a_voided_sale_keeps_its_row(client):
    """A receipt that vanishes is indistinguishable from one that never was."""
    owner_id, _, _, _, _, sale_id, _ = await _a_closed_shift()

    await corrections.void_sale(owner_id, owner_id, sale_id, "սխալ")

    row = await db.fetchrow("SELECT voided_at, void_reason, total FROM sales WHERE id = $1",
                            sale_id)
    assert row["voided_at"] is not None
    assert row["void_reason"] == "սխալ"
    assert row["total"] == Decimal("10500.00"), "its numbers are untouched"


async def test_voiding_twice_is_refused(client):
    owner_id, _, _, _, _, sale_id, _ = await _a_closed_shift()
    await corrections.void_sale(owner_id, owner_id, sale_id)

    with pytest.raises(AppError):
        await corrections.void_sale(owner_id, owner_id, sale_id)


# -- amending ----------------------------------------------------------------

async def test_amending_replaces_rather_than_edits(client):
    """The whole design: the original keeps its numbers and points at the new."""
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift(stock=20, sold=3)

    replacement = await corrections.amend_sale(
        owner_id, owner_id, sale_id,
        [{"item_id": item_id, "quantity": 5, "unit_price": "3000.00"}],
        "card", "հաճախորդը 5 հատ վերցրեց",
    )

    original = await db.fetchrow("SELECT total, voided_at, superseded_by_sale_id FROM sales "
                                 "WHERE id = $1", sale_id)
    assert original["total"] == Decimal("10500.00"), "untouched"
    assert original["voided_at"] is not None
    assert original["superseded_by_sale_id"] == replacement

    new = await db.fetchrow("SELECT total, payment_method FROM sales WHERE id = $1", replacement)
    assert new["total"] == Decimal("15000.00")
    assert new["payment_method"] == "card"


async def test_amending_moves_the_stock_by_the_difference(client):
    owner_id, _, _, item_id, _, sale_id, _ = await _a_closed_shift(stock=20, sold=3)

    await corrections.amend_sale(
        owner_id, owner_id, sale_id, [{"item_id": item_id, "quantity": 5}], "cash"
    )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 15


async def test_raising_a_quantity_can_use_the_stock_the_sale_was_holding(client):
    """Undo has to happen before the new basket is checked, or correcting 3 to 4
    would fail on a shelf that only has 1 left."""
    owner_id, _, _, item_id, _, sale_id, _ = await _a_closed_shift(stock=4, sold=3)
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 1

    await corrections.amend_sale(
        owner_id, owner_id, sale_id, [{"item_id": item_id, "quantity": 4}], "cash"
    )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 0


async def test_amending_beyond_the_stock_is_refused_and_changes_nothing(client):
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift(stock=5, sold=3)
    before = await _snapshot(session_id)

    with pytest.raises(AppError):
        await corrections.amend_sale(
            owner_id, owner_id, sale_id, [{"item_id": item_id, "quantity": 99}], "cash"
        )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 2
    assert await db.fetchval("SELECT voided_at FROM sales WHERE id = $1", sale_id) is None
    assert await _snapshot(session_id) == before


# -- adding what was missed --------------------------------------------------

async def test_a_forgotten_sale_can_be_added(client):
    owner_id, _, worker_id, item_id, session_id, _, _ = await _a_closed_shift(stock=20, sold=3)

    await corrections.add_sale(
        owner_id, owner_id, session_id, worker_id,
        [{"item_id": item_id, "quantity": 2, "unit_price": "3500.00"}],
        "card", "մոռացվել էր",
    )

    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 15
    assert (await money_repo.totals_for_session(session_id))["card"] == Decimal("7000.00")


async def test_a_sale_cannot_be_added_for_somebody_who_did_not_work(client):
    """Every sale belongs to somebody, or the per-worker figures are wrong."""
    owner_id, _, _, item_id, session_id, _, _ = await _a_closed_shift()
    stranger, _ = await make_worker(owner_id, "Բ")

    with pytest.raises(AppError):
        await corrections.add_sale(
            owner_id, owner_id, session_id, stranger,
            [{"item_id": item_id, "quantity": 1}], "cash",
        )


# -- money that is not a sale ------------------------------------------------

async def test_an_owner_movement_needs_a_purpose(client):
    owner_id, _, _, _, session_id, _, _ = await _a_closed_shift()

    with pytest.raises(AppError):
        await corrections.add_movement(
            owner_id, owner_id, session_id, "withdrawal", "cash", Decimal("5000"), "  "
        )


async def test_paying_an_influencer_is_recorded_with_its_reason(client):
    owner_id, _, _, _, session_id, _, _ = await _a_closed_shift()

    await corrections.add_movement(
        owner_id, owner_id, session_id, "withdrawal", "cash", Decimal("50000"),
        "բլոգերին վճարված գումար",
    )

    row = await db.fetchrow(
        "SELECT amount, note, created_by FROM cash_movements WHERE kind = 'withdrawal'"
    )
    assert row["amount"] == Decimal("-50000.00")
    assert row["note"] == "բլոգերին վճարված գումար"
    assert row["created_by"] == "owner", "distinguishable from what the system recorded"


async def test_a_sale_movement_cannot_be_deleted_directly(client):
    """It is the record of something that happened; correct the sale instead."""
    owner_id, _, _, _, _, _, _ = await _a_closed_shift()
    movement = await db.fetchval("SELECT id FROM cash_movements WHERE kind = 'sale'")

    with pytest.raises(AppError):
        await corrections.delete_movement(owner_id, owner_id, movement)


async def test_an_owner_movement_can_be_deleted(client):
    owner_id, _, _, _, session_id, _, _ = await _a_closed_shift()
    movement = await corrections.add_movement(
        owner_id, owner_id, session_id, "deposit", "cash", Decimal("1000"), "մանրադրամ"
    )

    await corrections.delete_movement(owner_id, owner_id, movement)

    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE id = $1", movement) == 0


# -- salary ------------------------------------------------------------------

async def test_a_salary_can_be_corrected_and_the_till_follows(client):
    owner_id, _, _, _, session_id, _, shift_id = await _a_closed_shift(salary="8000.00")

    await corrections.set_salary(owner_id, owner_id, shift_id, Decimal("5000"))

    assert await db.fetchval("SELECT salary_paid FROM work_sessions") == Decimal("5000.00")
    assert await db.fetchval(
        "SELECT count(*) FROM cash_movements WHERE kind = 'salary'"
    ) == 1, "replaced, not added to"
    assert await _snapshot(session_id) == Decimal("5500.00")


# -- history and undo --------------------------------------------------------

async def test_every_correction_is_recorded(client):
    owner_id, _, worker_id, item_id, session_id, sale_id, shift_id = await _a_closed_shift()

    await corrections.void_sale(owner_id, owner_id, sale_id, "սխալ")
    await corrections.add_movement(
        owner_id, owner_id, session_id, "withdrawal", "cash", Decimal("100"), "թեստ"
    )

    events = await audit_repo.recent(owner_id)
    assert [e["action"] for e in events] == ["add_movement", "void_sale"]
    assert all(e["summary"] for e in events), "each one reads as a sentence"


async def test_undoing_a_void_puts_the_sale_back(client):
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift(stock=20, sold=3)
    await corrections.void_sale(owner_id, owner_id, sale_id)
    event = await audit_repo.newest_pending(owner_id)

    await corrections.revert(owner_id, owner_id, event["id"])

    assert await db.fetchval("SELECT voided_at FROM sales WHERE id = $1", sale_id) is None
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17
    assert await _snapshot(session_id) == Decimal("2500.00"), "back to where it started"


async def test_undoing_an_amendment_restores_the_original(client):
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift(stock=20, sold=3)
    replacement = await corrections.amend_sale(
        owner_id, owner_id, sale_id, [{"item_id": item_id, "quantity": 5}], "card"
    )
    event = await audit_repo.newest_pending(owner_id)

    await corrections.revert(owner_id, owner_id, event["id"])

    assert await db.fetchval("SELECT count(*) FROM sales WHERE id = $1", replacement) == 0
    original = await db.fetchrow("SELECT voided_at, total FROM sales WHERE id = $1", sale_id)
    assert original["voided_at"] is None
    assert original["total"] == Decimal("10500.00")
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17
    assert await _snapshot(session_id) == Decimal("2500.00")


async def test_undoing_an_added_sale_removes_it_entirely(client):
    owner_id, _, worker_id, item_id, session_id, _, _ = await _a_closed_shift(stock=20, sold=3)
    added = await corrections.add_sale(
        owner_id, owner_id, session_id, worker_id,
        [{"item_id": item_id, "quantity": 2}], "cash",
    )
    event = await audit_repo.newest_pending(owner_id)

    await corrections.revert(owner_id, owner_id, event["id"])

    assert await db.fetchval("SELECT count(*) FROM sales WHERE id = $1", added) == 0
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == 17


async def test_undoing_a_deleted_movement_brings_the_money_back(client):
    owner_id, _, _, _, session_id, _, _ = await _a_closed_shift()
    movement = await corrections.add_movement(
        owner_id, owner_id, session_id, "withdrawal", "cash", Decimal("3000"), "թեստ"
    )
    await corrections.delete_movement(owner_id, owner_id, movement)
    before = await _snapshot(session_id)
    event = await audit_repo.newest_pending(owner_id)

    await corrections.revert(owner_id, owner_id, event["id"])

    assert await _snapshot(session_id) == before - Decimal("3000.00")


async def test_undoing_a_salary_change_restores_the_previous_figure(client):
    owner_id, _, _, _, session_id, _, shift_id = await _a_closed_shift(salary="8000.00")
    await corrections.set_salary(owner_id, owner_id, shift_id, Decimal("5000"))
    event = await audit_repo.newest_pending(owner_id)

    await corrections.revert(owner_id, owner_id, event["id"])

    assert await db.fetchval("SELECT salary_paid FROM work_sessions") == Decimal("8000.00")
    assert await _snapshot(session_id) == Decimal("2500.00")


async def test_undo_is_newest_first(client):
    """Unpicking a change something later depends on would leave the chain
    pointing at rows that no longer mean what the link says."""
    owner_id, _, _, item_id, session_id, sale_id, _ = await _a_closed_shift()
    await corrections.void_sale(owner_id, owner_id, sale_id)
    first = (await audit_repo.recent(owner_id))[0]
    await corrections.add_movement(
        owner_id, owner_id, session_id, "deposit", "cash", Decimal("100"), "թեստ"
    )

    with pytest.raises(AppError) as caught:
        await corrections.revert(owner_id, owner_id, first["id"])

    assert "ավելի ուշ" in caught.value.message


async def test_rewinding_step_by_step_returns_to_the_starting_point(client):
    """Three corrections, undone newest-first, leave the books as they were."""
    owner_id, _, worker_id, item_id, session_id, sale_id, shift_id = await _a_closed_shift(
        stock=20, sold=3
    )
    started_at = await _snapshot(session_id)
    started_stock = await db.fetchval("SELECT count FROM items WHERE id = $1", item_id)

    await corrections.set_salary(owner_id, owner_id, shift_id, Decimal("5000"))
    await corrections.add_sale(
        owner_id, owner_id, session_id, worker_id,
        [{"item_id": item_id, "quantity": 2}], "card",
    )
    await corrections.void_sale(owner_id, owner_id, sale_id)

    for _ in range(3):
        pending = await audit_repo.newest_pending(owner_id)
        await corrections.revert(owner_id, owner_id, pending["id"])

    assert await audit_repo.newest_pending(owner_id) is None
    assert await _snapshot(session_id) == started_at
    assert await db.fetchval("SELECT count FROM items WHERE id = $1", item_id) == started_stock


async def test_an_undone_correction_cannot_be_undone_again(client):
    owner_id, _, _, _, _, sale_id, _ = await _a_closed_shift()
    await corrections.void_sale(owner_id, owner_id, sale_id)
    event = await audit_repo.newest_pending(owner_id)
    await corrections.revert(owner_id, owner_id, event["id"])

    with pytest.raises(AppError):
        await corrections.revert(owner_id, owner_id, event["id"])


async def test_another_owners_correction_is_not_reachable(client):
    owner_id, _, _, _, _, sale_id, _ = await _a_closed_shift()
    intruder = await make_owner()

    with pytest.raises(AppError) as caught:
        await corrections.void_sale(intruder, intruder, sale_id)

    assert caught.value.status == 404
