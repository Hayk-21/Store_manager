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
    is_delivery: bool = False,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO sales (owner_id, store_id, worker_id, work_session_id, store_session_id,
                           payment_method, total, external_id, is_delivery)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
        is_delivery,
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
    """An un-voided receipt of this worker's in *this* shift, locked.

    The most recent one by default, which is what "undo that" means when a
    cashier realises straight away. ``sale_id`` names one instead: the undo
    button under a confirmation has to reverse *that* sale, not whatever
    happened to be last by the time it was tapped.

    Scoped to the current shift either way: a worker undoing their own slip is
    convenient, a worker reaching back into yesterday is not.
    """
    return await conn.fetchrow(
        """
        SELECT id, owner_id, store_id, store_session_id, payment_method, total,
               sold_at, is_delivery
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


async def lines_in_work_session(work_session_id: int) -> list[asyncpg.Record]:
    """Everything this worker has already rung up this shift, by product.

    Rolled up per product rather than per receipt: the worker is checking their own
    day against what they remember selling, and "six HQD Cuvie" is that. Twelve
    separate receipts for one product is a different question, and the owner's
    report answers it.

    Voided receipts are left out — they were taken back, and showing them would
    invite the worker to declare them again in the write-up.

    Grouped by door as well as by product, so the two can be listed apart: a
    delivery is an order the shop took rather than something this worker sold, and
    running the two together tells them they sold six of something when they handed
    over two and posted four.
    """
    return await db.fetch(
        """
        SELECT i.name,
               sa.is_delivery,
               sum(si.quantity)   AS quantity,
               sum(si.line_total) AS total
          FROM sale_items si
          JOIN sales sa ON sa.id = si.sale_id
          JOIN items i  ON i.id = si.item_id
         WHERE sa.work_session_id = $1 AND sa.voided_at IS NULL
         GROUP BY i.name, sa.is_delivery
         ORDER BY lower(i.name)
        """,
        work_session_id,
    )


async def receipts_in_work_session(
    work_session_id: int, limit: int = 20
) -> list[asyncpg.Record]:
    """This worker's own receipts from this shift, newest first, one row each.

    The other read of a shift, ``lines_in_work_session``, rolls sales up per product,
    which is the right shape for "what have I sold today" and the wrong one for "which
    receipt did I get wrong" — a total per product cannot be pointed at and corrected.
    This one keeps the receipts whole and carries their ids, because the correcting is
    done by choosing one.

    Voided receipts come back too, marked. Dropping them would leave a cashier unable
    to tell a receipt they already cancelled from one that was never there, and the
    next thing they do is cancel something else to find out.

    A shift is one worker's, so scoping to the work session scopes to them; there is
    no way to reach a colleague's receipt through this.
    """
    return await db.fetch(
        """
        SELECT sa.id, sa.total, sa.payment_method, sa.sold_at, sa.is_delivery,
               sa.voided_at IS NOT NULL AS voided,
               (SELECT string_agg(i.name || ' ×' || si.quantity, ', ' ORDER BY si.id)
                  FROM sale_items si JOIN items i ON i.id = si.item_id
                 WHERE si.sale_id = sa.id) AS lines
          FROM sales sa
         WHERE sa.work_session_id = $1
         ORDER BY sa.sold_at DESC, sa.id DESC
         LIMIT $2
        """,
        work_session_id,
        limit,
    )


async def count_in_work_session(work_session_id: int) -> int:
    """How many receipts the shift has in total, so a truncated list can say so."""
    return await db.fetchval(
        "SELECT count(*) FROM sales WHERE work_session_id = $1", work_session_id
    )


async def summary_for_work_session(work_session_id: int) -> asyncpg.Record:
    """What one worker sold during one shift, voids excluded — over the counter and
    by delivery, kept apart.

    They are not the same work and the worker should not be shown one number for
    both. A delivery is an order the shop took: the goods leave, the money arrives,
    and somebody enters it — but nobody stood at the counter and sold it. Counting
    it into «you sold 57,500 today» credited the cashier with orders they had no
    hand in, and the same figure decided their bonus.

    So ``receipts``/``cash_total``/``card_total``/``total`` are the counter alone, and
    the delivery figures sit beside them under their own names. Anything reading
    ``total`` gets the honest answer to "what did this worker sell" without having to
    know the distinction exists; anything that wants the shop's whole takings for the
    shift adds the two, or asks the ledger, which never split them in the first place.
    """
    return await db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE NOT is_delivery)                     AS receipts,
               coalesce(sum(total) FILTER (
                   WHERE NOT is_delivery AND payment_method = 'cash'), 0)  AS cash_total,
               coalesce(sum(total) FILTER (
                   WHERE NOT is_delivery AND payment_method = 'card'), 0)  AS card_total,
               coalesce(sum(total) FILTER (WHERE NOT is_delivery), 0)      AS total,
               count(*) FILTER (WHERE is_delivery)                    AS delivery_receipts,
               coalesce(sum(total) FILTER (
                   WHERE is_delivery AND payment_method = 'cash'), 0)  AS delivery_cash,
               coalesce(sum(total) FILTER (
                   WHERE is_delivery AND payment_method = 'card'), 0)  AS delivery_card,
               coalesce(sum(total) FILTER (WHERE is_delivery), 0)      AS delivery_total
          FROM sales
         WHERE work_session_id = $1 AND voided_at IS NULL
        """,
        work_session_id,
    )


