"""Stock moving between two of one owner's shops."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db

# Every read wants the item name and both shop names, because a transfer with ids
# in it is unreadable, and every caller is rendering it for somebody.
_SELECT = """
    SELECT t.id, t.item_id, t.quantity, t.status, t.unit_cost, t.note,
           t.from_store_id, t.to_store_id, t.created_at, t.decided_at,
           t.decided_by_owner,
           i.name AS item_name,
           src.name AS from_store_name,
           dst.name AS to_store_name,
           t.requested_by_worker_id, t.decided_by_worker_id
      FROM transfers t
      JOIN items  i   ON i.id = t.item_id
      JOIN stores src ON src.id = t.from_store_id
      JOIN stores dst ON dst.id = t.to_store_id
"""


async def insert(
    conn,
    *,
    owner_id: int,
    item_id: int,
    from_store_id: int,
    to_store_id: int,
    quantity: int,
    unit_cost: Decimal,
    requested_by_worker_id: int | None,
    status: str = "pending",
    decided_by_worker_id: int | None = None,
    decided_by_owner: bool = False,
    note: str | None = None,
    external_id: str | None = None,
) -> int:
    """One row. ``status`` is 'approved' straight away when the owner did it."""
    return await conn.fetchval(
        """
        INSERT INTO transfers
            (owner_id, item_id, from_store_id, to_store_id, quantity, unit_cost,
             requested_by_worker_id, status, decided_by_worker_id, decided_by_owner,
             decided_at, note, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                CASE WHEN $8 = 'pending' THEN NULL ELSE now() END, $11, $12)
        RETURNING id
        """,
        owner_id, item_id, from_store_id, to_store_id, quantity, unit_cost,
        requested_by_worker_id, status, decided_by_worker_id, decided_by_owner,
        note, external_id,
    )


async def get(owner_id: int, transfer_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE t.id = $1 AND t.owner_id = $2", transfer_id, owner_id
    )


async def lock_pending(conn, owner_id: int, transfer_id: int) -> asyncpg.Record | None:
    """The row, locked, only while it is still waiting for an answer.

    Locked because two workers at the source shop can be looking at the same
    request: whoever taps first decides it, and the second must be told it is
    already answered rather than moving the stock twice.
    """
    return await conn.fetchrow(
        """
        SELECT id, item_id, quantity, from_store_id, to_store_id, status
          FROM transfers
         WHERE id = $1 AND owner_id = $2 AND status = 'pending'
           FOR UPDATE
        """,
        transfer_id,
        owner_id,
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE t.owner_id = $1 AND t.external_id = $2", owner_id, external_id
    )


async def decide(
    conn,
    transfer_id: int,
    status: str,
    *,
    worker_id: int | None = None,
    by_owner: bool = False,
) -> None:
    await conn.execute(
        """
        UPDATE transfers
           SET status = $2, decided_by_worker_id = $3, decided_by_owner = $4,
               decided_at = now()
         WHERE id = $1
        """,
        transfer_id,
        status,
        worker_id,
        by_owner,
    )


async def pending_for_store(owner_id: int, store_id: int) -> list[asyncpg.Record]:
    """Requests waiting for somebody at this shop to answer."""
    return await db.fetch(
        _SELECT
        + """
         WHERE t.owner_id = $1 AND t.from_store_id = $2 AND t.status = 'pending'
         ORDER BY t.created_at
        """,
        owner_id,
        store_id,
    )


async def recent_for_owner(owner_id: int, limit: int = 50) -> list[asyncpg.Record]:
    """The history, newest first — what the owner sees on the transfers page."""
    return await db.fetch(
        _SELECT
        + """
         WHERE t.owner_id = $1
         ORDER BY t.created_at DESC, t.id DESC
         LIMIT $2
        """,
        owner_id,
        limit,
    )
