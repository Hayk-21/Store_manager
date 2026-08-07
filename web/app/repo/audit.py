"""The history of corrections, and what is needed to undo them."""

from __future__ import annotations

import json

import asyncpg

from app.db import db


async def record(
    conn,
    owner_id: int,
    user_id: int,
    action: str,
    summary: str,
    *,
    store_session_id: int | None = None,
    payload: dict | None = None,
) -> int:
    """Written inside the correction's own transaction.

    If the correction rolls back, so does its history entry — a log that can
    disagree with what happened is worse than no log.
    """
    return await conn.fetchval(
        """
        INSERT INTO audit_events (owner_id, user_id, action, summary,
                                  store_session_id, payload)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb)
        RETURNING id
        """,
        owner_id,
        user_id,
        action,
        summary,
        store_session_id,
        json.dumps(payload or {}),
    )


async def recent(owner_id: int, limit: int = 100) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT a.id, a.action, a.summary, a.store_session_id, a.created_at,
               a.reverted_at, a.payload,
               coalesce(u.display_name, u.telegram_name, '@' || u.telegram_username) AS by_whom,
               s.name AS store_name
          FROM audit_events a
          LEFT JOIN users u ON u.id = a.user_id
          LEFT JOIN store_sessions ss ON ss.id = a.store_session_id
          LEFT JOIN stores s ON s.id = ss.store_id
         WHERE a.owner_id = $1
         ORDER BY a.id DESC
         LIMIT $2
        """,
        owner_id,
        limit,
    )


async def for_session(owner_id: int, store_session_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT a.id, a.action, a.summary, a.created_at, a.reverted_at,
               coalesce(u.display_name, u.telegram_name, '@' || u.telegram_username) AS by_whom
          FROM audit_events a
          LEFT JOIN users u ON u.id = a.user_id
         WHERE a.owner_id = $1 AND a.store_session_id = $2
         ORDER BY a.id DESC
        """,
        owner_id,
        store_session_id,
    )


async def newest_pending(owner_id: int) -> asyncpg.Record | None:
    """The only event that can be undone: the most recent one still standing."""
    return await db.fetchrow(
        """
        SELECT id, action, summary FROM audit_events
         WHERE owner_id = $1 AND reverted_at IS NULL
         ORDER BY id DESC LIMIT 1
        """,
        owner_id,
    )


async def lock_for_revert(conn, owner_id: int, event_id: int) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT id, action, payload, store_session_id, reverted_at, summary
          FROM audit_events WHERE id = $1 AND owner_id = $2 FOR UPDATE
        """,
        event_id,
        owner_id,
    )


async def newer_pending_count(conn, owner_id: int, event_id: int) -> int:
    """How many corrections came after this one and are still standing.

    Undoing out of order would unpick a change something later depends on, so
    the application refuses while this is non-zero.
    """
    return await conn.fetchval(
        """
        SELECT count(*) FROM audit_events
         WHERE owner_id = $1 AND id > $2 AND reverted_at IS NULL
        """,
        owner_id,
        event_id,
    )


async def mark_reverted(conn, event_id: int, user_id: int) -> None:
    await conn.execute(
        "UPDATE audit_events SET reverted_at = now(), reverted_by = $2 WHERE id = $1",
        event_id,
        user_id,
    )
