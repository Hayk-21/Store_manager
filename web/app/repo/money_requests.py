"""A shop asking another shop, or the owner, for cash."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# Every read carries the names, because a request with ids in it is unreadable and
# every caller is rendering it for somebody who has to decide. ``asked_of_name`` is
# null when the owner is the one being asked — they have no shop — and the reader
# says «ղեկավարից» rather than printing a blank.
_SELECT = f"""
    SELECT r.id, r.owner_id, r.amount, r.status, r.created_at, r.decided_at,
           r.to_store_id, r.to_session_id, r.asked_of_store_id, r.asked_the_owner,
           r.transfer_id, r.requested_by_worker_id,
           r.decided_by_worker_id, r.decided_by_owner,
           asker.name AS to_store_name,
           asked.name AS asked_of_name,
           CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END AS requested_by_name
      FROM money_requests r
      JOIN stores asker ON asker.id = r.to_store_id
      LEFT JOIN stores asked ON asked.id = r.asked_of_store_id
      LEFT JOIN workers w ON w.id = r.requested_by_worker_id
"""


async def insert(
    conn,
    *,
    owner_id: int,
    to_store_id: int,
    to_session_id: int,
    amount: Decimal,
    asked_of_store_id: int | None,
    asked_the_owner: bool,
    requested_by_worker_id: int | None,
    external_id: str | None = None,
) -> int:
    """One row, always pending. Exactly one of shop-or-owner is filled in; the
    schema refuses the other two combinations."""
    return await conn.fetchval(
        """
        INSERT INTO money_requests
            (owner_id, to_store_id, to_session_id, amount, asked_of_store_id,
             asked_the_owner, requested_by_worker_id, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        owner_id, to_store_id, to_session_id, amount, asked_of_store_id,
        asked_the_owner, requested_by_worker_id, external_id,
    )


async def get(owner_id: int, request_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE r.id = $1 AND r.owner_id = $2", request_id, owner_id
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        _SELECT + " WHERE r.owner_id = $1 AND r.external_id = $2", owner_id, external_id
    )


async def lock_pending(conn, owner_id: int, request_id: int) -> asyncpg.Record | None:
    """The row, locked, only while it is still waiting for an answer.

    Locked because two people can be looking at the same request — two workers at
    the shop being asked, or the owner on one phone and a colleague on another. The
    second tap must be told it is answered rather than sending the money twice.
    """
    return await conn.fetchrow(
        """
        SELECT id, amount, status, to_store_id, to_session_id,
               asked_of_store_id, asked_the_owner, requested_by_worker_id
          FROM money_requests
         WHERE id = $1 AND owner_id = $2 AND status = 'pending'
           FOR UPDATE
        """,
        request_id,
        owner_id,
    )


async def decide(
    conn,
    request_id: int,
    status: str,
    *,
    worker_id: int | None = None,
    by_owner: bool = False,
    transfer_id: int | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE money_requests
           SET status = $2, decided_by_worker_id = $3, decided_by_owner = $4,
               transfer_id = $5, decided_at = now()
         WHERE id = $1
        """,
        request_id,
        status,
        worker_id,
        by_owner,
        transfer_id,
    )


async def pending_for_store(owner_id: int, store_id: int) -> list[asyncpg.Record]:
    """What this shop is being asked for and has not answered."""
    return await db.fetch(
        _SELECT
        + """
         WHERE r.owner_id = $1 AND r.asked_of_store_id = $2 AND r.status = 'pending'
         ORDER BY r.created_at, r.id
        """,
        owner_id,
        store_id,
    )


async def pending_for_owner(owner_id: int) -> list[asyncpg.Record]:
    """What the owner is being asked for, across every shop at once."""
    return await db.fetch(
        _SELECT
        + """
         WHERE r.owner_id = $1 AND r.asked_the_owner AND r.status = 'pending'
         ORDER BY r.created_at, r.id
        """,
        owner_id,
    )


async def recent_for_owner(owner_id: int, limit: int = 50) -> list[asyncpg.Record]:
    """The history, newest first."""
    return await db.fetch(
        _SELECT
        + """
         WHERE r.owner_id = $1
         ORDER BY r.created_at DESC, r.id DESC
         LIMIT $2
        """,
        owner_id,
        limit,
    )
