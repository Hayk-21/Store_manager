"""Signing in with a Telegram handle and a code from the bot."""

from __future__ import annotations

import pytest

from app.db import db
from app.deps import SESSION_COOKIE
from app.services import telegram

BASE = "/api/bot/v1"
TG_ID = 555000111


@pytest.fixture
def outbox(monkeypatch) -> list[tuple[int, str]]:
    """Capture what would have been sent over Telegram."""
    sent: list[tuple[int, str]] = []

    async def fake_send(chat_id: int, text: str) -> None:
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram, "send_message", fake_send)
    # The service imported the module, not the function, so patching the module
    # attribute is enough — but assert it, because a future refactor to
    # "from .telegram import send_message" would silently send real messages.
    from app.services import login as login_service

    assert login_service.telegram is telegram
    return sent


async def make_admin(handle: str = "justhayk", bound: bool = True) -> int:
    user_id = await db.fetchval(
        """
        INSERT INTO users (telegram_username, display_name, activated_at)
        VALUES ($1, 'Հայկ', now()) RETURNING id
        """,
        handle,
    )
    if bound:
        await db.execute(
            "UPDATE users SET telegram_id = $2, telegram_bound_at = now() WHERE id = $1",
            user_id, TG_ID,
        )
    return user_id


def code_from(outbox) -> str:
    import re

    return re.search(r"<code>(\d{6})</code>", outbox[-1][1]).group(1)


async def _csrf(client, path="/login") -> str:
    await client.get(path)
    return client.cookies.get("vs_csrf")


async def _request(client, handle="@justhayk"):
    token = await _csrf(client)
    return await client.post(
        "/login", data={"telegram_username": handle, "csrf_token": token}
    )


# -- the happy path ----------------------------------------------------------

async def test_a_handle_gets_a_code_and_the_code_gets_you_in(client, outbox):
    await make_admin()

    asked = await _request(client)
    assert asked.status_code == 200
    assert len(outbox) == 1 and outbox[0][0] == TG_ID

    token = client.cookies.get("vs_csrf")
    signed_in = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": code_from(outbox), "csrf_token": token,
    })

    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/stores"
    assert client.cookies.get(SESSION_COOKIE)


async def test_the_handle_works_with_or_without_the_at_sign(client, outbox):
    await make_admin()

    assert (await _request(client, "justhayk")).status_code == 200
    assert len(outbox) == 1


async def test_the_handle_is_matched_case_insensitively(client, outbox):
    """Telegram treats @JustHayk and @justhayk as one account."""
    await make_admin("justhayk")

    await _request(client, "@JUSTHAYK")

    assert len(outbox) == 1


async def test_only_the_hash_of_the_code_is_stored(client, outbox):
    await make_admin()
    await _request(client)

    stored = await db.fetchval("SELECT code_hash FROM login_codes")
    assert code_from(outbox) not in stored


# -- refusals ----------------------------------------------------------------

async def test_an_unknown_handle_sends_nothing_but_says_the_same_thing(client, outbox):
    """The form must not be usable to find out who has an account."""
    await make_admin("justhayk")

    known = await _request(client, "@justhayk")
    unknown = await _request(client, "@nobodyhere")

    assert known.status_code == unknown.status_code == 200
    assert len(outbox) == 1, "nothing was sent for the unknown handle"


async def test_an_unbound_handle_is_told_to_press_start(client, outbox):
    """A bot cannot open a conversation, so there is nowhere to send the code."""
    await make_admin(bound=False)

    response = await _request(client)

    assert response.status_code == 400
    assert "Start" in response.text
    assert outbox == []


async def test_a_wrong_code_does_not_sign_you_in(client, outbox):
    await make_admin()
    await _request(client)
    token = client.cookies.get("vs_csrf")

    response = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": "000000", "csrf_token": token,
    })

    assert response.status_code == 400
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_a_code_only_works_once(client, outbox):
    await make_admin()
    await _request(client)
    code = code_from(outbox)
    token = client.cookies.get("vs_csrf")
    await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": code, "csrf_token": token,
    })
    await client.post("/logout")

    again = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": code,
        "csrf_token": await _csrf(client),
    })

    assert again.status_code == 400


