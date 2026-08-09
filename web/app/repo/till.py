"""Hand counts of the cash drawer."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME


async def insert(
    conn,
    *,
    owner_id: int,
    store_id: int,
    store_session_id: int | None,
    work_session_id: int | None,
    worker_id: int | None,
    kind: str,
    counted: Decimal,
    expected: Decimal,
    note: str | None = None,
    external_id: str | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO till_counts
            (owner_id, store_id, store_session_id, work_session_id, worker_id,
             kind, counted, expected, note, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        owner_id, store_id, store_session_id, work_session_id, worker_id,
        kind, counted, expected, note, external_id,
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, kind, counted, expected FROM till_counts
         WHERE owner_id = $1 AND external_id = $2
        """,
        owner_id,
        external_id,
    )


async def last_close_for_store(conn, owner_id: int, store_id: int) -> Decimal | None:
    """What the last person out of this shop said they were leaving.

    Read on the caller's connection: it decides the float of a session being opened
    in the same transaction. ``None`` when nobody has ever counted here, which is
    not zero — it means "unknown", and the caller starts the till empty rather than
    inventing a figure.
    """
    return await conn.fetchval(
        """
        SELECT counted FROM till_counts
         WHERE owner_id = $1 AND store_id = $2 AND kind = 'close'
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        owner_id,
        store_id,
    )


async def last_close_for_store_pooled(
    owner_id: int, store_id: int
) -> asyncpg.Record | None:
    """The same lookup for a page render, off the pool.

    Separate from ``last_close_for_store`` rather than sharing it: that one takes an
    explicit connection because it decides the float of a session being opened in
    that transaction, and a page has no transaction to belong to. Carries who
    counted and when, which the float does not need and a reader does.
    """
    return await db.fetchrow(
        f"""
        SELECT t.counted, t.expected, t.created_at,
               (t.counted - t.expected) AS difference,
               {DISPLAY_NAME} AS worker_name
          FROM till_counts t
          LEFT JOIN workers w ON w.id = t.worker_id
         WHERE t.owner_id = $1 AND t.store_id = $2 AND t.kind = 'close'
         ORDER BY t.created_at DESC, t.id DESC
         LIMIT 1
        """,
        owner_id,
        store_id,
    )


async def for_session(store_session_id: int) -> list[asyncpg.Record]:
    """Both ends of the session's counts, for the report."""
    return await db.fetch(
        f"""
        SELECT t.id, t.kind, t.counted, t.expected, t.created_at,
               (t.counted - t.expected) AS difference,
               {DISPLAY_NAME} AS worker_name
          FROM till_counts t
          LEFT JOIN workers w ON w.id = t.worker_id
         WHERE t.store_session_id = $1
         ORDER BY t.created_at, t.id
        """,
        store_session_id,
    )


async def delete_for_session(conn, owner_id: int, store_session_id: int) -> None:
    """Deleting a report takes its counts with it — the key sets null rather than
    cascading, which would strand them."""
    await conn.execute(
        "DELETE FROM till_counts WHERE store_session_id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )
