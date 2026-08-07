"""The ledger: the owner's own movements, and the invariants the schema enforces."""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import pytest

from app.db import db
from app.errors import AppError
from app.repo import money as money_repo
from app.services import money as money_service
from app.services import shifts as shifts_service
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_owner,
    make_store,
    make_worker,
)


async def _open_store(salary: str = "0.00"):
    owner_id = await make_owner("@ownerhandle")
    store_id = await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    worker_id, _ = await make_worker(owner_id, "Անի", salary_amount=salary)
    worker = shifts_service.Worker(
        id=worker_id, owner_id=owner_id, name="Անի", salary_amount=Decimal(salary)
    )
    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-1", 900)
    session_id = await db.fetchval("SELECT id FROM store_sessions WHERE closed_at IS NULL")
    return owner_id, store_id, worker, session_id


async def test_taking_money_out_lowers_the_till(client):
    owner_id, store_id, _, session_id = await _open_store()
    await db.execute(
        """
        INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
        VALUES ($1, $2, $3, 'cash', 'sale', 50000.00)
        """,
        owner_id, store_id, session_id,
    )

    await money_service.record_movement(
        owner_id, store_id, "cash", "withdrawal", Decimal("20000.00"), "բանկ"
    )

    totals = await money_repo.totals_for_session(session_id)
    assert totals["cash"] == Decimal("30000.00")
    assert totals["withdrawn"] == Decimal("20000.00")


async def test_putting_money_in_raises_the_till(client):
    owner_id, store_id, _, session_id = await _open_store()

    await money_service.record_movement(
        owner_id, store_id, "cash", "deposit", Decimal("5000.00"), "մանրադրամ"
    )

    assert (await money_repo.totals_for_session(session_id))["cash"] == Decimal("5000.00")


async def test_money_cannot_be_moved_while_the_store_is_closed(client):
    """The till was settled and handed over at close; there is nothing to take."""
    owner_id, store_id, worker, _ = await _open_store()
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    with pytest.raises(AppError) as caught:
        await money_service.record_movement(
            owner_id, store_id, "cash", "withdrawal", Decimal("100.00")
        )

    assert "փակ" in caught.value.message


async def test_a_negative_amount_is_refused(client):
    owner_id, store_id, _, _ = await _open_store()

    with pytest.raises(AppError):
        await money_service.record_movement(
            owner_id, store_id, "cash", "withdrawal", Decimal("-100.00")
        )


async def test_totals_are_decimals_not_floats(client):
    """A float would already have lost digits by the time it reached a template."""
    owner_id, store_id, _, session_id = await _open_store()
    await money_service.record_movement(
        owner_id, store_id, "cash", "deposit", Decimal("0.10")
    )
    await money_service.record_movement(
        owner_id, store_id, "cash", "deposit", Decimal("0.20")
    )

    totals = await money_repo.totals_for_session(session_id)
    assert isinstance(totals["cash"], Decimal)
    assert totals["cash"] == Decimal("0.30")


async def test_one_sessions_money_never_leaks_into_another(client):
    """The property behind "closing resets the till"."""
    owner_id, store_id, worker, first_session = await _open_store()
    await db.execute(
        """
        INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
        VALUES ($1, $2, $3, 'cash', 'sale', 40000.00)
        """,
        owner_id, store_id, first_session,
    )
    await shifts_service.end_shift(worker, None, None, "idem-key-end-01")

    await shifts_service.open_store(worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-2", 900)
    second_session = await db.fetchval("SELECT id FROM store_sessions WHERE closed_at IS NULL")

    assert (await money_repo.totals_for_session(second_session))["cash"] == Decimal("0")
    # And the closed one is still readable for the report.
    assert (await money_repo.totals_for_session(first_session))["cash"] == Decimal("40000.00")


# -- schema invariants -------------------------------------------------------

async def test_the_schema_refuses_a_positive_salary(client):
    owner_id, store_id, _, session_id = await _open_store()
    shift = await db.fetchval("SELECT id FROM work_sessions")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            """
            INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind,
                                        amount, work_session_id)
            VALUES ($1, $2, $3, 'cash', 'salary', 8000.00, $4)
            """,
            owner_id, store_id, session_id, shift,
        )


