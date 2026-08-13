"""Store sessions (the accounting period) and work sessions (a worker's shift)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME

# -- store sessions ---------------------------------------------------------

async def open_for_store(store_id: int) -> asyncpg.Record | None:
    """The store's currently-open session, if it has one."""
    return await db.fetchrow(
        """
        SELECT ss.id, ss.store_id, ss.opened_at, ss.opened_by_worker_id, w.name AS opened_by_name
          FROM store_sessions ss
          LEFT JOIN workers w ON w.id = ss.opened_by_worker_id
         WHERE ss.store_id = $1 AND ss.closed_at IS NULL
        """,
        store_id,
    )


async def lock_open_for_store(conn, owner_id: int, store_id: int) -> asyncpg.Record | None:
    """The open session, locked for update.

    Held while closing so a sale cannot land between reading the totals and
    writing the snapshot.
    """
    return await conn.fetchrow(
        """
        SELECT id, store_id, opened_at
          FROM store_sessions
         WHERE store_id = $1 AND owner_id = $2 AND closed_at IS NULL
           FOR UPDATE
        """,
        store_id,
        owner_id,
    )


async def open_store_session(
    conn, owner_id: int, store_id: int, worker_id: int, opened_day: date
) -> int | None:
    """Open a session, or return None when the store already has one.

    ``ON CONFLICT DO NOTHING`` against the partial unique index is what makes two
    workers arriving at the same moment safe: exactly one insert wins and the
    other joins the session that won.
    """
    return await conn.fetchval(
        """
        INSERT INTO store_sessions (owner_id, store_id, opened_by_worker_id, opened_day)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        owner_id,
        store_id,
        worker_id,
        opened_day,
    )


async def close_store_session(
    conn,
    store_session_id: int,
    cash: Decimal,
    card: Decimal,
    salaries: Decimal,
    closed_by: str,
) -> None:
    await conn.execute(
        """
        UPDATE store_sessions
           SET closed_at = now(), cash_at_close = $2, card_at_close = $3,
               salaries_at_close = $4, closed_by = $5
         WHERE id = $1 AND closed_at IS NULL
        """,
        store_session_id,
        cash,
        card,
        salaries,
        closed_by,
    )


async def stale_open_sessions(hours: int) -> list[asyncpg.Record]:
    """Sessions somebody forgot to close."""
    return await db.fetch(
        """
        SELECT id, owner_id, store_id, opened_at
          FROM store_sessions
         WHERE closed_at IS NULL
           AND opened_at < now() - make_interval(hours => $1)
         ORDER BY opened_at
        """,
        hours,
    )


async def recent_store_sessions(
    owner_id: int, tz: str, limit: int = 50, offset: int = 0
) -> list[asyncpg.Record]:
    """Backs /reports: one row per time a store was open, newest first.

    Every figure the row shows is the same figure the detail page under it shows,
    read from the same places — the ledger for the money, the sale lines for the
    margin — so opening a row can never contradict the row you opened. That is why
    the parts of the profit are columns here rather than a number worked out a
    second way: the subtraction itself is done in one place, by
    ``statistics.session_profit``.

    ``tz`` is the display timezone, needed for the one figure that is asked by
    calendar day rather than by session: a shop's own typed expenses. Bucketing
    ``opened_at`` by the server's UTC date would file an evening opening under
    tomorrow and take the wrong day's expenses off it.
    """
    return await db.fetch(
        f"""
        SELECT ss.id, ss.store_id, s.name AS store_name,
               ss.opened_at, ss.closed_at, ss.opened_day, ss.closed_by,
               ss.cash_at_close, ss.card_at_close, ss.salaries_at_close,
               -- Who worked it. In practice one person opens a shop and closes it,
               -- so a count of shifts said «1» on every row and answered nothing;
               -- the name is the thing an owner is actually looking for. Joined
               -- rather than picked, because two workers in one session is possible
               -- and silently showing one of them would be a lie.
               (SELECT string_agg(DISTINCT {DISPLAY_NAME}, ' · ')
                  FROM work_sessions ws
                  JOIN workers w ON w.id = ws.worker_id
                 WHERE ws.store_session_id = ss.id) AS worker_names,
               till.income, till.net_sale_cash, till.net_sale_card,
               till.salaries, till.bonuses, till.withdrawn, till.carried_in,
               goods.delivered, goods.deliveries, goods.margin,
               -- The evening's last reading of the drawer, and null when nobody
               -- looked. A second count replaces the first rather than adding to
               -- it, so the newest is the one that counts.
               (SELECT t.counted FROM till_counts t
                 WHERE t.store_session_id = ss.id
                 ORDER BY t.created_at DESC, t.id DESC LIMIT 1) AS last_count,
               -- Only this shop's own expenses, on the day it opened. One left as
               -- «Ամբողջ բիզնեսը» belongs to the business rather than to the branch
               -- that happened to be trading, and counting it here would take the
               -- same rent off every shop open that day.
               coalesce((SELECT sum(e.amount) FROM expenses e
                          WHERE e.owner_id = ss.owner_id AND e.store_id = ss.store_id
                            AND e.spent_on = (ss.opened_at AT TIME ZONE $2)::date), 0)
                   AS day_spending
          FROM store_sessions ss
          JOIN stores s ON s.id = ss.store_id
          -- What the drawer did. Sales and voids only for the takings: cash and card
          -- balances are not takings — a wage paid or money taken out has already come
          -- off them — and a void row is negative and carries the method of the sale it
          -- reverses, so a reversed receipt nets itself out without a second term.
          LEFT JOIN LATERAL (
              SELECT coalesce(sum(m.amount) FILTER (WHERE m.kind IN ('sale', 'void')), 0)
                         AS income,
                     coalesce(sum(m.amount) FILTER (
                         WHERE m.kind IN ('sale', 'void') AND m.method = 'cash'), 0)
                         AS net_sale_cash,
                     coalesce(sum(m.amount) FILTER (
                         WHERE m.kind IN ('sale', 'void') AND m.method = 'card'), 0)
                         AS net_sale_card,
                     coalesce(-sum(m.amount) FILTER (WHERE m.kind = 'salary'), 0)
                         AS salaries,
                     coalesce(-sum(m.amount) FILTER (WHERE m.kind = 'bonus'), 0)
                         AS bonuses,
                     coalesce(-sum(m.amount) FILTER (WHERE m.kind = 'withdrawal'), 0)
                         AS withdrawn,
                     -- The float the shop carried in this morning: the one deposit
                     -- nobody typed. It is what stays on the premises when a session
                     -- ends without anybody counting the drawer.
                     coalesce(sum(m.amount) FILTER (
                         WHERE m.kind = 'deposit' AND m.created_by = 'system'), 0)
                         AS carried_in
                FROM cash_movements m WHERE m.store_session_id = ss.id
          ) till ON true
          -- What was sold. Delivery is the same money through a different door, and
          -- the margin is the sale lines' own snapshot of price and cost, so
          -- repricing a vape today cannot rewrite what last week earned.
          LEFT JOIN LATERAL (
              SELECT coalesce(sum(si.line_total) FILTER (WHERE sa.is_delivery), 0)
                         AS delivered,
                     count(DISTINCT sa.id) FILTER (WHERE sa.is_delivery) AS deliveries,
                     coalesce(sum((si.unit_price - si.unit_cost) * si.quantity), 0) AS margin
                FROM sale_items si
                JOIN sales sa ON sa.id = si.sale_id
               WHERE sa.store_session_id = ss.id AND sa.voided_at IS NULL
          ) goods ON true
         WHERE ss.owner_id = $1
         ORDER BY ss.opened_at DESC
         LIMIT $3 OFFSET $4
        """,
        owner_id,
        tz,
        limit,
        offset,
    )


async def delete_store_session(conn, owner_id: int, store_session_id: int) -> bool:
    """Erase one store session and everything filed under it.

    Two deletes rather than one, because of how the keys are drawn. Sales, the
    ledger and the audit trail all cascade from the session row, but
    ``work_sessions`` does not — it is the one child that would block the delete —
    so the shifts go first, taking their own sales, ledger rows and location pings
    with them. What is left cascades.

    Write-offs are handled by the caller: their key sets null rather than
    cascading, which would strand them.
    """
    await conn.execute(
        "DELETE FROM work_sessions WHERE store_session_id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )
    result = await conn.execute(
        "DELETE FROM store_sessions WHERE id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )
    return result.endswith(" 1")


async def get_store_session(owner_id: int, store_session_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ss.id, ss.owner_id, ss.store_id, s.name AS store_name,
               ss.opened_at, ss.closed_at,
               ss.closed_by, ss.cash_at_close, ss.card_at_close, ss.salaries_at_close
          FROM store_sessions ss
          JOIN stores s ON s.id = ss.store_id
         WHERE ss.id = $1 AND ss.owner_id = $2
        """,
        store_session_id,
        owner_id,
    )


