"""Users, login throttling, and server-side sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

from app.db import db

# -- users -----------------------------------------------------------------

async def by_email(email: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, email, password_hash, display_name, is_active
          FROM users WHERE email = $1
        """,
        email,
    )


async def by_id(user_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT id, email, display_name, is_active FROM users WHERE id = $1",
        user_id,
    )


async def create(email: str, password_hash: str | None, display_name: str | None) -> int:
    return await db.fetchval(
        """
        INSERT INTO users (email, password_hash, display_name, activated_at, password_changed_at)
        VALUES ($1, $2, $3,
                CASE WHEN $2::text IS NULL THEN NULL ELSE now() END,
                CASE WHEN $2::text IS NULL THEN NULL ELSE now() END)
        RETURNING id
        """,
        email,
        password_hash,
        display_name,
    )


async def set_password(user_id: int, password_hash: str) -> None:
    await db.execute(
        """
        UPDATE users
           SET password_hash = $2,
               password_changed_at = now(),
               activated_at = coalesce(activated_at, now())
         WHERE id = $1
        """,
        user_id,
        password_hash,
    )


async def set_active(user_id: int, is_active: bool) -> None:
    await db.execute("UPDATE users SET is_active = $2 WHERE id = $1", user_id, is_active)


async def touch_login(user_id: int) -> None:
    await db.execute("UPDATE users SET last_login_at = now() WHERE id = $1", user_id)


async def list_all() -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, email, display_name, is_active,
               password_hash IS NOT NULL AS has_password,
               created_at, last_login_at
          FROM users ORDER BY id
        """
    )


# -- login throttling ------------------------------------------------------

async def record_attempt(email: str, ip: str | None, succeeded: bool) -> None:
    await db.execute(
        "INSERT INTO login_attempts (email, ip, succeeded) VALUES ($1, $2::inet, $3)",
        email,
        ip,
        succeeded,
    )


async def recent_failures(email: str, ip: str | None, window_minutes: int) -> int:
    """Failed attempts in the window, counted per-email OR per-IP.

    Either axis alone is easy to slip: rotating IPs defeats an IP-only counter,
    and spraying many addresses from one host defeats an email-only counter.
    """
    return await db.fetchval(
        """
        SELECT count(*) FROM login_attempts
         WHERE NOT succeeded
           AND attempted_at > now() - make_interval(mins => $3)
           AND (email = $1 OR ($2::inet IS NOT NULL AND ip = $2::inet))
        """,
        email,
        ip,
        window_minutes,
    )


async def clear_failures(email: str) -> None:
    """Called after a successful login so one good password resets the counter."""
    await db.execute(
        "DELETE FROM login_attempts WHERE email = $1 AND NOT succeeded", email
    )


# -- sessions --------------------------------------------------------------

async def create_session(
    token_hash: str, user_id: int, csrf_token: str, user_agent: str | None, ttl_days: int
) -> None:
    expires = datetime.now(UTC) + timedelta(days=ttl_days)
    await db.execute(
        """
        INSERT INTO auth_sessions (token_hash, user_id, csrf_token, user_agent, expires_at)
        VALUES ($1, $2, $3, $4, $5)
        """,
        token_hash,
        user_id,
        csrf_token,
        (user_agent or "")[:300] or None,
        expires,
    )


async def session_with_user(token_hash: str) -> asyncpg.Record | None:
    """The live session and its user in one round trip.

    Expiry is checked in SQL rather than in Python so a clock difference between
    the app and the database cannot extend a session.
    """
    return await db.fetchrow(
        """
        SELECT s.token_hash, s.csrf_token, s.expires_at,
               u.id AS user_id, u.email, u.display_name, u.is_active
          FROM auth_sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = $1 AND s.expires_at > now()
        """,
        token_hash,
    )


async def touch_session(token_hash: str) -> None:
    await db.execute(
        "UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = $1", token_hash
    )


async def delete_session(token_hash: str) -> None:
    await db.execute("DELETE FROM auth_sessions WHERE token_hash = $1", token_hash)


async def delete_sessions_for_user(user_id: int) -> None:
    """Used when a password changes: setting one signs the account out everywhere."""
    await db.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)


async def purge_expired_sessions() -> int:
    result = await db.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
    return int(result.rsplit(" ", 1)[-1]) if result.startswith("DELETE") else 0
