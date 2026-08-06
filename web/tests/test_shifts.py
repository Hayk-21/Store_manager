"""Opening a store, working a shift, paying the salary, closing the till.

Requirements 5 and 8, plus the store-session accounting model: the period runs
from "a worker pressed open" to "a worker pressed close", not from midnight to
midnight.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.errors import BotError
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    make_owner,
    make_store,
    make_worker,
)

FAR_LAT = YEREVAN_LAT + 0.0072
KEY = "idem-key-open-0001"


async def _worker_of(owner_id: int, worker_id: int, salary: str = "8000.00"):
    return shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_per_shift=Decimal(salary)
    )


async def _setup(salary: str = "8000.00", radius_m: int = 120):
    owner_id = await make_owner()
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=radius_m)
    worker_id, telegram_id = await make_worker(owner_id, salary_per_shift=salary)
    return owner_id, store_id, await _worker_of(owner_id, worker_id, salary), telegram_id


# -- opening -----------------------------------------------------------------

async def test_opening_creates_a_store_session_and_a_shift(client):
    owner_id, store_id, worker, _ = await _setup()

    result = await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    assert result["duplicate"] is False
    assert result["session"]["store_id"] == store_id
    assert result["session"]["distance_m"] == 0
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 1
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 1


async def test_opening_out_of_range_attaches_nobody(client):
    owner_id, store_id, worker, _ = await _setup()

    with pytest.raises(BotError) as caught:
        await shifts_service.open_store(worker, FAR_LAT, YEREVAN_LNG, 20, KEY)

    assert caught.value.code == "no_store_in_range"
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 0
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 0


async def test_opening_twice_with_the_same_key_is_one_shift(client):
    _, _, worker, _ = await _setup()

    first = await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    second = await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    assert first["duplicate"] is False and second["duplicate"] is True
    assert first["session"]["id"] == second["session"]["id"]
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 1


async def test_opening_twice_with_different_keys_is_refused(client):
    _, _, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    with pytest.raises(BotError) as caught:
        await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-other-1")

    assert caught.value.code == "session_already_open"
    assert caught.value.status == 409
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 1


async def test_two_workers_share_one_store_session(client):
    """The user asked for several people on shift together: the first to arrive
    opens the till, the second joins it."""
    owner_id, store_id, first, _ = await _setup()
    second_id, _ = await make_worker(owner_id, salary_per_shift="6000.00")
    second = await _worker_of(owner_id, second_id, "6000.00")

    a = await shifts_service.open_store(first, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-aaaa")
    b = await shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-bbbb")

    assert a["session"]["store_session_id"] == b["session"]["store_session_id"]
    assert await db.fetchval("SELECT count(*) FROM store_sessions") == 1
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 2


# -- salary ------------------------------------------------------------------

async def test_ending_a_shift_takes_the_salary_out_of_cash(client):
    """Requirement 5."""
    _, _, worker, _ = await _setup(salary="8000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    result = await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    assert result["summary"]["salary_deducted"] == "8000.00"
    movement = await db.fetchrow(
        "SELECT method, kind, amount FROM cash_movements WHERE kind = 'salary'"
    )
    assert movement["method"] == "cash"
    assert movement["amount"] == Decimal("-8000.00")


async def test_the_salary_is_snapshotted_and_survives_a_later_rate_change(client):
    """What a past shift cost must not change when the rate does."""
    owner_id, _, worker, _ = await _setup(salary="8000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    await db.execute("UPDATE workers SET salary_per_shift = 99999.00 WHERE id = $1", worker.id)

    paid = await db.fetchval("SELECT salary_paid FROM work_sessions")
    assert paid == Decimal("8000.00")


async def test_a_shift_can_only_ever_pay_its_salary_once(client):
    import asyncpg

    _, _, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    shift = await db.fetchrow("SELECT * FROM work_sessions")

    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await db.execute(
            """
            INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind,
                                        amount, work_session_id)
            VALUES ($1, $2, $3, 'cash', 'salary', -1.00, $4)
            """,
            shift["owner_id"], shift["store_id"], shift["store_session_id"], shift["id"],
        )


async def test_a_zero_salary_writes_no_movement(client):
    _, _, worker, _ = await _setup(salary="0.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'salary'") == 0


async def test_ending_a_shift_twice_is_refused_after_the_replay_window(client):
    _, _, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    replay = await shifts_service.end_shift(worker, None, None, "idem-key-end-01")
    assert replay["duplicate"] is True

    with pytest.raises(BotError) as caught:
        await shifts_service.end_shift(worker, None, None, "idem-key-end-02")
    assert caught.value.code == "no_open_session"


# -- closing the till --------------------------------------------------------

async def test_the_last_worker_out_closes_the_store(client):
    _, store_id, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    result = await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    assert result["summary"]["store_closed"] is True
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 0


async def test_the_store_stays_open_while_somebody_is_still_working(client):
    owner_id, store_id, first, _ = await _setup()
    second_id, _ = await make_worker(owner_id)
    second = await _worker_of(owner_id, second_id)
    await shifts_service.open_store(first, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-aaaa")
    await shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-bbbb")

    result = await shifts_service.end_shift(first, None, None, "idem-key-end-01")

    assert result["summary"]["store_closed"] is False
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 1


async def test_closing_the_store_ends_everyone_and_pays_every_salary(client):
    owner_id, store_id, first, _ = await _setup(salary="8000.00")
    second_id, _ = await make_worker(owner_id, salary_per_shift="6000.00")
    second = await _worker_of(owner_id, second_id, "6000.00")
    await shifts_service.open_store(first, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-aaaa")
    await shifts_service.open_store(second, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-bbbb")

    await shifts_service.close_store(first, "idem-key-close-1")

    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0
    paid = await db.fetchval("SELECT -sum(amount) FROM cash_movements WHERE kind = 'salary'")
    assert paid == Decimal("14000.00")


async def test_closing_snapshots_the_till_and_resets_the_visible_total(client):
    """The behaviour the user asked for: cash is handed over at close."""
    from app.repo import money as money_repo

    _, store_id, worker, _ = await _setup(salary="3000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    session_id = await db.fetchval("SELECT id FROM store_sessions")
    # Pretend a sale happened.
    await db.execute(
        """
        INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
        SELECT owner_id, store_id, id, 'cash', 'sale', 10000.00 FROM store_sessions WHERE id = $1
        """,
        session_id,
    )

    before = await money_repo.totals_for_session(session_id)
    assert before["cash"] == Decimal("10000.00")

    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    closed = await db.fetchrow("SELECT * FROM store_sessions WHERE id = $1", session_id)
    assert closed["closed_at"] is not None
    assert closed["cash_at_close"] == Decimal("7000.00")  # 10000 sale - 3000 salary
    assert closed["salaries_at_close"] == Decimal("3000.00")

    # And the store now reads zero, because "current" means "the open session".
    footer = await money_repo.totals_by_store(closed["owner_id"])
    row = next(r for r in footer if r["id"] == store_id)
    assert row["store_session_id"] is None
    assert row["cash"] == Decimal("0")


async def test_the_store_can_be_opened_again_and_starts_from_zero(client):
    from app.repo import money as money_repo

    owner_id, store_id, worker, _ = await _setup(salary="0.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-aaaa")
    first_session = await db.fetchval("SELECT id FROM store_sessions")
    await db.execute(
        """
        INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
        VALUES ($1, $2, $3, 'cash', 'sale', 50000.00)
        """,
        owner_id, store_id, first_session,
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-bbbb")

    footer = await money_repo.totals_by_store(owner_id)
    row = next(r for r in footer if r["id"] == store_id)
    assert row["store_session_id"] != first_session
    assert row["cash"] == Decimal("0"), "yesterday's takings must not follow the store around"


async def test_cash_is_allowed_to_go_negative(client):
    """A store can genuinely owe wages it has not taken in. Clamping would hide it."""
    _, _, worker, _ = await _setup(salary="8000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)

    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("-8000.00")


# -- housekeeping ------------------------------------------------------------

async def test_the_owner_can_force_close_a_forgotten_shift(client):
    owner_id, _, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await shifts_service.close_store_session_as_owner(owner_id, session_id)

    row = await db.fetchrow("SELECT closed_at, closed_by FROM store_sessions WHERE id = $1",
                            session_id)
    assert row["closed_at"] is not None
    assert row["closed_by"] == "owner"
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0


async def test_a_forgotten_store_is_auto_closed_and_the_salary_still_paid(client):
    """Without this, a worker who never pressed "close" could not start tomorrow."""
    _, _, worker, _ = await _setup(salary="8000.00")
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    await db.execute("UPDATE store_sessions SET opened_at = now() - interval '17 hours'")

    closed = await shifts_service.auto_close_stale()

    assert closed == 1
    row = await db.fetchrow("SELECT closed_by, salaries_at_close FROM store_sessions")
    assert row["closed_by"] == "auto"
    assert row["salaries_at_close"] == Decimal("8000.00")
    # And the worker is free to start again.
    assert await db.fetchval("SELECT count(*) FROM work_sessions WHERE ended_at IS NULL") == 0


async def test_a_store_open_for_a_normal_day_is_left_alone(client):
    _, _, worker, _ = await _setup()
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, KEY)
    await db.execute("UPDATE store_sessions SET opened_at = now() - interval '9 hours'")

    assert await shifts_service.auto_close_stale() == 0
    assert await db.fetchval("SELECT count(*) FROM store_sessions WHERE closed_at IS NULL") == 1