# -- work sessions ----------------------------------------------------------

async def workers_on_shift(store_id: int) -> list[asyncpg.Record]:
    """Requirement 3: who is working in this store right now."""
    return await db.fetch(
        f"""
        SELECT ws.id, ws.worker_id, {DISPLAY_NAME} AS name, ws.started_at,
               ws.start_distance_m, w.salary_amount,
               ws.last_distance_m, ws.last_ping_at, ws.ping_count,
               ws.left_area_at, ws.live_until,
               ws.live_until IS NOT NULL AND ws.live_until > now() AS live_now
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_id = $1 AND ws.ended_at IS NULL
         ORDER BY ws.started_at
        """,
        store_id,
    )


async def open_for_worker(worker_id: int) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, s.name AS store_name
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
         WHERE ws.worker_id = $1 AND ws.ended_at IS NULL
        """,
        worker_id,
    )


async def latest_for_worker(conn, worker_id: int) -> asyncpg.Record | None:
    """This worker's current shift, or the one they have just finished.

    For the things asked of them *after* their shift ends — counting the drawer is
    the one — where requiring an open shift would refuse the question at exactly the
    moment it is being answered. Open shifts first, so a worker who has already
    started another one counts against that.
    """
    return await conn.fetchrow(
        """
        SELECT id, owner_id, worker_id, store_id, store_session_id, started_at, ended_at
          FROM work_sessions
         WHERE worker_id = $1
         ORDER BY (ended_at IS NULL) DESC, started_at DESC, id DESC
         LIMIT 1
        """,
        worker_id,
    )


async def lock_open_for_worker(conn, worker_id: int) -> asyncpg.Record | None:
    """The worker's open shift, locked.

    Every sale takes this lock, which serialises one worker's sales against each
    other and blocks a concurrent shift-end — that is what stops a sale landing
    after the shift closed.
    """
    return await conn.fetchrow(
        """
        SELECT id, owner_id, worker_id, store_id, store_session_id, started_at
          FROM work_sessions
         WHERE worker_id = $1 AND ended_at IS NULL
           FOR UPDATE
        """,
        worker_id,
    )


async def open_work_session(
    conn,
    owner_id: int,
    worker_id: int,
    store_id: int,
    store_session_id: int,
    lat: float | None,
    lng: float | None,
    distance_m: int | None,
    idem_key: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO work_sessions
            (owner_id, worker_id, store_id, store_session_id,
             start_lat, start_lng, start_distance_m, start_idem_key)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        owner_id,
        worker_id,
        store_id,
        store_session_id,
        lat,
        lng,
        distance_m,
        idem_key,
    )


async def close_work_session(
    conn,
    work_session_id: int,
    salary: Decimal,
    lat: float | None,
    lng: float | None,
    idem_key: str | None,
    closed_by: str,
) -> None:
    await conn.execute(
        """
        UPDATE work_sessions
           SET ended_at = now(), salary_paid = $2, end_lat = $3, end_lng = $4,
               end_idem_key = $5, closed_by = $6
         WHERE id = $1 AND ended_at IS NULL
        """,
        work_session_id,
        salary,
        lat,
        lng,
        idem_key,
        closed_by,
    )


async def is_open(conn, store_session_id: int) -> bool:
    """Whether the shop is still trading, read on the caller's connection.

    Pooled would be wrong: the callers ask inside a transaction that has just closed
    the session or is about to, and a second connection would not see it.
    """
    return await conn.fetchval(
        "SELECT closed_at IS NULL FROM store_sessions WHERE id = $1", store_session_id
    ) or False


async def wages_for_session(store_session_id: int) -> asyncpg.Record:
    """What the shifts in one session cost, and how much of it the till could not pay.

    The *cost*, not the payment. Those stopped being the same number when the drawer
    started paying only as far as it reaches: a shift worth 5,500 out of a till holding
    1,634 costs 5,500 and pays 1,634, and a report that subtracts the payment says the
    day made 3,866 more than it did. Bonuses are in it because they are wages too.
    """
    return await db.fetchrow(
        """
        SELECT coalesce(sum(salary_paid), 0)                                    AS salary,
               coalesce(sum(bonus_paid), 0)                                     AS bonus,
               coalesce(sum(salary_paid), 0) + coalesce(sum(bonus_paid), 0)     AS cost,
               coalesce(sum(salary_unpaid), 0) + coalesce(sum(bonus_unpaid), 0) AS unpaid
          FROM work_sessions WHERE store_session_id = $1
        """,
        store_session_id,
    )


async def record_unpaid(
    conn, work_session_id: int, salary_unpaid: Decimal, bonus_unpaid: Decimal
) -> None:
    """What the drawer could not cover, so the owner still owes it.

    Written after the payment attempt rather than with the close, because how much
    the till could pay is only known once it has been asked.
    """
    await conn.execute(
        """
        UPDATE work_sessions SET salary_unpaid = $2, bonus_unpaid = $3
         WHERE id = $1
        """,
        work_session_id,
        salary_unpaid,
        bonus_unpaid,
    )


async def resync_snapshot(conn, store_session_id: int) -> None:
    """Bring a closed session's snapshot back in line with its ledger.

    An open session needs nothing: its totals are read live. A closed one has frozen
    numbers on the row, so anything that books a movement into it after the fact — every
    correction an owner can make from a report — leaves those stale and the report then
    disagrees with the ledger it was drawn from.
    """
    session = await conn.fetchrow(
        "SELECT closed_at FROM store_sessions WHERE id = $1", store_session_id
    )
    if session is None or session["closed_at"] is None:
        return

    # Imported here rather than at module scope: money's own reads live one layer
    # up, and a top-level import would tie two repos together for one query.
    from app.repo import money as money_repo

    totals = await money_repo.totals_on(conn, store_session_id)
    await conn.execute(
        """
        UPDATE store_sessions
           SET cash_at_close = $2, card_at_close = $3, salaries_at_close = $4
         WHERE id = $1
        """,
        store_session_id, totals["cash"], totals["card"], totals["salaries"],
    )


async def open_shifts_in_session(conn, store_session_id: int) -> list[asyncpg.Record]:
    """Every shift still open in a store session, locked — used when closing.

    ``started_at`` comes along because the wage depends on it: a shift under eight
    hours is paid half, and this is one of the paths that pays one.
    """
    return await conn.fetch(
        """
        SELECT ws.id, ws.worker_id, ws.store_id, ws.owner_id, ws.store_session_id,
               ws.started_at, w.salary_amount, w.salary_period
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_session_id = $1 AND ws.ended_at IS NULL
         ORDER BY ws.id
           FOR UPDATE OF ws
        """,
        store_session_id,
    )


async def by_start_idem(owner_id: int, idem_key: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, ws.start_distance_m,
               s.name AS store_name
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
         WHERE ws.owner_id = $1 AND ws.start_idem_key = $2
        """,
        owner_id,
        idem_key,
    )


