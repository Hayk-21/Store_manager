"""The ledger.

Every money figure in the UI is a SUM over ``cash_movements``. There is no
running-balance column, which is what lets the same rows answer two different
questions without either one needing a job to run:

* filtered to the *open store session* — what is in the till right now, so
  closing the store settles it and the next opening starts at zero;
* filtered to *this store's trading day* — what the shop sold today, which
  outlives a close and starts again at the store's own boundary hour.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.config import settings
from app.db import db
from app.repo.workers import DISPLAY_NAME


def _day_start(tz_param: str) -> str:
    """SQL for the moment store ``s``'s trading day began, as a local timestamp.

    Subtracting the boundary hour before truncating and adding it back is what
    makes "before 06:00 still belongs to yesterday" fall out arithmetically
    rather than needing a branch. ``tz_param`` is the placeholder holding the
    display timezone in the calling query.
    """
    return (
        f"date_trunc('day', (now() AT TIME ZONE {tz_param})"
        " - make_interval(hours => s.day_start_hour))"
        " + make_interval(hours => s.day_start_hour)"
    )


async def totals_by_store(owner_id: int) -> list[asyncpg.Record]:
    """One row per active store — backs the fixed footer and the store list.

    Two different figures, and the difference matters:

    * ``cash`` / ``card`` — what is in the till of the *open session*. Closing
      settles the till, so these are zero when the store is closed.
    * ``day_cash`` / ``day_card`` — what the store has *sold today*, across every
      session of the trading day. These survive a close, because the shop still
      took that money this morning, and start again at the store's own boundary
      hour.
    """
    return await db.fetch(
        f"""
        SELECT s.id, s.name, s.day_start_hour,
               ss.id        AS store_session_id,
               ss.opened_at AS opened_at,
               coalesce(sum(m.amount) FILTER (WHERE m.method = 'cash'), 0) AS cash,
               coalesce(sum(m.amount) FILTER (WHERE m.method = 'card'), 0) AS card,
               (SELECT count(*) FROM work_sessions ws
                 WHERE ws.store_session_id = ss.id AND ws.ended_at IS NULL) AS on_shift,
               d.day_start,
               d.day_cash, d.day_card, d.day_total, d.day_receipts
          FROM stores s
          LEFT JOIN store_sessions ss ON ss.store_id = s.id AND ss.closed_at IS NULL
          LEFT JOIN cash_movements m  ON m.store_session_id = ss.id
          CROSS JOIN LATERAL (
              SELECT dd.day_start,
                     coalesce(sum(dm.amount) FILTER (WHERE dm.method = 'cash'), 0) AS day_cash,
                     coalesce(sum(dm.amount) FILTER (WHERE dm.method = 'card'), 0) AS day_card,
                     coalesce(sum(dm.amount), 0)                                   AS day_total,
                     count(DISTINCT dm.sale_id) FILTER (WHERE dm.kind = 'sale')     AS day_receipts
                FROM (SELECT {_day_start("$2")} AS day_start) dd
                LEFT JOIN cash_movements dm
                       ON dm.store_id = s.id
                      AND dm.kind IN ('sale', 'void')
                      AND (dm.created_at AT TIME ZONE $2) >= dd.day_start
               GROUP BY dd.day_start
          ) d
         WHERE s.owner_id = $1 AND s.is_active
         GROUP BY s.id, s.name, s.day_start_hour, ss.id, ss.opened_at,
                  d.day_start, d.day_cash, d.day_card, d.day_total, d.day_receipts
         ORDER BY lower(s.name)
        """,
        owner_id,
        settings.tzname,
    )


async def day_totals_for_store(owner_id: int, store_id: int) -> asyncpg.Record | None:
    """Today's takings for one store, whether or not it is open now.

    Sales and their reversals only: a salary paid out of the till is a cost, not
    a negative sale, and netting it off here would understate what the shop sold.
    """
    return await db.fetchrow(
        f"""
        SELECT d.day_start,
               coalesce(sum(dm.amount) FILTER (WHERE dm.method = 'cash'), 0) AS day_cash,
               coalesce(sum(dm.amount) FILTER (WHERE dm.method = 'card'), 0) AS day_card,
               coalesce(sum(dm.amount), 0)                                   AS day_total,
               count(DISTINCT dm.sale_id) FILTER (WHERE dm.kind = 'sale')     AS day_receipts,
               (SELECT count(*) FROM store_sessions ss
                 WHERE ss.store_id = s.id
                   AND (ss.opened_at AT TIME ZONE $3) >= d.day_start)        AS day_sessions
          FROM stores s
          CROSS JOIN LATERAL (SELECT {_day_start("$3")} AS day_start) d
          LEFT JOIN cash_movements dm
                 ON dm.store_id = s.id
                AND dm.kind IN ('sale', 'void')
                AND (dm.created_at AT TIME ZONE $3) >= d.day_start
         WHERE s.id = $1 AND s.owner_id = $2 AND s.is_active
         GROUP BY s.id, d.day_start
        """,
        store_id,
        owner_id,
        settings.tzname,
    )


_TOTALS_SQL = """
        SELECT coalesce(sum(amount) FILTER (WHERE method = 'cash'), 0)  AS cash,
               coalesce(sum(amount) FILTER (WHERE method = 'card'), 0)  AS card,
               coalesce(sum(amount) FILTER (WHERE kind = 'sale'), 0)    AS sales,
               coalesce(-sum(amount) FILTER (WHERE kind = 'void'), 0)   AS voided,
               coalesce(-sum(amount) FILTER (WHERE kind = 'salary'), 0) AS salaries,
               coalesce(-sum(amount) FILTER (WHERE kind = 'withdrawal'), 0) AS withdrawn,
               coalesce(sum(amount) FILTER (WHERE kind = 'deposit'), 0) AS deposited
          FROM cash_movements
         WHERE store_session_id = $1
