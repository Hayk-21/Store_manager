"""Login, sessions and CSRF."""

from __future__ import annotations

from app.db import db
from app.deps import SESSION_COOKIE
from tests.factories import DEFAULT_PASSWORD, login, make_owner


async def test_correct_password_signs_in(client):
    await make_owner("owner@example.com")

    response = await login(client, "owner@example.com")

    assert response.status_code == 303
    assert response.headers["location"] == "/stores"
    assert client.cookies.get(SESSION_COOKIE)


async def test_wrong_password_is_refused(client):
    await make_owner("owner@example.com")

    response = await login(client, "owner@example.com", "not-the-password")

    assert response.status_code == 400
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_unknown_email_looks_exactly_like_a_wrong_password(client):
    """Anything that distinguishes the two lets an outsider test which addresses
    have accounts."""
    await make_owner("owner@example.com")

    wrong_password = await login(client, "owner@example.com", "not-the-password")
    unknown_email = await login(client, "nobody@example.com", "not-the-password")

    assert wrong_password.status_code == unknown_email.status_code
    # The page echoes back whatever address was typed, which tells the visitor
    # nothing they did not already know. What must match is the complaint.
    complaint = "Էլ. փոստը կամ գաղտնաբառը սխալ է։"
    assert complaint in wrong_password.text
    assert complaint in unknown_email.text


async def test_deactivated_user_cannot_sign_in(client):
    owner_id = await make_owner("owner@example.com")
    await db.execute("UPDATE users SET is_active = false WHERE id = $1", owner_id)

    response = await login(client, "owner@example.com")

    assert response.status_code == 400
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_a_user_without_a_password_can_never_sign_in(client):
    """An invited-but-not-yet-activated row must not be a way in. This is the
    property that lets password_hash stay nullable for the future email flow."""
    await db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('invited@example.com', NULL)"
    )

    response = await login(client, "invited@example.com", "anything-at-all")

    assert response.status_code == 400
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_session_cookie_is_httponly_and_samesite_lax(client):
    await make_owner("owner@example.com")

    response = await login(client, "owner@example.com")

    cookie = next(
        value for key, value in response.headers.items()
        if key.lower() == "set-cookie" and value.startswith(SESSION_COOKIE)
    )
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")


async def test_only_the_token_hash_is_stored(client):
    """A database dump must not hand over live sessions."""
    await make_owner("owner@example.com")
    await login(client, "owner@example.com")
    raw_token = client.cookies.get(SESSION_COOKIE)

    stored = await db.fetchval("SELECT token_hash FROM auth_sessions")

    assert stored != raw_token
    assert raw_token not in stored


async def test_login_without_a_csrf_token_is_refused(client):
    await make_owner("owner@example.com")
    await client.get("/login")

    response = await client.post(
        "/login", data={"email": "owner@example.com", "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 403
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_login_with_a_forged_csrf_token_is_refused(client):
    await make_owner("owner@example.com")
    await client.get("/login")

    response = await client.post(
        "/login",
        data={
            "email": "owner@example.com",
            "password": DEFAULT_PASSWORD,
            "csrf_token": "not-the-token-in-the-cookie",
        },
    )

    assert response.status_code == 403


async def test_cross_origin_login_is_refused(client):
    await make_owner("owner@example.com")
    page = await client.get("/login")
    assert page.status_code == 200
    token = client.cookies.get("vs_csrf")

    response = await client.post(
        "/login",
        data={"email": "owner@example.com", "password": DEFAULT_PASSWORD, "csrf_token": token},
        headers={"Origin": "https://evil.example.com"},
    )

    assert response.status_code == 403


async def test_logout_deletes_the_session_row(client):
    await make_owner("owner@example.com")
    await login(client, "owner@example.com")
    assert await db.fetchval("SELECT count(*) FROM auth_sessions") == 1

    response = await client.post("/logout")

    assert response.status_code == 303
    assert await db.fetchval("SELECT count(*) FROM auth_sessions") == 0


async def test_an_old_cookie_stops_working_after_logout(client):
    """Server-side sessions, not signed cookies: revocation has to be real."""
    await make_owner("owner@example.com")
    await login(client, "owner@example.com")
    stolen = client.cookies.get(SESSION_COOKIE)
    await client.post("/logout")
    client.cookies.set(SESSION_COOKIE, stolen)

    response = await client.get("/")

    assert response.headers["location"] == "/login"


async def test_repeated_failures_are_throttled(client):
    from app.config import settings

    await make_owner("owner@example.com")
    for _ in range(settings.login_max_attempts):
        await login(client, "owner@example.com", "wrong")

    # The right password now, but the account is in the penalty box.
    response = await login(client, "owner@example.com")

    assert response.status_code == 400
    assert "Չափազանց շատ փորձեր" in response.text
    assert client.cookies.get(SESSION_COOKIE) is None


async def test_a_successful_login_clears_the_failure_count(client):
    await make_owner("owner@example.com")
    for _ in range(3):
        await login(client, "owner@example.com", "wrong")

    assert (await login(client, "owner@example.com")).status_code == 303

    remaining = await db.fetchval(
        "SELECT count(*) FROM login_attempts WHERE NOT succeeded"
    )
    assert remaining == 0
