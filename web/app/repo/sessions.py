"""Store sessions (the accounting period) and work sessions (a worker's shift)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# -- store sessions ---------------------------------------------------------

async def open_for_store(store_id: int) -> asyncpg.Record | None:
    """The store's currently-open session, if it has one."""
    return await db.fetchrow(
        """
        SELECT ss.id, ss.store_id, ss.opened_at, ss.opened_by_worker_id, w.name AS opened_by_name
          FROM store_sessions ss
          LEFT JOIN workers w ON w.id = ss.opened_by_worker_id
         WHERE ss.store_id = $1 AND ss.closed_at IS NULL
        """,
        store_id,
    )


async def lock_open_for_store(conn, owner_id: int, store_id: int) -> asyncpg.Record | None:
    """The open session, locked for update.

    Held while closing so a sale cannot land between reading the totals and
    writing the snapshot.
    """
    return await conn.fetchrow(
        """
        SELECT id, store_id, opened_at
          FROM store_sessions
         WHERE store_id = $1 AND owner_id = $2 AND closed_at IS NULL
           FOR UPDATE
        """,
        store_id,
        owner_id,
    )


async def open_store_session(
    conn, owner_id: int, store_id: int, worker_id: int, opened_day: date
) -> int | None:
    """Open a session, or return None when the store already has one.

    ``ON CONFLICT DO NOTHING`` against the partial unique index is what makes two
    workers arriving at the same moment safe: exactly one insert wins and the
    other joins the session that won.
    """
    return await conn.fetchval(
        """
        INSERT INTO store_sessions (owner_id, store_id, opened_by_worker_id, opened_day)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        owner_id,
        store_id,
        worker_id,
        opened_day,
    )


async def close_store_session(
    conn,
    store_session_id: int,
    cash: Decimal,
    card: Decimal,
    salaries: Decimal,
    closed_by: str,
) -> None:
    await conn.execute(
        """
        UPDATE store_sessions
           SET closed_at = now(), cash_at_close = $2, card_at_close = $3,
               salaries_at_close = $4, closed_by = $5
         WHERE id = $1 AND closed_at IS NULL
        """,
        store_session_id,
        cash,
        card,
        salaries,
        closed_by,
    )


async def stale_open_sessions(hours: int) -> list[asyncpg.Record]:
    """Sessions somebody forgot to close."""
    return await db.fetch(
        """
        SELECT id, owner_id, store_id, opened_at
          FROM store_sessions
         WHERE closed_at IS NULL
           AND opened_at < now() - make_interval(hours => $1)
         ORDER BY opened_at
        """,
        hours,
    )


async def recent_store_sessions(
    owner_id: int, limit: int = 50, offset: int = 0
) -> list[asyncpg.Record]:
    """Backs /reports: one row per time a store was open, newest first."""
    return await db.fetch(
        """
        SELECT ss.id, ss.store_id, s.name AS store_name,
               ss.opened_at, ss.closed_at, ss.opened_day, ss.closed_by,
               ss.cash_at_close, ss.card_at_close, ss.salaries_at_close,
               (SELECT count(*) FROM work_sessions ws WHERE ws.store_session_id = ss.id)
                   AS shift_count,
               (SELECT count(*) FROM sales sa
                 WHERE sa.store_session_id = ss.id AND sa.voided_at IS NULL) AS receipt_count,
               coalesce((SELECT sum(m.amount) FILTER (WHERE m.method = 'cash')
                           FROM cash_movements m WHERE m.store_session_id = ss.id), 0) AS cash_now,
               coalesce((SELECT sum(m.amount) FILTER (WHERE m.method = 'card')
                           FROM cash_movements m WHERE m.store_session_id = ss.id), 0) AS card_now
          FROM store_sessions ss
          JOIN stores s ON s.id = ss.store_id
         WHERE ss.owner_id = $1
         ORDER BY ss.opened_at DESC
         LIMIT $2 OFFSET $3
        """,
        owner_id,
        limit,
        offset,
    )


async def get_store_session(owner_id: int, store_session_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ss.id, ss.store_id, s.name AS store_name, ss.opened_at, ss.closed_at,
               ss.closed_by, ss.cash_at_close, ss.card_at_close, ss.salaries_at_close
          FROM store_sessions ss
          JOIN stores s ON s.id = ss.store_id
         WHERE ss.id = $1 AND ss.owner_id = $2
        """,
        store_session_id,
        owner_id,
    )


