"""Stock removed without a sale."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# Every read joins the item, because a write-off is only meaningful with the
# name beside it, and the remaining count is what the worker wants to see.
_SELECT = """
    SELECT wo.id, wo.item_id, wo.quantity, wo.unit_cost, wo.total_cost,
           wo.reason, wo.created_at, wo.store_session_id,
           i.name, i.count AS remaining_count
      FROM write_offs wo
      JOIN items i ON i.id = wo.item_id
"""


async def insert(
    conn,
    *,
    owner_id: int,
    store_id: int,
    item_id: int,
    worker_id: int | None,
    work_session_id: int | None,
    store_session_id: int | None,
    quantity: int,
    unit_cost: Decimal,
    total_cost: Decimal,
    reason: str | None,
    external_id: str | None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO write_offs
            (owner_id, store_id, item_id, worker_id, work_session_id, store_session_id,
             quantity, unit_cost, total_cost, reason, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        owner_id, store_id, item_id, worker_id, work_session_id, store_session_id,
        quantity, unit_cost, total_cost, reason, external_id,
    )


async def get(owner_id: int, write_off_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE wo.id = $1 AND wo.owner_id = $2", write_off_id, owner_id
    )


async def by_external_id(owner_id: int, external_id: str | None) -> asyncpg.Record | None:
    """The idempotency lookup: a retried request finds its original write-off."""
    if external_id is None:
        return None
    return await db.fetchrow(
        _SELECT + " WHERE wo.owner_id = $1 AND wo.external_id = $2", owner_id, external_id
    )


async def for_session(store_session_id: int) -> list[asyncpg.Record]:
    """What was written off while the store was open, newest first."""
    return await db.fetch(
        f"""
        SELECT wo.id, wo.item_id, wo.quantity, wo.unit_cost, wo.total_cost,
               wo.reason, wo.created_at,
               i.name, i.count AS remaining_count,
               {DISPLAY_NAME} AS worker_name
          FROM write_offs wo
          JOIN items i ON i.id = wo.item_id
          LEFT JOIN workers w ON w.id = wo.worker_id
         WHERE wo.store_session_id = $1
         ORDER BY wo.created_at DESC, wo.id DESC
        """,
        store_session_id,
    )


async def for_work_session(work_session_id: int) -> list[asyncpg.Record]:
    """What this worker wrote off during this shift.

    No cost. This is read back to the cashier before they end the shift, and what
    the shop paid for a vape is the owner's business — the loss shows on the
    owner's report. What the cashier needs is that it was recorded, and for how
    many.
    """
    return await db.fetch(
        """
        SELECT wo.id, wo.quantity, wo.reason, wo.created_at, i.name
          FROM write_offs wo
          JOIN items i ON i.id = wo.item_id
         WHERE wo.work_session_id = $1
         ORDER BY wo.created_at, wo.id
        """,
        work_session_id,
    )


async def delete(conn, owner_id: int, write_off_id: int) -> bool:
    """Remove one write-off row. Takes a connection because the caller is putting
    the stock back in the same transaction."""
    result = await conn.execute(
        "DELETE FROM write_offs WHERE id = $1 AND owner_id = $2", write_off_id, owner_id
    )
    return result.endswith(" 1")


async def delete_for_session(conn, owner_id: int, store_session_id: int) -> None:
    """Every write-off of one store session.

    The foreign key sets ``store_session_id`` to null when a session goes, which
    would leave the rows behind as breakage belonging to no shift — still counted
    in the statistics, no longer visible in any report. Deleting the report means
    deleting them.
    """
    await conn.execute(
        "DELETE FROM write_offs WHERE store_session_id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )


async def cost_between(owner_id: int, since, until, store_id: int | None = None) -> Decimal:
    """What breakage cost over a period — a real expense, just not a till one."""
    return await db.fetchval(
        """
        SELECT coalesce(sum(total_cost), 0) FROM write_offs
         WHERE owner_id = $1
           AND created_at >= $2 AND created_at < ($3::date + 1)
           AND ($4::bigint IS NULL OR store_id = $4)
        """,
        owner_id, since, until, store_id,
    )
