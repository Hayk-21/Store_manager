"""Workers.

``telegram_id`` is how a bot request resolves to an owner: it is the only
identifying thing in the payload, which is why the column is globally unique.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db


async def by_telegram_id(telegram_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, owner_id, name, telegram_id, salary_per_shift, is_active
          FROM workers WHERE telegram_id = $1
        """,
        telegram_id,
    )


async def list_for_owner(owner_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT w.id, w.name, w.telegram_id, w.salary_per_shift, w.is_active,
               ws.id AS open_shift_id, ws.started_at, s.name AS store_name
          FROM workers w
          LEFT JOIN work_sessions ws ON ws.worker_id = w.id AND ws.ended_at IS NULL
          LEFT JOIN stores s ON s.id = ws.store_id
         WHERE w.owner_id = $1
         ORDER BY w.is_active DESC, lower(w.name)
        """,
        owner_id,
    )


async def get(owner_id: int, worker_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, name, telegram_id, salary_per_shift, is_active
          FROM workers WHERE id = $1 AND owner_id = $2
        """,
        worker_id,
        owner_id,
    )


async def create(
    owner_id: int, name: str, telegram_id: int, salary_per_shift: Decimal
) -> int:
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
    name: str,
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