# -- work sessions ----------------------------------------------------------

async def workers_on_shift(store_id: int) -> list[asyncpg.Record]:
    """Requirement 3: who is working in this store right now."""
    return await db.fetch(
        f"""
        SELECT ws.id, ws.worker_id, {DISPLAY_NAME} AS name, ws.started_at,
               ws.start_distance_m, w.salary_per_shift
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_id = $1 AND ws.ended_at IS NULL
         ORDER BY ws.started_at
        """,
        store_id,
    )


async def open_for_worker(worker_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, s.name AS store_name
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
         WHERE ws.worker_id = $1 AND ws.ended_at IS NULL
        """,
        worker_id,
    )


async def lock_open_for_worker(conn, worker_id: int) -> asyncpg.Record | None:
    """The worker's open shift, locked.

    Every sale takes this lock, which serialises one worker's sales against each
    other and blocks a concurrent shift-end — that is what stops a sale landing
    after the shift closed.
    """
    return await conn.fetchrow(
        """
        SELECT id, owner_id, worker_id, store_id, store_session_id, started_at
          FROM work_sessions
         WHERE worker_id = $1 AND ended_at IS NULL
           FOR UPDATE
        """,
        worker_id,
    )


async def open_work_session(
    conn,
    owner_id: int,
    worker_id: int,
    store_id: int,
    store_session_id: int,
    lat: float | None,
    lng: float | None,
    distance_m: int | None,
    idem_key: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO work_sessions
            (owner_id, worker_id, store_id, store_session_id,
             start_lat, start_lng, start_distance_m, start_idem_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        owner_id,
        worker_id,
        store_id,
        store_session_id,
        lat,
        lng,
        distance_m,
        idem_key,
    )


async def close_work_session(
    conn,
    work_session_id: int,
    salary: Decimal,
    lat: float | None,
    lng: float | None,
    idem_key: str | None,
    closed_by: str,
) -> None:
    await conn.execute(
        """
        UPDATE work_sessions
           SET ended_at = now(), salary_paid = $2, end_lat = $3, end_lng = $4,
               end_idem_key = $5, closed_by = $6
         WHERE id = $1 AND ended_at IS NULL
        """,
        work_session_id,
        salary,
        lat,
        lng,
        idem_key,
        closed_by,
    )


async def open_shifts_in_session(conn, store_session_id: int) -> list[asyncpg.Record]:
    """Every shift still open in a store session, locked — used when closing."""
    return await conn.fetch(
        """
        SELECT ws.id, ws.worker_id, ws.store_id, ws.owner_id, ws.store_session_id,
               w.salary_per_shift
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_session_id = $1 AND ws.ended_at IS NULL
         ORDER BY ws.id
           FOR UPDATE OF ws
        """,
        store_session_id,
    )


async def by_start_idem(owner_id: int, idem_key: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, ws.start_distance_m,
               s.name AS store_name
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
         WHERE ws.owner_id = $1 AND ws.start_idem_key = $2
        """,
        owner_id,
        idem_key,
    )


async def by_end_idem(owner_id: int, idem_key: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, ws.ended_at,
               ws.salary_paid, s.name AS store_name
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
         WHERE ws.owner_id = $1 AND ws.end_idem_key = $2
        """,
        owner_id,
        idem_key,
    )


async def shifts_in_session(store_session_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"""
        SELECT ws.id, {DISPLAY_NAME} AS worker_name, ws.started_at, ws.ended_at,
               ws.salary_paid, ws.start_distance_m, ws.closed_by
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_session_id = $1
         ORDER BY ws.started_at
        """,
        store_session_id,
    )
