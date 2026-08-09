"""Aggregates behind the statistics page.

Every figure here comes from ``sale_items``, never from ``items``. The line
carries ``unit_price`` and ``unit_cost`` as they were at the moment of sale, so
repricing a vape today cannot rewrite what last month earned — which is the
whole reason those snapshots exist.

Voided sales are excluded everywhere: a sale that was taken back is not revenue.

Days are Yerevan days. ``sold_at`` is a timestamptz, and bucketing it by the
server's UTC date would push an evening sale into tomorrow.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# Repeated by every query below, so "what counts as revenue" cannot drift
# between the summary and the chart underneath it. $1 owner, $2/$3 the range,
# $4 the display timezone, $5 an optional store filter.
_WHERE = """
         WHERE sa.owner_id = $1
           AND sa.voided_at IS NULL
           AND (sa.sold_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
           AND ($5::bigint IS NULL OR sa.store_id = $5)
"""

_REVENUE = "coalesce(sum(si.line_total), 0)"
_PROFIT = "coalesce(sum((si.unit_price - si.unit_cost) * si.quantity), 0)"


async def profit_for_session(store_session_id: int) -> Decimal:
    """Gross profit on one store session: what the goods sold for, less what they cost.

    The same expression the statistics page uses, so a session's figure and the period
    that contains it cannot disagree about what profit means. Voided sales are excluded
    for the same reason they are there — the goods went back on the shelf.
    """
    return await db.fetchval(
        f"""
        SELECT {_PROFIT}
          FROM sale_items si
          JOIN sales sa ON sa.id = si.sale_id
         WHERE sa.store_session_id = $1 AND sa.voided_at IS NULL
        """,
        store_session_id,
    )


async def summary(
    owner_id: int, since: date, until: date, tz: str, store_id: int | None = None
) -> asyncpg.Record:
    """Revenue, profit and volume for the whole period."""
    return await db.fetchrow(
        f"""
        SELECT {_REVENUE} AS revenue,
               {_PROFIT}  AS profit,
               coalesce(sum(si.quantity), 0) AS units,
               count(DISTINCT sa.id)         AS receipts,
               coalesce(sum(si.line_total) FILTER (WHERE sa.payment_method = 'cash'), 0)
                   AS cash,
               coalesce(sum(si.line_total) FILTER (WHERE sa.payment_method = 'card'), 0)
                   AS card,
               -- Split by which price list the line came from. 'custom' is a
               -- haggle or a discount and belongs with neither, so it is its own
               -- figure rather than being folded into retail.
               coalesce(sum(si.line_total) FILTER (WHERE si.price_kind = 'wholesale'), 0)
                   AS wholesale,
               coalesce(sum(si.line_total) FILTER (WHERE si.price_kind = 'retail'), 0)
                   AS retail,
               coalesce(sum(si.line_total) FILTER (WHERE si.price_kind = 'custom'), 0)
                   AS custom_priced,
               coalesce(sum(si.quantity) FILTER (WHERE si.price_kind = 'wholesale'), 0)
                   AS wholesale_units,
               -- Same money, different door. Worth its own figure because the
               -- split is not recoverable from anything else in the row.
               coalesce(sum(si.line_total) FILTER (WHERE sa.is_delivery), 0) AS delivered,
               count(DISTINCT sa.id) FILTER (WHERE sa.is_delivery)           AS deliveries
          FROM sale_items si
          JOIN sales sa ON sa.id = si.sale_id
        {_WHERE}
        """,
        owner_id, since, until, tz, store_id,
    )


async def daily(
    owner_id: int, since: date, until: date, tz: str, store_id: int | None = None
) -> list[asyncpg.Record]:
    """One row per day in the range, including days nothing was sold.

    The gap-filling matters: a chart that quietly drops empty days draws a
    smooth line across a week the shop was shut.
    """
    return await db.fetch(
        f"""
        WITH days AS (
            SELECT generate_series($2::date, $3::date, '1 day')::date AS day
        ),
        sold AS (
            SELECT (sa.sold_at AT TIME ZONE $4)::date AS day,
                   sum(si.line_total) AS revenue,
                   sum((si.unit_price - si.unit_cost) * si.quantity) AS profit,
                   sum(si.line_total) FILTER (WHERE sa.payment_method = 'cash') AS cash,
                   sum(si.line_total) FILTER (WHERE sa.payment_method = 'card') AS card,
                   count(DISTINCT sa.id) AS receipts
              FROM sale_items si
              JOIN sales sa ON sa.id = si.sale_id
            {_WHERE}
             GROUP BY 1
        )
        SELECT d.day,
               coalesce(s.revenue, 0)  AS revenue,
               coalesce(s.profit, 0)   AS profit,
               coalesce(s.cash, 0)     AS cash,
               coalesce(s.card, 0)     AS card,
               coalesce(s.receipts, 0) AS receipts
          FROM days d
          LEFT JOIN sold s ON s.day = d.day
         ORDER BY d.day
        """,
        owner_id, since, until, tz, store_id,
    )


async def by_hour(
    owner_id: int, since: date, until: date, tz: str, store_id: int | None = None
) -> list[asyncpg.Record]:
    """Takings by hour of the trading day, over the whole period.

    The one question the daily chart cannot answer: *when* does this shop sell. It is
    what a rota is built from, and until now the only way to see it was to read a
    session's receipts one row at a time.

    Every hour of the 24 is returned, including the dead ones, for the same reason the
    daily chart fills its gaps: dropping the quiet hours draws a shop that trades evenly
    from open to close.
    """
    return await db.fetch(
        f"""
        WITH hours AS (SELECT generate_series(0, 23) AS hour),
        sold AS (
            SELECT extract(hour FROM sa.sold_at AT TIME ZONE $4)::int AS hour,
                   sum(si.line_total) AS revenue,
                   count(DISTINCT sa.id) AS receipts
              FROM sale_items si
              JOIN sales sa ON sa.id = si.sale_id
            {_WHERE}
             GROUP BY 1
        )
        SELECT h.hour,
               coalesce(s.revenue, 0)  AS revenue,
               coalesce(s.receipts, 0) AS receipts
          FROM hours h
          LEFT JOIN sold s ON s.hour = h.hour
         ORDER BY h.hour
        """,
        owner_id, since, until, tz, store_id,
    )


async def top_items(
    owner_id: int,
    since: date,
    until: date,
    tz: str,
    store_id: int | None = None,
    limit: int = 10,
) -> list[asyncpg.Record]:
    """What actually earns, biggest first.

    Grouped by name rather than id: the same product stocked in two shops is one
    line to the owner, not two.
    """
    return await db.fetch(
        f"""
        SELECT i.name,
               sum(si.quantity) AS units,
               {_REVENUE}       AS revenue,
               {_PROFIT}        AS profit
          FROM sale_items si
          JOIN sales sa ON sa.id = si.sale_id
          JOIN items i  ON i.id = si.item_id
        {_WHERE}
         GROUP BY i.name
         ORDER BY revenue DESC
         LIMIT $6
        """,
        owner_id, since, until, tz, store_id, limit,
    )


async def by_store(
    owner_id: int, since: date, until: date, tz: str
) -> list[asyncpg.Record]:
    """Which shop earns what — the comparison the owner actually wants."""
    return await db.fetch(
        f"""
        SELECT st.name,
               sum(si.quantity)      AS units,
               {_REVENUE}            AS revenue,
               {_PROFIT}             AS profit,
               count(DISTINCT sa.id) AS receipts
          FROM sale_items si
          JOIN sales sa  ON sa.id = si.sale_id
          JOIN stores st ON st.id = sa.store_id
        {_WHERE}
         GROUP BY st.name
         ORDER BY revenue DESC
        """,
        owner_id, since, until, tz, None,
    )


async def by_worker(
    owner_id: int, since: date, until: date, tz: str, store_id: int | None = None
) -> list[asyncpg.Record]:
    """Who sold what. Wages are counted separately, by shift."""
    return await db.fetch(
        f"""
        SELECT {DISPLAY_NAME} AS worker_name,
               sum(si.quantity)      AS units,
               {_REVENUE}            AS revenue,
               {_PROFIT}             AS profit,
               count(DISTINCT sa.id) AS receipts
          FROM sale_items si
          JOIN sales sa   ON sa.id = si.sale_id
          JOIN workers w  ON w.id = sa.worker_id
        {_WHERE}
         GROUP BY {DISPLAY_NAME}
         ORDER BY revenue DESC
        """,
        owner_id, since, until, tz, store_id,
    )


async def salaries_between(
    owner_id: int, since: date, until: date, tz: str, store_id: int | None = None
) -> Decimal:
    """What the shifts in this period cost in wages.

    Bucketed by when the shift *ended*, which is when the money left the till.
    """
    return await db.fetchval(
        """
        SELECT coalesce(sum(salary_paid), 0)
          FROM work_sessions
         WHERE owner_id = $1 AND ended_at IS NOT NULL
           AND (ended_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
           AND ($5::bigint IS NULL OR store_id = $5)
        """,
        owner_id, since, until, tz, store_id,
    )


async def stock_value(owner_id: int, store_id: int | None = None) -> asyncpg.Record:
    """What is sitting on the shelves right now, at cost and at retail."""
    return await db.fetchrow(
        """
        SELECT coalesce(sum(self_price * count), 0) AS at_cost,
               coalesce(sum(sell_price * count), 0) AS at_retail,
               coalesce(sum(possible_profit), 0)    AS possible_profit,
               coalesce(sum(count), 0)              AS units
          FROM items
         WHERE owner_id = $1 AND is_active
           AND ($2::bigint IS NULL OR store_id = $2)
        """,
        owner_id, store_id,
    )
