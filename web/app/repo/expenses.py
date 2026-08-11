"""Costs that are not till movements: rent, advertising, paying an influencer.

Kept apart from ``cash_movements`` on purpose. That table is tied to a store
session and cannot represent "the landlord was paid on Sunday", which is exactly
the kind of cost this is for.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg

from app.db import db

# What most shops need on day one, offered rather than imposed.
STARTER_CATEGORIES = [
    "Վարձակալություն",      # rent
    "Գովազդ",               # advertising
    "Ապրանքի գնում",        # buying stock
    "Կոմունալ վճարներ",     # utilities
    "Աշխատավարձ (լրացուցիչ)",  # extra wages beyond per-shift salary
    "Այլ",                  # other
]

RECURRENCES = {"once", "monthly"}
METHODS = {"cash", "card", "bank", "other"}


# -- categories --------------------------------------------------------------

async def categories(owner_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, name FROM expense_categories
         WHERE owner_id = $1 AND is_active ORDER BY lower(name)
        """,
        owner_id,
    )


async def ensure_starter_categories(owner_id: int) -> None:
    """Give a new owner something to pick from rather than an empty list.

    ``ON CONFLICT DO NOTHING`` against the per-owner unique index, so calling
    this on every visit to the page is free and never duplicates.
    """
    await db.execute(
        """
        INSERT INTO expense_categories (owner_id, name)
        SELECT $1, unnest($2::text[])
        ON CONFLICT DO NOTHING
        """,
        owner_id,
        STARTER_CATEGORIES,
    )


async def create_category(owner_id: int, name: str) -> int | None:
    return await db.fetchval(
        """
        INSERT INTO expense_categories (owner_id, name) VALUES ($1, $2)
        ON CONFLICT DO NOTHING RETURNING id
        """,
        owner_id,
        name,
    )


# -- expenses ----------------------------------------------------------------

async def create(
    owner_id: int,
    purpose: str,
    amount: Decimal,
    spent_on: date,
    *,
    store_id: int | None = None,
    category_id: int | None = None,
    method: str = "cash",
    recurrence: str = "once",
    note: str | None = None,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO expenses (owner_id, store_id, category_id, purpose, amount,
                              method, spent_on, recurrence, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        owner_id, store_id, category_id, purpose, amount, method, spent_on,
        recurrence, note,
    )


async def update(
    owner_id: int,
    expense_id: int,
    purpose: str,
    amount: Decimal,
    spent_on: date,
    *,
    store_id: int | None = None,
    category_id: int | None = None,
    method: str = "cash",
    recurrence: str = "once",
    note: str | None = None,
) -> bool:
    result = await db.execute(
        """
        UPDATE expenses
           SET purpose = $3, amount = $4, spent_on = $5, store_id = $6,
               category_id = $7, method = $8, recurrence = $9, note = $10,
               updated_at = now()
         WHERE id = $1 AND owner_id = $2
        """,
        expense_id, owner_id, purpose, amount, spent_on, store_id, category_id,
        method, recurrence, note,
    )
    return result.endswith(" 1")


async def delete(owner_id: int, expense_id: int) -> bool:
    """A real delete. Unlike a sale, an expense references nothing and nothing
    references it, so a mistyped one is best simply gone."""
    result = await db.execute(
        "DELETE FROM expenses WHERE id = $1 AND owner_id = $2", expense_id, owner_id
    )
    return result.endswith(" 1")


async def get(owner_id: int, expense_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT e.id, e.store_id, e.category_id, e.purpose, e.amount, e.method,
               e.spent_on, e.recurrence, e.note
          FROM expenses e WHERE e.id = $1 AND e.owner_id = $2
        """,
        expense_id,
        owner_id,
    )


async def list_between(
    owner_id: int, since: date, until: date, limit: int = 200
) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT e.id, e.purpose, e.amount, e.method, e.spent_on, e.recurrence, e.note,
               e.store_id, s.name AS store_name,
               e.category_id, c.name AS category_name
          FROM expenses e
          LEFT JOIN stores s ON s.id = e.store_id
          LEFT JOIN expense_categories c ON c.id = e.category_id
         WHERE e.owner_id = $1 AND e.spent_on BETWEEN $2 AND $3
         ORDER BY e.spent_on DESC, e.id DESC
         LIMIT $4
        """,
        owner_id,
        since,
        until,
        limit,
    )


async def total_between(owner_id: int, since: date, until: date) -> Decimal:
    return await db.fetchval(
        """
        SELECT coalesce(sum(amount), 0) FROM expenses
         WHERE owner_id = $1 AND spent_on BETWEEN $2 AND $3
        """,
        owner_id,
        since,
        until,
    )


async def total_for_store_on(owner_id: int, store_id: int, day: date) -> Decimal:
    """What this shop spent on this day, out of what the owner typed on /expenses.

    Only expenses attached to this shop. One left as «Ամբողջ բիզնեսը» — rent,
    advertising — belongs to the business rather than to the branch that happened to
    be open, and counting it here would subtract the same rent again from every shop
    trading that day. The statistics page is where business-wide costs are taken off,
    once, over a period.
    """
    return await db.fetchval(
        """
        SELECT coalesce(sum(amount), 0) FROM expenses
         WHERE owner_id = $1 AND store_id = $2 AND spent_on = $3
        """,
        owner_id,
        store_id,
        day,
    )


async def by_category_between(
    owner_id: int, since: date, until: date
) -> list[asyncpg.Record]:
    """What the money went on, largest first — the shape of the outgoings."""
    return await db.fetch(
        """
        SELECT coalesce(c.name, 'Առանց կատեգորիայի') AS category,
               sum(e.amount) AS total, count(*) AS entries
          FROM expenses e
          LEFT JOIN expense_categories c ON c.id = e.category_id
         WHERE e.owner_id = $1 AND e.spent_on BETWEEN $2 AND $3
         GROUP BY 1 ORDER BY total DESC
        """,
        owner_id,
        since,
        until,
    )
