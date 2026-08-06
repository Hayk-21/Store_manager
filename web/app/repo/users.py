"""Owner accounts, their Telegram binding, login codes and sessions.

Signing in means a Telegram handle and a code the bot delivers. There is no
password: the column is gone, and the way back in without Telegram is
``manage.py user login-link``, which needs database access rather than a public
form.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

from app.db import db

# The label to show for an account, in order of authority.
USER_DISPLAY = (
    "coalesce(nullif(btrim(u.display_name), ''), nullif(btrim(u.telegram_name), ''), "
    "'@' || u.telegram_username)"
)

_COLUMNS = f"""
    u.id, u.telegram_username, u.telegram_id, u.telegram_name, u.display_name,
    u.is_active, {USER_DISPLAY} AS label
"""


# -- accounts ---------------------------------------------------------------

async def by_id(user_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(f"SELECT {_COLUMNS} FROM users u WHERE u.id = $1", user_id)


async def by_telegram_username(username: str) -> asyncpg.Record | None:
    """Who is trying to sign in. Matched case-insensitively."""
    return await db.fetchrow(
        f"SELECT {_COLUMNS} FROM users u WHERE lower(u.telegram_username) = lower($1)",
        username,
    )


async def by_telegram_id(telegram_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"SELECT {_COLUMNS} FROM users u WHERE u.telegram_id = $1", telegram_id
    )


async def create_admin(telegram_username: str, display_name: str | None = None) -> int:
    """Register an owner by Telegram handle. The chat binds on first /start."""
    return await db.fetchval(
        """
        INSERT INTO users (telegram_username, display_name, activated_at)
        VALUES ($1, $2, now()) RETURNING id
        """,
        telegram_username,
        display_name,
    )


async def set_telegram_username(user_id: int, telegram_username: str) -> None:
    """Point an account at a different handle, releasing any previous binding."""
    await db.execute(
        """
        UPDATE users
           SET telegram_username = $2, telegram_id = NULL, telegram_bound_at = NULL
         WHERE id = $1
        """,
        user_id,
        telegram_username,
    )


async def set_active(user_id: int, is_active: bool) -> None:
    await db.execute("UPDATE users SET is_active = $2 WHERE id = $1", user_id, is_active)


async def touch_login(user_id: int) -> None:
    await db.execute("UPDATE users SET last_login_at = now() WHERE id = $1", user_id)


async def list_all() -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, display_name, telegram_username, telegram_id, telegram_name,
               is_active, created_at, last_login_at
          FROM users ORDER BY id
        """
    )


# -- telegram binding -------------------------------------------------------

async def claim_admin_by_username(
    username: str, telegram_id: int, telegram_name: str | None
) -> asyncpg.Record | None:
    """Bind a registered handle to the account that just messaged the bot.

    Until this happens there is no chat to deliver a login code to, because a
    bot cannot open a conversation. A single UPDATE guarded by
    ``telegram_id IS NULL`` so two simultaneous first messages cannot both claim.
    """
    return await db.fetchrow(
        f"""
        UPDATE users u
           SET telegram_id = $2, telegram_name = $3, telegram_bound_at = now()
         WHERE lower(u.telegram_username) = lower($1) AND u.telegram_id IS NULL
        RETURNING {_COLUMNS}
        """,
        username,
        telegram_id,
        telegram_name,
    )


async def refresh_telegram_name(user_id: int, telegram_name: str | None) -> None:
    if not telegram_name:
        return
    await db.execute(
        "UPDATE users SET telegram_name = $2 WHERE id = $1 AND telegram_name IS DISTINCT FROM $2",
        user_id,
        telegram_name[:200],
    )


# -- login codes ------------------------------------------------------------

