"""Sessions: staying signed in, and getting signed out.

The way *in* is tested in test_login.py. This is about what the cookie does
afterwards.
"""

from __future__ import annotations

from datetime import timedelta

from app.config import settings
from app.db import db
from app.deps import SESSION_COOKIE
from app.repo import users as users_repo
from app.security import generate_token, hash_token
from tests.factories import login, make_owner


async def test_a_session_gets_you_onto_the_site(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    response = await client.get("/stores")

    assert response.status_code == 200


async def test_no_cookie_means_the_login_page(client):
    response = await client.get("/stores")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


async def test_only_the_token_hash_is_stored(client):
    """A database dump must not hand over live sessions."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    raw = client.cookies.get(SESSION_COOKIE)

    stored = await db.fetchval("SELECT token_hash FROM auth_sessions")

    assert stored != raw
    assert raw not in stored


async def test_logout_really_revokes(client):
    """Server-side sessions, not signed cookies, so this can be real."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    stolen = client.cookies.get(SESSION_COOKIE)

    await client.post("/logout")
    assert await db.fetchval("SELECT count(*) FROM auth_sessions") == 0

    client.cookies.set(SESSION_COOKIE, stolen)
    assert (await client.get("/stores")).headers["location"] == "/login"


async def test_a_deactivated_account_loses_access_immediately(client):
    owner_id = await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    await db.execute("UPDATE users SET is_active = false WHERE id = $1", owner_id)

    assert (await client.get("/stores")).headers["location"] == "/login"


# -- staying signed in -------------------------------------------------------

async def test_sessions_last_months_not_days(client):
    """"Remember me" is the default: nobody should sign in every morning."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")

    row = await db.fetchrow("SELECT created_at, expires_at FROM auth_sessions")

    assert settings.session_ttl_days >= 30
    assert (row["expires_at"] - row["created_at"]) > timedelta(days=29)


async def test_using_the_site_pushes_the_expiry_back_out(client):
    """An active session should never lapse, however long ago you signed in."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    # Wind it down to nearly spent, the way a real session would drift.
    await db.execute("UPDATE auth_sessions SET expires_at = now() + interval '2 days'")
    nearly_gone = await db.fetchval("SELECT expires_at FROM auth_sessions")

    await client.get("/stores")

    renewed = await db.fetchval("SELECT expires_at FROM auth_sessions")
    assert renewed > nearly_gone + timedelta(days=30)


async def test_a_fresh_session_is_not_rewritten_on_every_request(client):
    """The expiry only moves once it is more than half spent, so an open tab
    polling the footer is not writing a timestamp every ten seconds."""
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    before = await db.fetchval("SELECT expires_at FROM auth_sessions")

    await client.get("/stores")

    assert await db.fetchval("SELECT expires_at FROM auth_sessions") == before


async def test_an_expired_session_stops_working(client):
    await make_owner("@ownerhandle")
    await login(client, "@ownerhandle")
    await db.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 minute'")

    assert (await client.get("/stores")).headers["location"] == "/login"


# -- the command-line escape hatch -------------------------------------------

async def test_a_one_time_link_signs_you_in(client):
    """The way back in when Telegram is unavailable. Minted by manage.py."""
    owner_id = await make_owner("@ownerhandle")
    token = generate_token()
    await users_repo.create_login_link(owner_id, hash_token(token), 15)

    response = await client.get(f"/login/link/{token}")

    assert response.status_code == 303
    assert response.headers["location"] == "/stores"
    assert client.cookies.get(SESSION_COOKIE)


async def test_a_one_time_link_works_exactly_once(client):
    owner_id = await make_owner("@ownerhandle")
    token = generate_token()
    await users_repo.create_login_link(owner_id, hash_token(token), 15)
    await client.get(f"/login/link/{token}")
    await client.post("/logout")

    again = await client.get(f"/login/link/{token}")

    assert again.status_code == 400
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_an_expired_link_is_refused(client):
    owner_id = await make_owner("@ownerhandle")
    token = generate_token()
    await users_repo.create_login_link(owner_id, hash_token(token), 15)
    await db.execute("UPDATE login_links SET expires_at = now() - interval '1 minute'")

    assert (await client.get(f"/login/link/{token}")).status_code == 400


async def test_a_made_up_link_is_refused(client):
    await make_owner("@ownerhandle")

    assert (await client.get(f"/login/link/{generate_token()}")).status_code == 400


# -- the password login is gone ----------------------------------------------

async def test_there_is_no_password_form_any_more(client):
    assert (await client.get("/login/password")).status_code == 404


async def test_the_login_page_asks_for_a_telegram_handle(client):
    page = await client.get("/login")

    assert "Telegram" in page.text
    assert 'name="telegram_username"' in page.text
    assert 'name="password"' not in page.text
