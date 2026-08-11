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
    * ``left_in_till`` — the shop's own float, straight off ``stores``. The one
      figure here that outlives a session, because the money does: it sits in the
      drawer overnight and is what the next shift opens with. Without it a closed
      shop reported nothing at all about its drawer, and the cash in it was invisible
      to the owner until somebody opened up again.
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
               d.day_cash, d.day_card, d.day_total, d.day_receipts,
               -- The shop's float: a column now, not the last count read back
               -- out of history. Outlives every session, because the cash does.
               s.till_balance    AS left_in_till
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
                  d.day_start, d.day_cash, d.day_card, d.day_total, d.day_receipts,
                  s.till_balance
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
               -- The takings split by how they were paid, which is *not* the two
               -- balances above: those have wages, petty cash and the handover
               -- already taken off them. An owner reading «cash 2,500 · card 16,000»
               -- beside «sales 101,500» is being shown three numbers that cannot
               -- all be about the same thing, and the cash sales were 85,500.
               coalesce(sum(amount) FILTER (WHERE kind = 'sale' AND method = 'cash'), 0)
                   AS sale_cash,
               coalesce(sum(amount) FILTER (WHERE kind = 'sale' AND method = 'card'), 0)
                   AS sale_card,
               -- The same three with the reversals folded in. A voided sale is not a
               -- sale, and a day where everything was taken back read as a day that
               -- sold 7,000 — the void row carries the method of the sale it reverses,
               -- so the split nets out with it.
               coalesce(sum(amount) FILTER (WHERE kind IN ('sale', 'void')), 0)
                   AS net_sales,
               coalesce(sum(amount) FILTER (
                   WHERE kind IN ('sale', 'void') AND method = 'cash'), 0)
                   AS net_sale_cash,
               coalesce(sum(amount) FILTER (
                   WHERE kind IN ('sale', 'void') AND method = 'card'), 0)
                   AS net_sale_card,
               coalesce(-sum(amount) FILTER (WHERE kind = 'void'), 0)   AS voided,
               coalesce(-sum(amount) FILTER (WHERE kind = 'salary'), 0) AS salaries,
               coalesce(-sum(amount) FILTER (WHERE kind = 'bonus'), 0)  AS bonuses,
               coalesce(-sum(amount) FILTER (WHERE kind = 'withdrawal'), 0) AS withdrawn,
               coalesce(sum(amount) FILTER (WHERE kind = 'deposit'), 0) AS deposited,
               -- The float the shop carried in this morning: the one deposit nobody
               -- typed. It is what stays on the premises when a session ends without
               -- anybody counting the drawer, because nothing then changes the shop's
               -- balance — so it is also what tomorrow will open with.
               coalesce(sum(amount) FILTER (
                   WHERE kind = 'deposit' AND created_by = 'system'), 0) AS carried_in,
               -- Signed, unlike the two above: an owner's correction can go either
               -- way, and forcing it positive would make «+5,000» and «−5,000» look
               -- like the same entry in the one line that explains the till.
               coalesce(sum(amount) FILTER (WHERE kind = 'adjustment'), 0) AS adjusted
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
               -- The takings split by how they were paid, which is *not* the two
               -- balances above: those have wages, petty cash and the handover
               -- already taken off them. An owner reading «cash 2,500 · card 16,000»
               -- beside «sales 101,500» is being shown three numbers that cannot
               -- all be about the same thing, and the cash sales were 85,500.
               coalesce(sum(amount) FILTER (WHERE kind = 'sale' AND method = 'cash'), 0)
                   AS sale_cash,
               coalesce(sum(amount) FILTER (WHERE kind = 'sale' AND method = 'card'), 0)
                   AS sale_card,
               -- The same three with the reversals folded in. A voided sale is not a
               -- sale, and a day where everything was taken back read as a day that
               -- sold 7,000 — the void row carries the method of the sale it reverses,
               -- so the split nets out with it.
               coalesce(sum(amount) FILTER (WHERE kind IN ('sale', 'void')), 0)
                   AS net_sales,
               coalesce(sum(amount) FILTER (
                   WHERE kind IN ('sale', 'void') AND method = 'cash'), 0)
                   AS net_sale_cash,
               coalesce(sum(amount) FILTER (
                   WHERE kind IN ('sale', 'void') AND method = 'card'), 0)
                   AS net_sale_card,
               coalesce(-sum(amount) FILTER (WHERE kind = 'void'), 0)   AS voided,
               coalesce(-sum(amount) FILTER (WHERE kind = 'salary'), 0) AS salaries,
               coalesce(-sum(amount) FILTER (WHERE kind = 'bonus'), 0)  AS bonuses,
               coalesce(-sum(amount) FILTER (WHERE kind = 'withdrawal'), 0) AS withdrawn,
               coalesce(sum(amount) FILTER (WHERE kind = 'deposit'), 0) AS deposited,
               -- The float the shop carried in this morning: the one deposit nobody
               -- typed. It is what stays on the premises when a session ends without
               -- anybody counting the drawer, because nothing then changes the shop's
               -- balance — so it is also what tomorrow will open with.
               coalesce(sum(amount) FILTER (
                   WHERE kind = 'deposit' AND created_by = 'system'), 0) AS carried_in,
               -- Signed, unlike the two above: an owner's correction can go either
               -- way, and forcing it positive would make «+5,000» and «−5,000» look
               -- like the same entry in the one line that explains the till.
               coalesce(sum(amount) FILTER (WHERE kind = 'adjustment'), 0) AS adjusted
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
    external_id: str | None = None,
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
             sale_id, work_session_id, worker_id, note, created_by, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
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
        external_id,
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


async def taken_out_during_shift(work_session_id: int) -> list[asyncpg.Record]:
    """What the worker took out of the drawer during this shift, and what for.

    Only their own withdrawals: a salary, a handover and the float carried in are
    all movements on the same shift, and none of them is the worker answering for
    petty cash. The reason matters more than the amount here — this is read back to
    them before they end the shift so they can spot the one they forgot.
    """
    return await db.fetch(
        """
        SELECT id, -amount AS amount, note, created_at
          FROM cash_movements
         WHERE work_session_id = $1 AND kind = 'withdrawal' AND created_by = 'worker'
         ORDER BY created_at, id
        """,
        work_session_id,
    )


async def withdrawn_by_worker_on(conn, work_session_id: int) -> Decimal:
    """What this worker has already taken out during this shift, as a positive
    number. Read on the caller's connection because it decides whether the write
    about to happen is allowed, and the shift row is locked around both.
    """
    return await conn.fetchval(
        """
        SELECT coalesce(-sum(amount), 0)
          FROM cash_movements
         WHERE work_session_id = $1 AND kind = 'withdrawal' AND created_by = 'worker'
        """,
        work_session_id,
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    """The idempotency lookup for a movement the bot wrote.

    Carries the session's totals with it, because the caller is rebuilding a
    reply that quotes them and a second query could see a different moment.
    """
    return await db.fetchrow(
        """
        SELECT m.id, m.amount, m.note, m.store_session_id,
               coalesce((SELECT sum(x.amount) FILTER (WHERE x.method = 'cash')
                           FROM cash_movements x
                          WHERE x.store_session_id = m.store_session_id), 0) AS cash,
               coalesce((SELECT sum(x.amount) FILTER (WHERE x.method = 'card')
                           FROM cash_movements x
                          WHERE x.store_session_id = m.store_session_id), 0) AS card
          FROM cash_movements m
         WHERE m.owner_id = $1 AND m.external_id = $2
        """,
        owner_id,
        external_id,
    )
