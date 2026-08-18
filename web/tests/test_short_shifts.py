"""A shift wage, and what a shift under eight hours comes to.

The figure on the worker's record is a day's pay. Somebody who left after two
hours has not worked a day, so it is halved. One step rather than pay-by-the-
minute, because the wage is agreed as a day rate and billing it by the second
would turn every late start into an argument about four minutes.

Applied in one place — the function every close path goes through — so the worker
pressing "end my shift", the write-up, the last one out closing the shop, the owner
forcing it and the auto-close cannot disagree about what a day's work is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db import db
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    make_item,
    make_owner,
    make_store,
    make_worker,
)

WAGE = "8000.00"


async def _on_shift(salary: str = WAGE, period: str = "shift", till: str = "50000.00"):
    """A shop with money in the drawer, because these tests are about the *size* of a
    wage rather than about whether the till can cover it. A shop with nothing in it
    owes the wage instead of paying it — see test_unpaid_wages.py."""
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, "Խանութ 1", lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    await db.execute(
        "UPDATE stores SET till_balance = $2 WHERE id = $1", store_id, Decimal(till)
    )
    worker_id, telegram_id = await make_worker(
        owner_id, "Անի", salary_amount=salary, salary_period=period
    )
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի",
        salary_amount=Decimal(salary), salary_period=period,
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-open-1", 900)
    item_id = await make_item(owner_id, store_id, "HQD Cuvie", count=50)
    return owner_id, store_id, worker, item_id, telegram_id


async def _backdate(worker_id: int, hours: float) -> None:
    """Pretend the shift started that long ago.

    The clock is the only input the rule has, and a test cannot wait eight hours.
    """
    await db.execute(
        "UPDATE work_sessions SET started_at = $2 WHERE worker_id = $1 AND ended_at IS NULL",
        worker_id,
        datetime.now(UTC) - timedelta(hours=hours),
    )


async def _paid() -> Decimal:
    return Decimal(await db.fetchval("SELECT salary_paid FROM work_sessions"))


# -- the rule ----------------------------------------------------------------

def test_a_full_shift_is_paid_in_full():
    assert shifts_service.salary_for_hours_worked(
        Decimal(WAGE), datetime.now(UTC) - timedelta(hours=9)
    ) == Decimal(WAGE)


def test_exactly_eight_hours_counts_as_full():
    """The boundary belongs to the worker. Somebody who did the whole shift and
    clocked out on the minute has not earned half of it."""
    assert shifts_service.salary_for_hours_worked(
        Decimal(WAGE), datetime.now(UTC) - timedelta(hours=8)
    ) == Decimal(WAGE)


def test_a_short_shift_is_halved():
    assert shifts_service.salary_for_hours_worked(
        Decimal(WAGE), datetime.now(UTC) - timedelta(hours=3)
    ) == Decimal("4000.00")


def test_half_of_nothing_is_still_nothing():
    """A monthly worker's shift wage is zero, and half of zero must not become a
    payment out of the till."""
    assert shifts_service.salary_for_hours_worked(
        Decimal("0.00"), datetime.now(UTC) - timedelta(hours=1)
    ) == Decimal("0.00")


def test_a_missing_start_time_reads_as_a_full_shift():
    """Guessing against the worker over a bookkeeping gap is the wrong way round."""
    assert shifts_service.salary_for_hours_worked(Decimal(WAGE), None) == Decimal(WAGE)


# -- ending a shift ----------------------------------------------------------

async def test_ending_a_short_shift_pays_half_out_of_the_till(client):
    _, _, worker, _, _ = await _on_shift()
    await _backdate(worker.id, 2)

    result = await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert result["summary"]["salary_deducted"] == "4000.00"
    assert await _paid() == Decimal("4000.00")
    assert await db.fetchval(
        "SELECT amount FROM cash_movements WHERE kind = 'salary'"
    ) == Decimal("-4000.00")


async def test_ending_a_full_shift_pays_all_of_it(client):
    _, _, worker, _, _ = await _on_shift()
    await _backdate(worker.id, 9)

    result = await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert result["summary"]["salary_deducted"] == "8000.00"
    assert await _paid() == Decimal("8000.00")


async def test_the_worker_is_told_why_it_is_half(client):
    """Being paid less than expected with no explanation is a phone call."""
    _, _, worker, _, _ = await _on_shift()
    await _backdate(worker.id, 2)

    result = await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert result["summary"]["salary_halved"] is True
    assert result["summary"]["full_shift_hours"] == 8


async def test_a_full_shift_says_nothing_about_halving(client):
    _, _, worker, _, _ = await _on_shift()
    await _backdate(worker.id, 9)

    result = await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert result["summary"]["salary_halved"] is False


async def test_a_monthly_worker_is_unaffected(client):
    """Their wage is not settled out of the till at all, so there is nothing to
    halve and no note to make about it."""
    _, _, worker, _, _ = await _on_shift(period="month")
    await _backdate(worker.id, 1)

    result = await shifts_service.end_shift(worker, None, None, "idem-end-1")

    assert result["summary"]["salary_deducted"] == "0.00"
    assert result["summary"]["salary_halved"] is False
    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'salary'") == 0


# -- the other ways a shift ends ---------------------------------------------

async def test_the_write_up_halves_it_too(client):
    """Five call sites, one rule. This is the one a cashier actually uses."""
    _, _, worker, item_id, _ = await _on_shift()
    await _backdate(worker.id, 3)

    await shifts_service.close_out_shift(
        worker,
        [{"item_id": item_id, "quantity": 1, "unit_price": "3500.00",
          "payment_method": "cash"}],
        "idem-close-1",
        counted=Decimal("0"),
    )

    assert await _paid() == Decimal("4000.00")


async def test_the_owner_forcing_the_shop_closed_halves_it_too(client):
    _, _, worker, _, _ = await _on_shift()
    await _backdate(worker.id, 1)
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await shifts_service.close_store_session_as_owner(worker.owner_id, session_id)

    assert await _paid() == Decimal("4000.00")
