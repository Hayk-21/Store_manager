"""Live-location readings received while a shift is open.

Append-only, like the ledger. The newest reading is also written onto the shift
row so the store page can show every worker's current distance without a
subquery per row — the trail is the record, the copy is a convenience.
"""

from __future__ import annotations

import asyncpg

from app.db import db


async def record_ping(
    conn,
    *,
    owner_id: int,
    worker_id: int,
    work_session_id: int,
    store_id: int,
    lat: float,
    lng: float,
    distance_m: int | None,
    accuracy_m: int | None,
    in_range: bool,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO location_pings
            (owner_id, worker_id, work_session_id, store_id, lat, lng,
             distance_m, accuracy_m, in_range)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        owner_id, worker_id, work_session_id, store_id, lat, lng,
        distance_m, accuracy_m, in_range,
    )


async def mark_position(
    conn,
    work_session_id: int,
    lat: float,
    lng: float,
    distance_m: int | None,
    *,
    in_range: bool,
) -> None:
    """Mirror the newest reading onto the shift.

    ``left_area_at`` is set on the first reading outside the radius and then left
    alone: it answers "did this worker leave, and when did it start", which a
    value that moved with every later reading could not.
    """
    await conn.execute(
        """
        UPDATE work_sessions
           SET last_lat = $2, last_lng = $3, last_distance_m = $4,
               last_ping_at = now(), ping_count = ping_count + 1,
               left_area_at = CASE
                   WHEN $5 THEN left_area_at
                   ELSE coalesce(left_area_at, now())
               END
         WHERE id = $1
        """,
        work_session_id, lat, lng, distance_m, in_range,
    )


async def set_live_window(
    conn, work_session_id: int, live_period: int | None
) -> None:
    """Record how long the worker agreed to share for, and when that runs out."""
    await conn.execute(
        """
        UPDATE work_sessions
           SET start_live_period = $2,
               live_until = CASE
                   WHEN $2::int IS NULL THEN NULL
                   ELSE now() + make_interval(secs => $2)
               END
         WHERE id = $1
        """,
        work_session_id, live_period,
    )


async def track_for_session(work_session_id: int, limit: int = 500) -> list[asyncpg.Record]:
    """One shift's trail, oldest first — the shape a map would draw."""
    return await db.fetch(
        """
        SELECT lat, lng, distance_m, in_range, created_at
          FROM location_pings
         WHERE work_session_id = $1
         ORDER BY created_at
         LIMIT $2
        """,
        work_session_id,
        limit,
    )


async def summary_for_session(work_session_id: int) -> asyncpg.Record:
    """What the trail adds up to, for a finished shift's report row."""
    return await db.fetchrow(
        """
        SELECT count(*)                                        AS pings,
               count(*) FILTER (WHERE NOT in_range)            AS out_of_range,
               max(distance_m)                                 AS furthest_m,
               min(created_at)                                 AS first_at,
               max(created_at)                                 AS last_at
          FROM location_pings
         WHERE work_session_id = $1
        """,
        work_session_id,
    )
