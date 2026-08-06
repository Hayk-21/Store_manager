"""Stores, and the geofence candidate query."""

from __future__ import annotations

import asyncpg

from app.db import db


async def list_for_owner(owner_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, name, address, lat, lng, radius_m
          FROM stores
         WHERE owner_id = $1 AND is_active
         ORDER BY lower(name)
        """,
        owner_id,
    )


async def get(owner_id: int, store_id: int) -> asyncpg.Record | None:
    """Always scoped by owner: a store belonging to someone else must read as
    missing, so the caller renders 404 rather than 403."""
    return await db.fetchrow(
        """
        SELECT id, name, address, lat, lng, radius_m, is_active, created_at
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
) -> int:
    return await db.fetchval(
        """
        INSERT INTO stores (owner_id, name, address, lat, lng, radius_m)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        owner_id,
        name,
        address,
        lat,
        lng,
        radius_m,
    )


async def update(
    owner_id: int,
    store_id: int,
    name: str,
    address: str | None,
    lat: float | None,
    lng: float | None,
    radius_m: int,
) -> bool:
    result = await db.execute(
        """
        UPDATE stores
           SET name = $3, address = $4, lat = $5, lng = $6, radius_m = $7, updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND is_active
        """,
        store_id,
        owner_id,
        name,
        address,
        lat,
        lng,
        radius_m,
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
