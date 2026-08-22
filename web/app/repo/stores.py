"""Stores, and the geofence candidate query."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db


async def list_for_owner(owner_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, name, address, lat, lng, radius_m, day_start_hour
          FROM stores
         WHERE owner_id = $1 AND is_active
         ORDER BY lower(name)
        """,
        owner_id,
    )


async def list_open_for_owner(owner_id: int) -> list[asyncpg.Record]:
    """The shops trading right now, with the session they are trading in.

    For anything that has to reach a person rather than a record — sending cash
    across, above all. A closed shop has nobody to hand an envelope to and no till
    to put it in, so offering it in a list would be offering a dead end.
    """
    return await db.fetch(
        """
        SELECT s.id, s.name, ss.id AS store_session_id
          FROM stores s
          JOIN store_sessions ss ON ss.store_id = s.id AND ss.closed_at IS NULL
         WHERE s.owner_id = $1 AND s.is_active
         ORDER BY lower(s.name)
        """,
        owner_id,
    )


async def till_balance(conn, owner_id: int, store_id: int) -> Decimal | None:
    """The cash this shop keeps in its drawer between shifts.

    Takes a connection: every caller is deciding or changing it inside a transaction
    that has to include the read.
    """
    return await conn.fetchval(
        "SELECT till_balance FROM stores WHERE id = $1 AND owner_id = $2",
        store_id,
        owner_id,
    )


async def set_till_balance(conn, owner_id: int, store_id: int, amount: Decimal) -> None:
    """Set it. Two callers only — a worker's closing count and the owner correcting
    it — and both book the matching ledger movement in the same transaction."""
    await conn.execute(
        """
        UPDATE stores SET till_balance = $3, updated_at = now()
         WHERE id = $1 AND owner_id = $2
        """,
        store_id,
        owner_id,
        amount,
    )


async def get(owner_id: int, store_id: int) -> asyncpg.Record | None:
    """Always scoped by owner: a store belonging to someone else must read as
    missing, so the caller renders 404 rather than 403."""
    return await db.fetchrow(
        """
        SELECT id, name, address, lat, lng, radius_m, day_start_hour, is_active,
               till_balance, created_at
          FROM stores
         WHERE id = $1 AND owner_id = $2 AND is_active
        """,
        store_id,
        owner_id,
    )


async def create(
    owner_id: int,
    name: str,
    address: str | None,
    lat: float | None,
    lng: float | None,
    radius_m: int,
    day_start_hour: int,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO stores (owner_id, name, address, lat, lng, radius_m, day_start_hour)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        owner_id,
        name,
        address,
        lat,
        lng,
        radius_m,
        day_start_hour,
    )


async def update(
    owner_id: int,
    store_id: int,
    name: str,
    address: str | None,
    lat: float | None,
    lng: float | None,
    radius_m: int,
    day_start_hour: int,
) -> bool:
    result = await db.execute(
        """
        UPDATE stores
           SET name = $3, address = $4, lat = $5, lng = $6, radius_m = $7,
               day_start_hour = $8, updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND is_active
        """,
        store_id,
        owner_id,
        name,
        address,
        lat,
        lng,
        radius_m,
        day_start_hour,
    )
    return result.endswith(" 1")


async def deactivate(owner_id: int, store_id: int) -> bool:
    """Soft delete. Sales and sessions reference this row forever, so it is never
    actually removed."""
    result = await db.execute(
        "UPDATE stores SET is_active = false, updated_at = now() WHERE id = $1 AND owner_id = $2",
        store_id,
        owner_id,
    )
    return result.endswith(" 1")


async def candidates_near(
    owner_id: int, lat: float, lng: float, limit: int = 8
) -> list[asyncpg.Record]:
    """The owner's located stores, nearest first, with each one's own radius.

    Deliberately returns candidates rather than a winner: deciding which of them
    counts as a match is a rule, and rules live in ``app.services.geofence``.
    """
    return await db.fetch(
        """
        SELECT id, name, radius_m,
               distance_m($2, $3, lat, lng) AS distance_m
          FROM stores
         WHERE owner_id = $1 AND is_active AND lat IS NOT NULL
         ORDER BY distance_m
         LIMIT $4
        """,
        owner_id,
        lat,
        lng,
        limit,
    )


async def count_located(owner_id: int) -> int:
    """Used to tell "you are out of range" apart from "no store has coordinates"."""
    return await db.fetchval(
        "SELECT count(*) FROM stores WHERE owner_id = $1 AND is_active AND lat IS NOT NULL",
        owner_id,
    )
