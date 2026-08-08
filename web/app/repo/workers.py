"""Workers.

The owner registers a ``@username`` because that is what they know. Telegram's
Bot API cannot turn a username into a numeric id, so the binding happens on first
contact: the first time that person messages the bot, their ``telegram_id`` is
written here and from then on it is the id that identifies them. Usernames can be
changed or handed to somebody else; a numeric id cannot.
"""

from __future__ import annotations

import re
from decimal import Decimal

import asyncpg

from app.db import db

# The name to show, in order of authority: what the owner typed, then what
# Telegram reports, then the @username, then the raw id — so a row is never
# blank. Defined once so every screen agrees.
DISPLAY_NAME = (
    "coalesce(nullif(btrim(w.name), ''), nullif(btrim(w.telegram_name), ''), "
    "'@' || w.telegram_username, 'ID ' || w.telegram_id)"
)

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{4,32}$")

SALARY_PERIODS = {"shift", "month"}


def normalise_username(raw: str | None) -> str | None:
    """``@JustHayk`` -> ``JustHayk``. Case is preserved for display; matching is
    case-insensitive, because Telegram treats the two as the same account."""
    if not raw:
        return None
    cleaned = raw.strip().lstrip("@").strip()
    if not cleaned:
        return None
    if not USERNAME_PATTERN.match(cleaned):
        return ""  # signals "given but unusable"; the caller turns it into an error
    return cleaned


async def by_telegram_id(telegram_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"""
        SELECT w.id, w.owner_id, {DISPLAY_NAME} AS name, w.telegram_id,
               w.telegram_username, w.telegram_name, w.salary_amount,
               w.salary_period, w.is_active
          FROM workers w WHERE w.telegram_id = $1
        """,
        telegram_id,
    )


async def claim_by_username(username: str, telegram_id: int) -> asyncpg.Record | None:
    """Bind an unbound registration to the account that just made contact.

    A single UPDATE so two simultaneous first messages cannot both claim the row:
    ``telegram_id IS NULL`` in the WHERE clause is the guard, and whichever
    statement gets there second matches nothing.
    """
    return await db.fetchrow(
        f"""
        UPDATE workers w
           SET telegram_id = $2, updated_at = now()
         WHERE lower(w.telegram_username) = lower($1) AND w.telegram_id IS NULL
        RETURNING w.id, w.owner_id, {DISPLAY_NAME} AS name, w.telegram_id,
                  w.telegram_username, w.telegram_name, w.salary_amount,
                  w.salary_period, w.is_active
        """,
        username,
        telegram_id,
    )


async def remember_telegram_name(worker_id: int, telegram_name: str | None) -> None:
    """Store the profile name the bot just reported.

    Only writes when it actually changed. Never touches ``name`` — an owner who
    renamed somebody must not have the correction undone by the next tap.
    """
    if not telegram_name:
        return
    await db.execute(
        """
        UPDATE workers SET telegram_name = $2, updated_at = now()
         WHERE id = $1 AND telegram_name IS DISTINCT FROM $2
        """,
        worker_id,
        telegram_name[:200],
    )


async def list_for_owner(owner_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"""
        SELECT w.id, {DISPLAY_NAME} AS name, w.name AS own_name, w.telegram_name,
               w.telegram_id, w.telegram_username, w.salary_amount, w.salary_period,
               w.is_active, w.bonus_threshold, w.bonus_amount, w.bonus_period,
               ws.id AS open_shift_id, ws.started_at, s.name AS store_name
          FROM workers w
          LEFT JOIN work_sessions ws ON ws.worker_id = w.id AND ws.ended_at IS NULL
          LEFT JOIN stores s ON s.id = ws.store_id
         WHERE w.owner_id = $1 AND w.archived_at IS NULL
         ORDER BY w.is_active DESC, lower({DISPLAY_NAME})
        """,
        owner_id,
    )


async def get(owner_id: int, worker_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"""
        SELECT w.id, {DISPLAY_NAME} AS name, w.name AS own_name, w.telegram_name,
               w.telegram_id, w.telegram_username, w.salary_amount, w.salary_period,
               w.is_active
          FROM workers w WHERE w.id = $1 AND w.owner_id = $2
        """,
        worker_id,
        owner_id,
    )


async def create(
    owner_id: int,
    telegram_username: str,
    salary_amount: Decimal,
    salary_period: str,
    name: str | None = None,
    bonus: tuple[Decimal, Decimal, str] | None = None,
) -> int:
    """Register a @username. telegram_id fills in on first contact."""
    return await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_username, salary_amount, salary_period,
                             bonus_threshold, bonus_amount, bonus_period)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        owner_id,
        name,
        telegram_username,
        salary_amount,
        salary_period,
        *(bonus or (None, None, None)),
    )


async def update(
    owner_id: int,
    worker_id: int,
    name: str | None,
    telegram_username: str,
    salary_amount: Decimal,
    salary_period: str,
    is_active: bool,
    bonus: tuple[Decimal, Decimal, str] | None = None,
) -> bool:
    """Edit a registration.

    Changing the username on a worker who has already been bound deliberately
    does *not* clear ``telegram_id``: the person who has been working the shifts
    is the one the id points at, and a typo in the username field must not hand
    their history to somebody else.
    """
    result = await db.execute(
        """
        UPDATE workers
           SET name = $3, telegram_username = $4, salary_amount = $5,
               salary_period = $6, is_active = $7,
               bonus_threshold = $8, bonus_amount = $9, bonus_period = $10,
               updated_at = now()
         WHERE id = $1 AND owner_id = $2
        """,
        worker_id,
        owner_id,
        name,
        telegram_username,
        salary_amount,
        salary_period,
        is_active,
        *(bonus or (None, None, None)),
    )
    return result.endswith(" 1")


async def has_history(owner_id: int, worker_id: int) -> bool:
    """Has this worker ever actually started a shift?"""
    return await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM work_sessions WHERE worker_id = $1 AND owner_id = $2
        )
        """,
        worker_id,
        owner_id,
    )


async def delete_outright(owner_id: int, worker_id: int) -> bool:
    """Remove a registration that never got used. Safe only with no history."""
    result = await db.execute(
        "DELETE FROM workers WHERE id = $1 AND owner_id = $2", worker_id, owner_id
    )
    return result.endswith(" 1")


async def archive(owner_id: int, worker_id: int) -> bool:
    """Take a worker off the list without touching what they did.

    Their shifts and receipts stay exactly as they were, so past reports still
    add up. Two details make that work:

    * the display name is *frozen into* ``name`` first, because the handle and
      id it would otherwise fall back to are about to be cleared; and
    * the handle and id are then released, so a departing worker's @username can
      be registered again by whoever replaces them.
    """
    result = await db.execute(
        f"""
        UPDATE workers w
           -- left(): telegram_name allows 200 characters and name only 120.
           SET name = left({DISPLAY_NAME}, 120),
               telegram_id = NULL,
               telegram_username = NULL,
               is_active = false,
               archived_at = now(),
               updated_at = now()
         WHERE w.id = $1 AND w.owner_id = $2 AND w.archived_at IS NULL
        """,
        worker_id,
        owner_id,
    )
    return result.endswith(" 1")


async def unbind(owner_id: int, worker_id: int) -> bool:
    """Forget which Telegram account a registration belongs to.

    For when the wrong person claimed a username, or somebody left and their
    handle was reused. The next matching account to make contact claims it.
    """
    result = await db.execute(
        """
        UPDATE workers SET telegram_id = NULL, telegram_name = NULL, updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND telegram_username IS NOT NULL
        """,
        worker_id,
        owner_id,
    )
    return result.endswith(" 1")
