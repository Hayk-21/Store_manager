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
) -> int | None:
    """Add an item, or bring back one that was deleted under the same name.

    Deleting is a soft delete — past sale lines reference the row forever — but
    the name index covers deleted rows too, so the name stayed taken. Adding the
    product back therefore failed against a row the owner could no longer see,
    which is the worst kind of failure: nothing on screen explains it.

    Reviving is better than allowing a second row: the product keeps the sales
    history it already has, and a shop never ends up with two "HQD Cuvie" that
    mean the same thing.

    Returns ``None`` when the name belongs to an item that is still *active* —
    that is a real collision and the caller should say so rather than silently
    overwrite somebody's prices.
    """
    return await db.fetchval(
        """
        INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price,
                           wholesale_price)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (store_id, lower(btrim(name))) DO UPDATE
           SET is_active = true,
               name = EXCLUDED.name,
               count = EXCLUDED.count,
               self_price = EXCLUDED.self_price,
               sell_price = EXCLUDED.sell_price,
               wholesale_price = EXCLUDED.wholesale_price,
               updated_at = now()
         WHERE NOT items.is_active
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
    count: int | None = None,
) -> bool:
    """Rename, reprice, and optionally set the count outright.

    The count is written as an absolute number because that is what the owner is
    doing: they have counted the shelf and this is what is on it. The risk is
    real — a sale recorded between the page loading and the form being submitted
    is overwritten — so it is deliberately *only* written when a number was
    typed, and it is the last word rather than an accumulated delta.

    That is the correct trade for a stock count. The alternative, refusing to
    write what the owner just counted because the number might be stale, leaves
    them with no way to correct the figure at all.
    """
    result = await db.execute(
        """
        UPDATE items
           SET name = $3, self_price = $4, sell_price = $5, wholesale_price = $6,
               count = coalesce($7, count),
               updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND is_active
        """,
        item_id,
        owner_id,
        name,
        self_price,
        sell_price,
        wholesale_price,
        count,
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
