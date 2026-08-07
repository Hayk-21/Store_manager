"""Registering a worker by @username, and paying them per shift or per month.

The owner writes a handle and a salary. The numeric id and the name both arrive
by themselves the first time that person messages the bot. Registration stays
closed either way: an account matching nothing on /workers is refused, which is
the whole access-control story for the bot.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db import db
from app.repo import workers as workers_repo
from tests.factories import (
    YEREVAN_LAT,
    YEREVAN_LNG,
    login,
    make_owner,
    make_store,
    make_worker,
)

BASE = "/api/bot/v1"
TG_ID = 555000777


async def _signed_in(client) -> tuple[int, str]:
    owner_id = await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    token = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id
    )
    return owner_id, token


async def _register(client, token, handle="@justhayk", salary="8000", period="shift", **extra):
    return await client.post(
        "/workers",
        data={"telegram_username": handle, "salary_amount": salary,
              "salary_period": period, "csrf_token": token, **extra},
    )


# -- registering -------------------------------------------------------------

async def test_a_handle_and_a_salary_are_all_that_is_needed(client):
    owner_id, token = await _signed_in(client)

    response = await _register(client, token)

    assert response.status_code == 303
    row = await db.fetchrow("SELECT * FROM workers WHERE owner_id = $1", owner_id)
    assert row["telegram_username"] == "justhayk", "the @ is stripped"
    assert row["telegram_id"] is None, "not bound until they make contact"
    assert row["name"] is None
    assert row["salary_amount"] == Decimal("8000.00")
    assert row["salary_period"] == "shift"


@pytest.mark.parametrize("typed", ["@justhayk", "justhayk", "  @JustHayk  "])
async def test_the_handle_is_accepted_however_it_is_typed(client, typed):
    _, token = await _signed_in(client)

    assert (await _register(client, token, typed)).status_code == 303

    stored = await db.fetchval("SELECT telegram_username FROM workers")
    assert stored.lower() == "justhayk"


@pytest.mark.parametrize("bad", ["@ab", "@has space", "@dash-es", "@" + "x" * 40, "@"])
async def test_a_handle_telegram_could_never_issue_is_refused(client, bad):
    _, token = await _signed_in(client)

    response = await _register(client, token, bad)

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_an_unbound_worker_is_shown_by_their_handle_never_as_a_blank(client):
    owner_id, token = await _signed_in(client)
    await _register(client, token)

    listed = await workers_repo.list_for_owner(owner_id)

    assert listed[0]["name"] == "@justhayk"


async def test_two_owners_cannot_claim_one_handle(client):
    """It is what resolves an unbound worker to exactly one owner."""
    _, token = await _signed_in(client)
    other = await make_owner()
    await db.execute(
        "INSERT INTO workers (owner_id, telegram_username, salary_amount) "
        "VALUES ($1, 'justhayk', 0)",
        other,
    )

    response = await _register(client, token)

    assert response.status_code == 422
    assert "արդեն գրանցված" in response.text


# -- binding on first contact ------------------------------------------------

async def test_first_contact_binds_the_handle_to_the_account(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await _register(client, token)

    response = await client.get(
        f"{BASE}/me",
        params={"telegram_id": TG_ID, "telegram_username": "justhayk",
                "telegram_name": "Հայկ Սաքոյան"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert response.json()["worker"]["name"] == "Հայկ Սաքոյան"
    row = await db.fetchrow("SELECT telegram_id, telegram_name, name FROM workers")
    assert row["telegram_id"] == TG_ID, "bound"
    assert row["telegram_name"] == "Հայկ Սաքոյան", "and named"
    assert row["name"] is None, "the owner's own field stays untouched"


async def test_the_binding_is_case_insensitive(client, bot_headers):
    """Telegram treats @JustHayk and @justhayk as one account."""
    _, token = await _signed_in(client)
    await _register(client, token, "@JustHayk")

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": TG_ID, "telegram_username": "justhayk"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT telegram_id FROM workers") == TG_ID


async def test_once_bound_the_id_is_what_identifies_them(client, bot_headers):
    """A username can be changed or handed on; the numeric id cannot."""
    _, token = await _signed_in(client)
    await _register(client, token)
    await client.get(
        f"{BASE}/me", params={"telegram_id": TG_ID, "telegram_username": "justhayk"},
        headers=bot_headers,
    )

    # They changed their Telegram handle. They are still the same worker.
    response = await client.get(
        f"{BASE}/me", params={"telegram_id": TG_ID, "telegram_username": "somethingelse"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM workers") == 1


async def test_somebody_else_claiming_a_bound_handle_is_refused(client, bot_headers):
    """The impersonation case: the handle is already spoken for."""
    _, token = await _signed_in(client)
    await _register(client, token)
    await client.get(
        f"{BASE}/me", params={"telegram_id": TG_ID, "telegram_username": "justhayk"},
        headers=bot_headers,
    )

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": 111222333, "telegram_username": "justhayk"},
        headers=bot_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_worker"
    assert await db.fetchval("SELECT telegram_id FROM workers") == TG_ID


async def test_an_unregistered_handle_is_refused_and_leaves_no_trace(client, bot_headers):
    await _signed_in(client)

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": 999888777, "telegram_username": "stranger"},
        headers=bot_headers,
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_the_owner_can_unbind_a_wrongly_claimed_handle(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await _register(client, token)
    await client.get(
        f"{BASE}/me", params={"telegram_id": TG_ID, "telegram_username": "justhayk"},
        headers=bot_headers,
    )
    worker_id = await db.fetchval("SELECT id FROM workers")

    response = await client.post(
        f"/workers/{worker_id}/unbind", data={"csrf_token": token}
    )

    assert response.status_code == 303
    assert await db.fetchval("SELECT telegram_id FROM workers") is None
    # And the right person can now claim it.
    await client.get(
        f"{BASE}/me", params={"telegram_id": 444555666, "telegram_username": "justhayk"},
        headers=bot_headers,
    )
    assert await db.fetchval("SELECT telegram_id FROM workers") == 444555666


async def test_binding_also_happens_when_opening_a_store(client, bot_headers):
    """A worker whose very first action is opening the shop still gets bound."""
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token)

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "telegram_name": "Գոռ", "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20,
              "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    row = await db.fetchrow("SELECT telegram_id, telegram_name FROM workers")
    assert row["telegram_id"] == TG_ID
    assert row["telegram_name"] == "Գոռ"


# -- names -------------------------------------------------------------------

async def test_a_name_the_owner_typed_beats_the_telegram_one(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await _register(client, token, name="Անի (գիշերային)")

    await client.get(
        f"{BASE}/me",
        params={"telegram_id": TG_ID, "telegram_username": "justhayk",
                "telegram_name": "nickname123"},
        headers=bot_headers,
    )

    listed = await workers_repo.list_for_owner(owner_id)
    assert listed[0]["name"] == "Անի (գիշերային)"
    assert listed[0]["telegram_name"] == "nickname123", "recorded, just not preferred"


async def test_a_changed_profile_name_is_picked_up(client, bot_headers):
    _, token = await _signed_in(client)
    await _register(client, token)

    for name in ("Անի", "Անի Հակոբյան"):
        await client.get(
            f"{BASE}/me",
            params={"telegram_id": TG_ID, "telegram_username": "justhayk",
                    "telegram_name": name},
            headers=bot_headers,
        )

    assert await db.fetchval("SELECT telegram_name FROM workers") == "Անի Հակոբյան"


async def test_the_name_shows_up_in_reports(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="0")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk", "telegram_name": "Գոռ",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    detail = await client.get(f"/reports?store_session_id={session_id}")

    assert "Գոռ" in detail.text


async def test_a_deactivated_worker_is_refused_and_not_renamed(client, bot_headers):
    owner_id = await make_owner("@ownerhandle")
    worker_id, telegram_id = await make_worker(owner_id, is_active=False)
    await db.execute("UPDATE workers SET telegram_name = 'old' WHERE id = $1", worker_id)

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": telegram_id, "telegram_name": "new"},
        headers=bot_headers,
    )

    assert response.status_code == 403
    assert await db.fetchval("SELECT telegram_name FROM workers") == "old"


# -- salary period -----------------------------------------------------------

async def test_a_per_shift_worker_is_paid_out_of_the_till(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="8000", period="shift")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )

    body = (await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": TG_ID, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )).json()

    assert body["summary"]["salary_deducted"] == "8000.00"
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("-8000.00")


async def test_a_monthly_worker_costs_the_till_nothing_at_shift_end(client, bot_headers):
    """A monthly wage is paid separately; ending a shift must not raid the drawer."""
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="200000", period="month")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )

    body = (await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": TG_ID, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )).json()

    assert body["summary"]["salary_deducted"] == "0.00"
    assert await db.fetchval("SELECT count(*) FROM cash_movements WHERE kind = 'salary'") == 0
    assert await db.fetchval("SELECT cash_at_close FROM store_sessions") == Decimal("0.00")


async def test_a_monthly_worker_is_not_paid_by_a_forced_close_either(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="200000", period="month")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    await client.post(f"/store-sessions/{session_id}/close", data={"csrf_token": token})

    assert await db.fetchval("SELECT salaries_at_close FROM store_sessions") == Decimal("0.00")
    assert await db.fetchval("SELECT salary_paid FROM work_sessions") == Decimal("0.00")


async def test_the_period_can_be_changed_later(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await _register(client, token, salary="8000", period="shift")
    worker_id = await db.fetchval("SELECT id FROM workers")

    response = await client.post(
        f"/workers/{worker_id}",
        data={"telegram_username": "@justhayk", "salary_amount": "250000",
              "salary_period": "month", "is_active": "1", "csrf_token": token},
    )

    assert response.status_code == 303
    row = await db.fetchrow("SELECT salary_amount, salary_period FROM workers")
    assert row["salary_amount"] == Decimal("250000.00")
    assert row["salary_period"] == "month"


async def test_an_unknown_period_is_refused(client):
    _, token = await _signed_in(client)

    response = await _register(client, token, period="fortnightly")

    assert response.status_code == 422
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


# -- removing -----------------------------------------------------------------

async def test_a_worker_who_never_started_is_removed_outright(client):
    _, token = await _signed_in(client)
    await _register(client, token)
    worker_id = await db.fetchval("SELECT id FROM workers")

    response = await client.post(f"/workers/{worker_id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_removing_someone_who_worked_keeps_their_history(client, bot_headers):
    """The foreign keys cascade, so a real DELETE would rewrite past reports."""
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="0")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk", "telegram_name": "Գոռ",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": TG_ID, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )
    worker_id = await db.fetchval("SELECT id FROM workers")
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    response = await client.post(f"/workers/{worker_id}/delete", data={"csrf_token": token})

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM work_sessions") == 1, "the shift survives"
    # And the report still names them.
    detail = await client.get(f"/reports?store_session_id={session_id}")
    assert "Գոռ" in detail.text


async def test_a_removed_worker_is_off_the_list(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="0")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": TG_ID, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )
    worker_id = await db.fetchval("SELECT id FROM workers")
    await client.post(f"/workers/{worker_id}/delete", data={"csrf_token": token})

    page = await client.get("/workers")

    assert await workers_repo.list_for_owner(owner_id) == []
    # The handle only survives as the placeholder in the empty "new worker" form.
    assert 'value="@justhayk"' not in page.text
    assert "Աշխատող դեռ չկա" in page.text


async def test_removing_a_worker_frees_their_handle_for_the_next_person(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="0")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    await client.post(
        f"{BASE}/shift/end",
        json={"telegram_id": TG_ID, "idempotency_key": "idem-key-end-01"},
        headers=bot_headers,
    )
    worker_id = await db.fetchval("SELECT id FROM workers")
    await client.post(f"/workers/{worker_id}/delete", data={"csrf_token": token})

    assert (await _register(client, token)).status_code == 303


async def test_somebody_on_shift_cannot_be_removed(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await _register(client, token, salary="0")
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": TG_ID, "telegram_username": "justhayk",
              "lat": YEREVAN_LAT, "lng": YEREVAN_LNG, "accuracy_m": 20, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    worker_id = await db.fetchval("SELECT id FROM workers")

    response = await client.post(f"/workers/{worker_id}/delete", data={"csrf_token": token})

    assert response.status_code == 422
    assert await db.fetchval("SELECT archived_at FROM workers") is None


async def test_the_workers_page_shows_the_handle_and_the_period(client):
    owner_id, token = await _signed_in(client)
    await _register(client, token, salary="200000", period="month")

    page = await client.get("/workers")

    assert "@justhayk" in page.text
    assert "Ամսվա վերջում" in page.text
    assert "չկապված" in page.text, "not bound to an account yet"
