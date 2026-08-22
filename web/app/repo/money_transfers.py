"""Cash carried between two of one owner's shops."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# Both shop names on every read, because a transfer with ids in it is unreadable
# and every caller is rendering it for somebody. The sender's name comes with it:
# the message at the other end says who to expect the envelope from, and "somebody
# at Կենտրոն" is not something a person can go and ask about.
_SELECT = f"""
    SELECT t.id, t.amount, t.status, t.created_at, t.decided_at,
           t.from_store_id, t.to_store_id, t.from_session_id, t.to_session_id,
           t.sent_by_worker_id, t.decided_by_worker_id,
           src.name AS from_store_name,
           dst.name AS to_store_name,
           CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END AS sent_by_name
      FROM money_transfers t
      JOIN stores src ON src.id = t.from_store_id
      JOIN stores dst ON dst.id = t.to_store_id
      LEFT JOIN workers w ON w.id = t.sent_by_worker_id
"""


async def insert(
    conn,
    *,
    owner_id: int,
    from_store_id: int,
    to_store_id: int,
    from_session_id: int,
    amount: Decimal,
    sent_by_worker_id: int | None,
    external_id: str | None = None,
) -> int:
    """One row, always pending. Nothing here is ever created already answered —
    unlike stock, there is no path where the same person is at both ends."""
    return await conn.fetchval(
        """
        INSERT INTO money_transfers
            (owner_id, from_store_id, to_store_id, from_session_id, amount,
             sent_by_worker_id, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        owner_id, from_store_id, to_store_id, from_session_id, amount,
        sent_by_worker_id, external_id,
    )


async def get(owner_id: int, transfer_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE t.id = $1 AND t.owner_id = $2", transfer_id, owner_id
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE t.owner_id = $1 AND t.external_id = $2", owner_id, external_id
    )


async def lock_pending(conn, owner_id: int, transfer_id: int) -> asyncpg.Record | None:
    """The row, locked, only while it is still waiting for an answer.

    Locked because two workers at the destination can be looking at the same
    envelope: whoever taps first decides it, and the second must be told it is
    already answered rather than crediting the till twice.
    """
    return await conn.fetchrow(
        """
        SELECT id, amount, status, from_store_id, to_store_id, from_session_id,
               sent_by_worker_id
          FROM money_transfers
         WHERE id = $1 AND owner_id = $2 AND status = 'pending'
           FOR UPDATE
        """,
        transfer_id,
        owner_id,
    )


async def decide(
    conn,
    transfer_id: int,
    status: str,
    *,
    worker_id: int | None = None,
    to_session_id: int | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE money_transfers
           SET status = $2, decided_by_worker_id = $3, to_session_id = $4,
               decided_at = now()
         WHERE id = $1
        """,
        transfer_id,
        status,
        worker_id,
        to_session_id,
    )


async def pending_for_store(owner_id: int, store_id: int) -> list[asyncpg.Record]:
    """Envelopes this shop has been told to expect and not yet confirmed."""
    return await db.fetch(
        _SELECT
        + """
         WHERE t.owner_id = $1 AND t.to_store_id = $2 AND t.status = 'pending'
         ORDER BY t.created_at, t.id
        """,
        owner_id,
        store_id,
    )


async def recent_for_owner(owner_id: int, limit: int = 50) -> list[asyncpg.Record]:
    """The history, newest first."""
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