async def test_asking_again_retires_the_previous_code(client, outbox):
    await make_admin()
    await _request(client)
    first = code_from(outbox)
    await _request(client)

    response = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": first,
        "csrf_token": client.cookies.get("vs_csrf"),
    })

    assert response.status_code == 400
    assert await db.fetchval("SELECT count(*) FROM login_codes WHERE consumed_at IS NULL") == 1


async def test_guessing_burns_the_code(client, outbox):
    from app.config import settings

    await make_admin()
    await _request(client)
    code = code_from(outbox)
    token = client.cookies.get("vs_csrf")

    for _ in range(settings.max_code_attempts):
        await client.post("/login/verify", data={
            "telegram_username": "justhayk", "code": "000000", "csrf_token": token,
        })

    # Even the right code is no good now.
    response = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": code, "csrf_token": token,
    })
    assert response.status_code == 429
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_an_expired_code_is_refused(client, outbox):
    await make_admin()
    await _request(client)
    await db.execute("UPDATE login_codes SET expires_at = now() - interval '1 minute'")

    response = await client.post("/login/verify", data={
        "telegram_username": "justhayk", "code": code_from(outbox),
        "csrf_token": client.cookies.get("vs_csrf"),
    })

    assert response.status_code == 400


async def test_a_deactivated_admin_gets_nothing(client, outbox):
    user_id = await make_admin()
    await db.execute("UPDATE users SET is_active = false WHERE id = $1", user_id)

    response = await _request(client)

    assert response.status_code == 200, "same answer as an unknown handle"
    assert outbox == []


async def test_too_many_requests_are_throttled(client, outbox):
    from app.config import settings

    await make_admin()
    for _ in range(settings.login_codes_per_hour):
        await _request(client)

    response = await _request(client)

    assert response.status_code == 429
    assert len(outbox) == settings.login_codes_per_hour


# -- binding through the bot -------------------------------------------------

async def test_pressing_start_binds_an_owner_who_is_not_a_cashier(client, bot_headers, outbox):
    """The whole reason /me answers for a non-worker: without this there is no
    chat to deliver a login code to."""
    await make_admin(bound=False)

    response = await client.get(
        f"{BASE}/me",
        params={"telegram_id": TG_ID, "telegram_username": "justhayk",
                "telegram_name": "Հայկ Ս"},
        headers=bot_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["worker"] is None, "they are an owner, not a cashier"
    assert body["admin"] is not None
    row = await db.fetchrow("SELECT telegram_id, telegram_name FROM users")
    assert row["telegram_id"] == TG_ID
    assert row["telegram_name"] == "Հայկ Ս"

    # And now a login code can actually be delivered.
    assert (await _request(client)).status_code == 200
    assert len(outbox) == 1


async def test_a_stranger_pressing_start_is_still_refused(client, bot_headers):
    await make_admin(bound=False)

    response = await client.get(
        f"{BASE}/me", params={"telegram_id": 999000111, "telegram_username": "stranger"},
        headers=bot_headers,
    )

    assert response.status_code == 404
    assert await db.fetchval("SELECT telegram_id FROM users") is None


async def test_someone_who_is_both_owner_and_cashier_gets_both(client, bot_headers):
    from tests.factories import make_owner, make_worker

    owner_id = await make_owner()
    await db.execute(
        "UPDATE users SET telegram_username = 'justhayk' WHERE id = $1", owner_id
    )
    await make_worker(owner_id, "Հայկ", telegram_id=TG_ID, telegram_username="justhayk")

    body = (await client.get(
        f"{BASE}/me",
        params={"telegram_id": TG_ID, "telegram_username": "justhayk"},
        headers=bot_headers,
    )).json()

    assert body["worker"] is not None
    assert body["admin"] is not None
