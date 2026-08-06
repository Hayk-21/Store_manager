"""The ledger.

Every money figure in the UI is a SUM over ``cash_movements`` filtered to one
store session. There is no running-balance column: that is what makes "closing
the store resets the till" fall out with nothing running at midnight.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME


async def totals_by_store(owner_id: int) -> list[asyncpg.Record]:
    """One row per active store — backs the fixed footer and the store list.

    ``store_session_id IS NULL`` means the store is closed, and the totals are
    zero because there is no open session for a movement to belong to.
    """
    return await db.fetch(
        """
        SELECT s.id, s.name,
               ss.id        AS store_session_id,
               ss.opened_at AS opened_at,
               coalesce(sum(m.amount) FILTER (WHERE m.method = 'cash'), 0) AS cash,
               coalesce(sum(m.amount) FILTER (WHERE m.method = 'card'), 0) AS card,
               (SELECT count(*) FROM work_sessions ws
                 WHERE ws.store_session_id = ss.id AND ws.ended_at IS NULL) AS on_shift
          FROM stores s
          LEFT JOIN store_sessions ss ON ss.store_id = s.id AND ss.closed_at IS NULL
          LEFT JOIN cash_movements m  ON m.store_session_id = ss.id
         WHERE s.owner_id = $1 AND s.is_active
         GROUP BY s.id, s.name, ss.id, ss.opened_at
         ORDER BY lower(s.name)
        """,
        owner_id,
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
) -> int:
    """Append one row. Takes an explicit connection because every caller is
    already inside a transaction that must include this write."""
    return await conn.fetchval(
        """
        INSERT INTO cash_movements
            (owner_id, store_id, store_session_id, method, kind, amount,
             sale_id, work_session_id, worker_id, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
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
    )


async def ledger_for_session(store_session_id: int) -> list[asyncpg.Record]:
    """Every movement of one session, newest first — the audit trail."""
    return await db.fetch(
        f"""
        SELECT m.id, m.method, m.kind, m.amount, m.note, m.created_at,
               CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END AS worker_name
          FROM cash_movements m
          LEFT JOIN workers w ON w.id = m.worker_id
         WHERE m.store_session_id = $1
         ORDER BY m.created_at DESC, m.id DESC
        """,
        store_session_id,
    )
