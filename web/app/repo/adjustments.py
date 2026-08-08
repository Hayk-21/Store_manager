"""Corrections to a stock count that are neither sales nor breakage."""

from __future__ import annotations

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME


async def insert(
    conn,
    *,
    owner_id: int,
    store_id: int,
    item_id: int,
    worker_id: int | None,
    work_session_id: int | None,
    store_session_id: int | None,
    delta: int,
    count_after: int,
    note: str | None = None,
    external_id: str | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO stock_adjustments
            (owner_id, store_id, item_id, worker_id, work_session_id,
             store_session_id, delta, count_after, note, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id
        """,
        owner_id, store_id, item_id, worker_id, work_session_id, store_session_id,
        delta, count_after, note, external_id,
    )


async def by_external_id(owner_id: int, external_id: str) -> list[asyncpg.Record]:
    """Every row one tap of "confirm" wrote.

    A batch shares its key, because the tap is the action: the cashier corrected
    the shelf once and a retry must not correct it again. Returns the rows so the
    reply to a retry is the same reply the first attempt gave.
    """
    return await db.fetch(
        """
        SELECT sa.id, sa.item_id, sa.delta, sa.count_after, i.name
          FROM stock_adjustments sa
          JOIN items i ON i.id = sa.item_id
         WHERE sa.owner_id = $1 AND sa.external_id = $2
         ORDER BY sa.id
        """,
        owner_id,
        external_id,
    )


async def for_session(store_session_id: int) -> list[asyncpg.Record]:
    """What was corrected while the store was open, newest first."""
    return await db.fetch(
        f"""
        SELECT sa.id, sa.item_id, sa.delta, sa.count_after, sa.note, sa.created_at,
               i.name, {DISPLAY_NAME} AS worker_name
          FROM stock_adjustments sa
          JOIN items i ON i.id = sa.item_id
          LEFT JOIN workers w ON w.id = sa.worker_id
         WHERE sa.store_session_id = $1
         ORDER BY sa.created_at DESC, sa.id DESC
        """,
        store_session_id,
    )


async def delete_for_session(conn, owner_id: int, store_session_id: int) -> None:
    """Deleting a report takes its corrections with it.

    The key sets null rather than cascading, which would leave rows behind that
    belong to no shift and appear on no report.
    """
    await conn.execute(
        "DELETE FROM stock_adjustments WHERE store_session_id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )
