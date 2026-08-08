"""Receipts and their lines."""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# The same expression, for the worker who voided a receipt. Aliased separately
# because both workers rows appear in one query.
VOIDED_BY_NAME = DISPLAY_NAME.replace("w.", "vw.")


async def by_external_id(worker_id: int, external_id: str) -> asyncpg.Record | None:
    """The idempotency lookup: a retried POST must find its original receipt."""
    return await db.fetchrow(
        """
        SELECT id, store_id, work_session_id, store_session_id, payment_method,
               total, sold_at, voided_at
          FROM sales WHERE worker_id = $1 AND external_id = $2
        """,
        worker_id,
        external_id,
    )


async def lines_for_sale(sale_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT si.item_id, si.quantity, si.unit_price, si.line_total, si.price_kind,
               i.name, i.count AS remaining_count
          FROM sale_items si
          JOIN items i ON i.id = si.item_id
         WHERE si.sale_id = $1
         ORDER BY si.id
        """,
        sale_id,
    )


async def insert_sale(
    conn,
    *,
    owner_id: int,
    store_id: int,
    worker_id: int,
    work_session_id: int,
    store_session_id: int,
    payment_method: str,
    total: Decimal,
    external_id: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO sales (owner_id, store_id, worker_id, work_session_id, store_session_id,
                           payment_method, total, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        owner_id,
        store_id,
        worker_id,
        work_session_id,
        store_session_id,
        payment_method,
        total,
        external_id,
    )


async def insert_lines(conn, owner_id: int, sale_id: int, lines: list[dict]) -> None:
    """One round trip for the whole basket."""
    await conn.executemany(
        """
        INSERT INTO sale_items (owner_id, sale_id, item_id, quantity, unit_price,
                                unit_cost, line_total, price_kind)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        [
            (
                owner_id,
                sale_id,
                line["item_id"],
                line["quantity"],
                line["unit_price"],
                line["unit_cost"],
                line["line_total"],
                line.get("price_kind", "retail"),
            )
            for line in lines
        ],
    )


async def lock_last_voidable(
    conn, worker_id: int, work_session_id: int, sale_id: int | None = None
) -> asyncpg.Record | None:
    """An un-voided receipt of this worker''s in *this* shift, locked.

    The most recent one by default, which is what "undo that" means when a
    cashier realises straight away. ``sale_id`` names one instead: the undo
    button under a confirmation has to reverse *that* sale, not whatever
    happened to be last by the time it was tapped.

    Scoped to the current shift either way: a worker undoing their own slip is
    convenient, a worker reaching back into yesterday is not.
    """
    return await conn.fetchrow(
        """
        SELECT id, owner_id, store_id, store_session_id, payment_method, total, sold_at
          FROM sales
         WHERE worker_id = $1 AND work_session_id = $2 AND voided_at IS NULL
           AND ($3::bigint IS NULL OR id = $3)
         ORDER BY sold_at DESC, id DESC
         LIMIT 1
           FOR UPDATE
        """,
        worker_id,
        work_session_id,
        sale_id,
    )


async def lines_for_void(conn, sale_id: int) -> list[asyncpg.Record]:
    """Lines in ascending item order, so restoring stock cannot deadlock against
    a concurrent sale that locks in the same order."""
    return await conn.fetch(
        """
        SELECT item_id, quantity, unit_price, line_total, price_kind
          FROM sale_items WHERE sale_id = $1 ORDER BY item_id
        """,
        sale_id,
    )


async def mark_voided(conn, sale_id: int, worker_id: int, reason: str | None) -> None:
    await conn.execute(
        """
        UPDATE sales
           SET voided_at = now(), voided_by_worker_id = $2, void_reason = $3
         WHERE id = $1 AND voided_at IS NULL
        """,
        sale_id,
        worker_id,
        reason,
    )


async def summary_for_work_session(work_session_id: int) -> asyncpg.Record:
    """What one worker sold during one shift, voids excluded."""
    return await db.fetchrow(
        """
        SELECT count(*)                                                      AS receipts,
               coalesce(sum(total) FILTER (WHERE payment_method = 'cash'), 0) AS cash_total,
               coalesce(sum(total) FILTER (WHERE payment_method = 'card'), 0) AS card_total,
               coalesce(sum(total), 0)                                        AS total
          FROM sales
         WHERE work_session_id = $1 AND voided_at IS NULL
        """,
        work_session_id,
    )


async def receipts_in_store_session(store_session_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"""
        SELECT sa.id, sa.total, sa.payment_method, sa.sold_at, sa.voided_at,
               sa.superseded_by_sale_id,
               {DISPLAY_NAME} AS worker_name,
               {VOIDED_BY_NAME} AS voided_by_name,
               (SELECT string_agg(i.name || ' ×' || si.quantity, ', ' ORDER BY si.id)
                  FROM sale_items si JOIN items i ON i.id = si.item_id
                 WHERE si.sale_id = sa.id) AS lines,
               (SELECT count(*) FROM sale_items si WHERE si.sale_id = sa.id) AS line_count,
               one.item_id, one.quantity, one.unit_price, one.price_kind
          FROM sales sa
          JOIN workers w ON w.id = sa.worker_id
          LEFT JOIN workers vw ON vw.id = sa.voided_by_worker_id
          -- The first line, so a single-line receipt can be edited in place.
          -- A close-out writes one sale per line, so in practice that is most
          -- of them; anything longer only gets the void button.
          LEFT JOIN LATERAL (
               SELECT si.item_id, si.quantity, si.unit_price, si.price_kind
                 FROM sale_items si WHERE si.sale_id = sa.id ORDER BY si.id LIMIT 1
          ) one ON true
         WHERE sa.store_session_id = $1
         ORDER BY sa.sold_at DESC, sa.id DESC
        """,
        store_session_id,
    )