# How the receipts on a report can be ordered, and the only two orders there are.
# Looked up rather than interpolated: whatever arrives in the query string can only
# ever choose one of these strings, never contribute to them.
#
# Time is the default because a report is read as an evening. Name is for the other
# question the table gets asked — "did we sell any Aokit tonight" — which time order
# answers by making somebody scan forty rows. Lower-cased so «Aokit» and «aokit» land
# together rather than in two blocks, and each order falls back to time, so equal
# names keep the reading of the evening inside the group.
RECEIPT_ORDERS = {
    "time": "sold_at DESC, id DESC",
    "name": "lower(lines) ASC NULLS LAST, sold_at DESC, id DESC",
}
DEFAULT_RECEIPT_ORDER = "time"

# Which receipts to show at all. Looked up the same way and for the same reason:
# whatever arrives in the query string can only choose one of these, never add to it.
#
# The three are not a partition and are not meant to read as one — a delivery is paid
# in cash or by card like anything else, so «Առաքում» overlaps both of the others.
# They are three questions an owner actually asks of this table («which ones were
# cash», «what went out of the door»), not three buckets that add up to the whole.
RECEIPT_KINDS = {
    "all": "TRUE",
    "cash": "payment_method = 'cash'",
    "card": "payment_method = 'card'",
    "delivery": "is_delivery",
}
DEFAULT_RECEIPT_KIND = "all"


async def receipts_in_store_session(
    store_session_id: int,
    order: str = DEFAULT_RECEIPT_ORDER,
    kind: str = DEFAULT_RECEIPT_KIND,
) -> list[asyncpg.Record]:
    """Every receipt of one session — or one kind of them — newest first or by name.

    Both the sort and the filter are applied outside the inner query rather than
    inside it, because the thing they read — the line «Vanter 30000 ×2», the
    delivery flag — is assembled in the select list, and Postgres will not let an
    expression in ORDER BY or WHERE reach an output alias.
    """
    ordering = RECEIPT_ORDERS.get(order, RECEIPT_ORDERS[DEFAULT_RECEIPT_ORDER])
    only = RECEIPT_KINDS.get(kind, RECEIPT_KINDS[DEFAULT_RECEIPT_KIND])
    return await db.fetch(
        f"""
        SELECT * FROM (
        SELECT sa.id, sa.total, sa.payment_method, sa.sold_at, sa.voided_at,
               sa.superseded_by_sale_id, sa.is_delivery,
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
        ) r
         WHERE {only}
         ORDER BY {ordering}
        """,
        store_session_id,
    )
