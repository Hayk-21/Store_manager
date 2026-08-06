"""Stock. The five columns requirement 2 asks for live here."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db

# Only these columns may be sorted on. The value arrives from a query string, so
# it is interpolated into SQL and must never be anything a visitor chose.
SORTS: dict[str, str] = {
    "name": "lower(name)",
    "count": "count",
    "self_price": "self_price",
    "sell_price": "sell_price",
    "wholesale_price": "wholesale_price",
    "possible_profit": "possible_profit",
}
DEFAULT_SORT = "name"


async def list_for_store(
    owner_id: int, store_id: int, sort: str = DEFAULT_SORT, descending: bool = False
) -> list[asyncpg.Record]:
    column = SORTS.get(sort, SORTS[DEFAULT_SORT])
    direction = "DESC" if descending else "ASC"
    return await db.fetch(
        f"""
        SELECT id, name, count, self_price, sell_price, wholesale_price, possible_profit
          FROM items
         WHERE owner_id = $1 AND store_id = $2 AND is_active
         ORDER BY {column} {direction}, id
        """,
        owner_id,
        store_id,
    )


async def summary_for_store(owner_id: int, store_id: int) -> asyncpg.Record:
    return await db.fetchrow(
        """
        SELECT count(*)                        AS lines,
               coalesce(sum(count), 0)         AS units,
               coalesce(sum(self_price * count), 0) AS stock_cost,
               coalesce(sum(possible_profit), 0)    AS possible_profit
          FROM items
         WHERE owner_id = $1 AND store_id = $2 AND is_active
        """,
        owner_id,
        store_id,
    )


async def get(owner_id: int, item_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, store_id, name, count, self_price, sell_price, wholesale_price,
               possible_profit, is_active
          FROM items WHERE id = $1 AND owner_id = $2
        """,
        item_id,
        owner_id,
    )


async def create(
    owner_id: int,
    store_id: int,
    name: str,
    count: int,
    self_price: Decimal,
    sell_price: Decimal,
    wholesale_price: Decimal | None = None,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price,
                           wholesale_price)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        owner_id,
        store_id,
        name,
        count,
        self_price,
        sell_price,
        wholesale_price,
    )


async def update_details(
    owner_id: int,
    item_id: int,
    name: str,
    self_price: Decimal,
    sell_price: Decimal,
    wholesale_price: Decimal | None = None,
) -> bool:
    """Rename and reprice. Deliberately does NOT touch ``count``.

    Writing an absolute count here would silently undo any sale the bot recorded
    between the page rendering and the form being submitted.
    """
    result = await db.execute(
        """
        UPDATE items
           SET name = $3, self_price = $4, sell_price = $5, wholesale_price = $6,
               updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND is_active
        """,
        item_id,
        owner_id,
        name,
        self_price,
        sell_price,
        wholesale_price,
    )
    return result.endswith(" 1")


async def restock(owner_id: int, item_id: int, delta: int) -> int | None:
    """Add (or remove) stock as a delta, and return the new count.

    A delta is race-safe against a concurrent sale in a way that setting an
    absolute number is not: both writes apply, and neither is lost.
    """
    return await db.fetchval(
        """
        UPDATE items
           SET count = count + $3, updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND is_active AND count + $3 >= 0
        RETURNING count
        """,
        item_id,
        owner_id,
        delta,
    )


async def deactivate(owner_id: int, item_id: int) -> bool:
    """Soft delete: past sale lines reference this row forever."""
    result = await db.execute(
        "UPDATE items SET is_active = false, updated_at = now() WHERE id = $1 AND owner_id = $2",
        item_id,
        owner_id,
    )
    return result.endswith(" 1")


async def search_in_store(
    store_id: int, query: str, limit: int = 12
) -> list[asyncpg.Record]:
    """Name search for the bot's sell flow.

    Ordered so an exact match beats a prefix match beats an anywhere match; a
    cashier typing the first few letters gets the obvious item at the top.
    """
    needle = query.strip().lower()
    return await db.fetch(
        """
        SELECT id, name, count, sell_price, wholesale_price
          FROM items
         WHERE store_id = $1 AND is_active AND position($2 in lower(name)) > 0
         ORDER BY CASE
                    WHEN lower(name) = $2 THEN 0
                    WHEN lower(name) LIKE $2 || '%' THEN 1
                    ELSE 2
                  END,
                  lower(name)
         LIMIT $3
        """,
        store_id,
        needle,
        limit,
    )


async def list_in_store_for_bot(
    store_id: int, limit: int = 50, offset: int = 0
) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, name, count, sell_price, wholesale_price
          FROM items
         WHERE store_id = $1 AND is_active
         ORDER BY lower(name)
         LIMIT $2 OFFSET $3
        """,
        store_id,
        limit,
        offset,
    )


async def count_in_store(store_id: int) -> int:
    return await db.fetchval(
        "SELECT count(*) FROM items WHERE store_id = $1 AND is_active", store_id
    )