async def by_end_idem(owner_id: int, idem_key: str) -> asyncpg.Record | None:
    """A finished shift by its own idempotency key — including whether closing it
    also closed the shop.

    ``store_closed`` is read fresh off ``store_sessions`` rather than trusted from
    a caller, specifically so a replay reports it correctly. A replay's whole point
    is to answer a retried request exactly as the original did, and there is
    nothing else here that can drift between "what actually happened" and "what
    this reply claims happened" for a caller deciding, from that field, whether to
    tell a worker the shop is still open.
    """
    return await db.fetchrow(
        """
        SELECT ws.id, ws.store_id, ws.store_session_id, ws.started_at, ws.ended_at,
               ws.salary_paid, ws.salary_unpaid, ws.bonus_paid, ws.bonus_unpaid,
               s.name AS store_name, ss.closed_at IS NOT NULL AS store_closed
          FROM work_sessions ws
          JOIN stores s ON s.id = ws.store_id
          JOIN store_sessions ss ON ss.id = ws.store_session_id
         WHERE ws.owner_id = $1 AND ws.end_idem_key = $2
        """,
        owner_id,
        idem_key,
    )


async def shifts_in_session(store_session_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        f"""
        SELECT ws.id, ws.worker_id, {DISPLAY_NAME} AS worker_name,
               ws.started_at, ws.ended_at,
               ws.salary_paid, ws.salary_unpaid, ws.bonus_paid, ws.bonus_unpaid,
               ws.start_distance_m, ws.closed_by
          FROM work_sessions ws
          JOIN workers w ON w.id = ws.worker_id
         WHERE ws.store_session_id = $1
         ORDER BY ws.started_at
        """,
        store_session_id,
    )