async def test_the_schema_refuses_a_negative_sale(client):
    owner_id, store_id, _, session_id = await _open_store()

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            """
            INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
            VALUES ($1, $2, $3, 'card', 'sale', -100.00)
            """,
            owner_id, store_id, session_id,
        )


async def test_a_salary_can_only_be_paid_in_cash(client):
    """Card money is a separate pot; wages come out of the drawer."""
    owner_id, store_id, _, session_id = await _open_store()
    shift = await db.fetchval("SELECT id FROM work_sessions")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await db.execute(
            """
            INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind,
                                        amount, work_session_id)
            VALUES ($1, $2, $3, 'card', 'salary', -8000.00, $4)
            """,
            owner_id, store_id, session_id, shift,
        )


# -- the page ----------------------------------------------------------------

async def test_the_status_block_shows_who_is_working_and_the_till(client):
    """Requirement 3, through HTTP."""
    owner_id, store_id, worker, session_id = await _open_store(salary="8000.00")
    await login(client, "@ownerhandle")
    await db.execute(
        """
        INSERT INTO cash_movements (owner_id, store_id, store_session_id, method, kind, amount)
        VALUES ($1, $2, $3, 'card', 'sale', 12000.00)
        """,
        owner_id, store_id, session_id,
    )

    response = await client.get(f"/partials/stores/{store_id}/status")

    assert response.status_code == 200
    assert "Անի" in response.text
    assert "12,000.00" in response.text
    assert "Հերթափոխի մեջ" in response.text


async def test_the_owner_can_force_close_from_the_page(client):
    owner_id, store_id, _, session_id = await _open_store(salary="8000.00")
    await login(client, "@ownerhandle")
    token = await db.fetchval("SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id)

    response = await client.post(
        f"/store-sessions/{session_id}/close", data={"csrf_token": token}
    )

    assert response.status_code == 200
    assert "Հերթափոխը բաց չէ" in response.text
    row = await db.fetchrow("SELECT closed_by, salaries_at_close FROM store_sessions")
    assert row["closed_by"] == "owner"
    assert row["salaries_at_close"] == Decimal("8000.00")


async def test_another_owners_session_cannot_be_closed(client):
    await make_owner("@ownerhandle")
    other_owner = await make_owner()
    await make_store(other_owner, lat=YEREVAN_LAT, lng=YEREVAN_LNG)
    other_worker_id, _ = await make_worker(other_owner)
    other_worker = shifts_service.Worker(
        id=other_worker_id, owner_id=other_owner, name="Բ", salary_amount=Decimal("0")
    )
    await shifts_service.open_store(other_worker, YEREVAN_LAT, YEREVAN_LNG, 20, "idem-key-open-9", 900)
    their_session = await db.fetchval("SELECT id FROM store_sessions")

    await login(client, "@ownerhandle")
    token = await db.fetchval("SELECT csrf_token FROM auth_sessions LIMIT 1")
    response = await client.post(
        f"/store-sessions/{their_session}/close", data={"csrf_token": token}
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT closed_at FROM store_sessions") is None


# -- workers page ------------------------------------------------------------

async def test_a_worker_can_be_registered_from_the_page(client):
    owner_id = await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    token = await db.fetchval("SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id)

    response = await client.post(
        "/workers",
        data={"telegram_username": "@justhayk", "salary_amount": "5000",
              "salary_period": "shift", "csrf_token": token},
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT telegram_username FROM workers") == "justhayk"


async def test_the_workers_page_shows_where_someone_is_working(client):
    owner_id, store_id, _, _ = await _open_store()
    await login(client, "@ownerhandle")

    response = await client.get("/workers")

    assert response.status_code == 200
    assert "Անի" in response.text
    store_name = await db.fetchval("SELECT name FROM stores WHERE id = $1", store_id)
    assert store_name in response.text
