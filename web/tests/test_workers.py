"""Registering a worker.

The owner types a Telegram id and a salary. The name arrives by itself the first
time that person uses the bot. Registration stays closed either way: an id that
is not in the table is refused, which is the whole access-control story for the
bot.
"""

from __future__ import annotations

from decimal import Decimal

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


async def _signed_in(client) -> tuple[int, str]:
    owner_id = await make_owner("owner@example.com")
    await login(client, "owner@example.com")
    token = await db.fetchval(
        "SELECT csrf_token FROM auth_sessions WHERE user_id = $1", owner_id
    )
    return owner_id, token


async def test_a_worker_can_be_registered_with_only_an_id_and_a_salary(client):
    owner_id, token = await _signed_in(client)

    response = await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "8000", "csrf_token": token},
    )

    assert response.status_code == 303
    row = await db.fetchrow("SELECT * FROM workers WHERE owner_id = $1", owner_id)
    assert row["telegram_id"] == 555000777
    assert row["name"] is None
    assert row["telegram_name"] is None
    assert row["salary_per_shift"] == Decimal("8000.00")


async def test_an_unnamed_worker_is_shown_as_their_id_never_as_a_blank(client):
    owner_id, token = await _signed_in(client)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0", "csrf_token": token},
    )

    listed = await workers_repo.list_for_owner(owner_id)

    assert listed[0]["name"] == "ID 555000777"


async def test_the_name_arrives_from_telegram_on_first_contact(client, bot_headers):
    """The owner never types a name; the bot reports the profile name."""
    owner_id, token = await _signed_in(client)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "8000", "csrf_token": token},
    )

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": 555000777, "telegram_name": "Անի Հակոբյան"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    assert response.json()["worker"]["name"] == "Անի Հակոբյան"
    stored = await db.fetchrow("SELECT name, telegram_name FROM workers")
    assert stored["telegram_name"] == "Անի Հակոբյան"
    assert stored["name"] is None, "the owner's own field stays untouched"

    # And it now shows on the workers page.
    page = await client.get("/workers")
    assert "Անի Հակոբյան" in page.text


async def test_a_name_the_owner_typed_beats_the_telegram_one(client, bot_headers):
    """An owner who renames somebody must not have it undone by the next tap."""
    owner_id, token = await _signed_in(client)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0",
              "name": "Անի (գիշերային)", "csrf_token": token},
    )

    await client.get(
        f"{BASE}/me", params={"telegram_id": 555000777, "telegram_name": "nickname123"},
        headers=bot_headers,
    )

    listed = await workers_repo.list_for_owner(owner_id)
    assert listed[0]["name"] == "Անի (գիշերային)"
    assert listed[0]["telegram_name"] == "nickname123", "still recorded, just not preferred"


async def test_a_changed_telegram_name_is_picked_up(client, bot_headers):
    owner_id, token = await _signed_in(client)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0", "csrf_token": token},
    )

    for name in ("Անի", "Անի Հակոբյան"):
        await client.get(
            f"{BASE}/me", params={"telegram_id": 555000777, "telegram_name": name},
            headers=bot_headers,
        )

    assert await db.fetchval("SELECT telegram_name FROM workers") == "Անի Հակոբյան"


async def test_an_unregistered_id_is_refused_and_leaves_no_trace(client, bot_headers):
    """Registration is closed: standing outside the shop gets a stranger nothing."""
    await _signed_in(client)

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": 999888777, "telegram_name": "Stranger"},
        headers=bot_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_worker"
    assert await db.fetchval("SELECT count(*) FROM workers") == 0


async def test_a_deactivated_worker_is_refused_and_their_name_is_not_refreshed(client, bot_headers):
    owner_id = await make_owner("owner@example.com")
    worker_id, telegram_id = await make_worker(owner_id, is_active=False)
    await db.execute("UPDATE workers SET telegram_name = 'old' WHERE id = $1", worker_id)

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": telegram_id, "telegram_name": "new"},
        headers=bot_headers,
    )

    assert response.status_code == 403
    assert await db.fetchval("SELECT telegram_name FROM workers") == "old"


async def test_the_name_also_arrives_when_opening_a_store(client, bot_headers):
    """A worker whose first action is opening the shop still gets named."""
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0", "csrf_token": token},
    )

    response = await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": 555000777, "telegram_name": "Գոռ", "lat": YEREVAN_LAT,
              "lng": YEREVAN_LNG, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )

    assert response.status_code == 201
    assert await db.fetchval("SELECT telegram_name FROM workers") == "Գոռ"


async def test_the_telegram_name_shows_up_in_reports(client, bot_headers):
    """Whatever name is in force must be the one on the shift and receipt rows."""
    owner_id, token = await _signed_in(client)
    await make_store(owner_id, lat=YEREVAN_LAT, lng=YEREVAN_LNG, radius_m=120)
    await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0", "csrf_token": token},
    )
    await client.post(
        f"{BASE}/store/open",
        json={"telegram_id": 555000777, "telegram_name": "Գոռ", "lat": YEREVAN_LAT,
              "lng": YEREVAN_LNG, "idempotency_key": "idem-key-open-01"},
        headers=bot_headers,
    )
    session_id = await db.fetchval("SELECT id FROM store_sessions")

    detail = await client.get(f"/reports?store_session_id={session_id}")

    assert "Գոռ" in detail.text


async def test_two_owners_cannot_share_a_telegram_id(client):
    """It is the only thing identifying an owner in a bot request."""
    _, token = await _signed_in(client)
    other = await make_owner()
    await make_worker(other, telegram_id=555000777)

    response = await client.post(
        "/workers",
        data={"telegram_id": "555000777", "salary_per_shift": "0", "csrf_token": token},
    )

    assert response.status_code == 422
    assert "արդեն գրանցված" in response.text
