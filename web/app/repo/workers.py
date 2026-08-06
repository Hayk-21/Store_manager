"""Workers.

``telegram_id`` is how a bot request resolves to an owner: it is the only
identifying thing in the payload, which is why the column is globally unique.
It is also the only thing the owner has to type to register somebody — the name
arrives from Telegram the first time that person uses the bot.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db

# The name to show, in order of authority: what the owner typed, then what
# Telegram reports, then the raw id so a row is never blank. Defined once so
# every screen agrees.
DISPLAY_NAME = (
    "coalesce(nullif(btrim(w.name), ''), nullif(btrim(w.telegram_name), ''), "
    "'ID ' || w.telegram_id)"
)


async def by_telegram_id(telegram_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"""
        SELECT w.id, w.owner_id, {DISPLAY_NAME} AS name, w.telegram_id,
               w.telegram_name, w.salary_per_shift, w.is_active
          FROM workers w WHERE w.telegram_id = $1
        """,
        telegram_id,
    )


async def remember_telegram_name(worker_id: int, telegram_name: str | None) -> None:
    """Store the profile name the bot just reported.

    Only writes when it actually changed, so an unchanged name does not cost a
    round trip on every request. Never touches ``name`` — an owner who renamed
    somebody must not have the correction undone by the next tap.
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
               w.telegram_id, w.salary_per_shift, w.is_active,
               ws.id AS open_shift_id, ws.started_at, s.name AS store_name
          FROM workers w
          LEFT JOIN work_sessions ws ON ws.worker_id = w.id AND ws.ended_at IS NULL
          LEFT JOIN stores s ON s.id = ws.store_id
         WHERE w.owner_id = $1
         ORDER BY w.is_active DESC, lower({DISPLAY_NAME})
        """,
        owner_id,
    )


async def get(owner_id: int, worker_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        f"""
        SELECT w.id, {DISPLAY_NAME} AS name, w.name AS own_name, w.telegram_name,
               w.telegram_id, w.salary_per_shift, w.is_active
          FROM workers w WHERE w.id = $1 AND w.owner_id = $2
        """,
        worker_id,
        owner_id,
    )


async def create(
    owner_id: int, telegram_id: int, salary_per_shift: Decimal, name: str | None = None
) -> int:
    """Register a Telegram id. The name is optional and usually left empty."""
    return await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_id, salary_per_shift)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        owner_id,
        name,
        telegram_id,
        salary_per_shift,
    )


async def update(
    owner_id: int,
    worker_id: int,
    name: str | None,
    telegram_id: int,
    salary_per_shift: Decimal,
    is_active: bool,
) -> bool:
    result = await db.execute(
        """
        UPDATE workers
           SET name = $3, telegram_id = $4, salary_per_shift = $5, is_active = $6,
               updated_at = now()
         WHERE id = $1 AND owner_id = $2
        """,
        worker_id,
        owner_id,
        name,
        telegram_id,
        salary_per_shift,
        is_active,
    )
    return result.endswith(" 1")