"""


async def totals_on(conn, store_session_id: int) -> asyncpg.Record:
    """The same totals, read on a caller-supplied connection.

    Anything computing a closing snapshot must use this rather than
    ``totals_for_session``: a pooled read would run on a different connection and
    would not see the salary rows the transaction has just written.
    """
    return await conn.fetchrow(_TOTALS_SQL, store_session_id)


async def totals_for_session(store_session_id: int) -> asyncpg.Record:
    """Cash, card and the breakdown behind them, for one store session."""
    return await db.fetchrow(
        """
        SELECT coalesce(sum(amount) FILTER (WHERE method = 'cash'), 0)  AS cash,
               coalesce(sum(amount) FILTER (WHERE method = 'card'), 0)  AS card,
               coalesce(sum(amount) FILTER (WHERE kind = 'sale'), 0)    AS sales,
               coalesce(-sum(amount) FILTER (WHERE kind = 'void'), 0)   AS voided,
               coalesce(-sum(amount) FILTER (WHERE kind = 'salary'), 0) AS salaries,
               coalesce(-sum(amount) FILTER (WHERE kind = 'withdrawal'), 0) AS withdrawn,
               coalesce(sum(amount) FILTER (WHERE kind = 'deposit'), 0) AS deposited
          FROM cash_movements
         WHERE store_session_id = $1
        """,
        store_session_id,
    )


async def insert_movement(
    conn,
    *,
    owner_id: int,
    store_id: int,
    store_session_id: int,
    method: str,
    kind: str,
    amount: Decimal,
    sale_id: int | None = None,
    work_session_id: int | None = None,
    worker_id: int | None = None,
    note: str | None = None,
    created_by: str = "system",
) -> int:
    """Append one row. Takes an explicit connection because every caller is
    already inside a transaction that must include this write.

    ``created_by`` separates what the system recorded from what the owner typed
    afterwards, so a reader of the ledger can tell a fact from an amendment.
    """
    return await conn.fetchval(
        """
        INSERT INTO cash_movements
            (owner_id, store_id, store_session_id, method, kind, amount,
             sale_id, work_session_id, worker_id, note, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        owner_id,
        store_id,
        store_session_id,
        method,
        kind,
        amount,
        sale_id,
        work_session_id,
        worker_id,
        note,
        created_by,
    )


async def ledger_for_session(store_session_id: int) -> list[asyncpg.Record]:
    """Every movement of one session, newest first — the audit trail."""
    return await db.fetch(
        f"""
        SELECT m.id, m.method, m.kind, m.amount, m.note, m.created_at, m.created_by,
               CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END AS worker_name
          FROM cash_movements m
          LEFT JOIN workers w ON w.id = m.worker_id
         WHERE m.store_session_id = $1
         ORDER BY m.created_at DESC, m.id DESC
        """,
        store_session_id,
    )