async def replace_login_code(user_id: int, code_hash: str, ttl_minutes: int) -> None:
    """Issue a code, retiring any earlier one for this account."""
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE login_codes SET consumed_at = now() "
            "WHERE user_id = $1 AND consumed_at IS NULL",
            user_id,
        )
        await conn.execute(
            """
            INSERT INTO login_codes (user_id, code_hash, expires_at)
            VALUES ($1, $2, now() + make_interval(mins => $3))
            """,
            user_id,
            code_hash,
            ttl_minutes,
        )


async def live_login_code(user_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, code_hash, attempts, expires_at
          FROM login_codes
         WHERE user_id = $1 AND consumed_at IS NULL AND expires_at > now()
        """,
        user_id,
    )


async def bump_code_attempts(code_id: int) -> int:
    return await db.fetchval(
        "UPDATE login_codes SET attempts = attempts + 1 WHERE id = $1 RETURNING attempts",
        code_id,
    )


async def consume_login_code(code_id: int) -> None:
    await db.execute(
        "UPDATE login_codes SET consumed_at = now() WHERE id = $1 AND consumed_at IS NULL",
        code_id,
    )


async def codes_issued_since(user_id: int, minutes: int) -> int:
    return await db.fetchval(
        """
        SELECT count(*) FROM login_codes
         WHERE user_id = $1 AND created_at > now() - make_interval(mins => $2)
        """,
        user_id,
        minutes,
    )


async def purge_expired_codes() -> int:
    result = await db.execute(
        "DELETE FROM login_codes WHERE expires_at <= now() - interval '1 day'"
    )
    return int(result.rsplit(" ", 1)[-1]) if result.startswith("DELETE") else 0


# -- one-time sign-in links (the command-line escape hatch) ------------------

async def create_login_link(user_id: int, token_hash: str, ttl_minutes: int) -> None:
    await db.execute(
        """
        INSERT INTO login_links (user_id, token_hash, expires_at)
        VALUES ($1, $2, now() + make_interval(mins => $3))
        """,
        user_id,
        token_hash,
        ttl_minutes,
    )


async def consume_login_link(token_hash: str) -> int | None:
    """Spend a link and return whose it was. Works exactly once."""
    return await db.fetchval(
        """
        UPDATE login_links SET consumed_at = now()
         WHERE token_hash = $1 AND consumed_at IS NULL AND expires_at > now()
        RETURNING user_id
        """,
        token_hash,
    )


# -- sessions ---------------------------------------------------------------

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
        f"""
        SELECT s.token_hash, s.csrf_token, s.expires_at, u.id AS user_id,
               u.telegram_username, u.telegram_name, u.display_name, u.is_active,
               {USER_DISPLAY} AS label
          FROM auth_sessions s
          JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = $1 AND s.expires_at > now()
        """,
        token_hash,
    )


async def touch_session(token_hash: str, ttl_days: int) -> None:
    """Mark the session used, and push its expiry back out.

    This is what "remember me" actually means here: somebody who uses the site
    is never signed out, while an abandoned session still dies on its own. The
    expiry only moves once it is more than half spent, so an active tab is not
    writing a new timestamp on every poll of the footer.
    """
    await db.execute(
        """
        UPDATE auth_sessions
           SET last_seen_at = now(),
               expires_at = CASE
                   WHEN expires_at < now() + make_interval(days => $2 / 2)
                   THEN now() + make_interval(days => $2)
                   ELSE expires_at
               END
         WHERE token_hash = $1
        """,
        token_hash,
        ttl_days,
    )


async def delete_session(token_hash: str) -> None:
    await db.execute("DELETE FROM auth_sessions WHERE token_hash = $1", token_hash)


async def delete_sessions_for_user(user_id: int) -> None:
    await db.execute("DELETE FROM auth_sessions WHERE user_id = $1", user_id)


async def purge_expired_sessions() -> int:
    result = await db.execute("DELETE FROM auth_sessions WHERE expires_at <= now()")
    return int(result.rsplit(" ", 1)[-1]) if result.startswith("DELETE") else 0
