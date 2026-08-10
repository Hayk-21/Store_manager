"""Every way money leaves the business, in one list.

It leaves by four doors and each had its own page, so the only view of the whole was
two aggregate rows: «Աշխատավարձ · Փակված հերթափոխեր · 18,000» and «Խոտան · Դուրս գրված
ապրանք · 10,976». Both true, neither answerable — which worker, which product, who took
what out of the till and what for.

The four doors:

* **wages** — what a finished shift paid, per shift and per person. Bonuses ride with
  them: a bonus is money out for work done, and its own row so the wage stays the wage.
* **withdrawals** — cash taken out of a drawer. The one a cashier can trigger, and the
  one whose reason matters most.
* **expenses** — what the owner enters on `/expenses`: rent, advertising, an influencer.
* **breakage** — stock written off, at what it cost. It never touched the till, which is
  exactly why it goes missing from a cash view and has to be here deliberately.

Each row carries the ids the page needs to edit or delete it in its own home. A union
rather than four queries because the answer is one list sorted by time — «what did this
month cost» is not four questions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import asyncpg

from app.config import settings
from app.db import db
from app.repo.workers import DISPLAY_NAME

# What each kind can have done to it, so a template does not have to know.
# 'amount' — the figure itself is editable in place.
# 'delete' — the row can be removed outright.
EDITABLE = {"salary", "withdrawal", "expense"}
DELETABLE = {"withdrawal", "expense", "breakage"}


async def list_between(
    owner_id: int,
    since: date,
    until: date,
    store_id: int | None = None,
    limit: int = 500,
) -> list[asyncpg.Record]:
    """Every outgoing row of a period, newest first.

    ``store_id`` narrows wages, withdrawals and breakage, which all belong to a shop.
    Expenses are kept whatever the filter: rent and advertising belong to the business,
    and dropping them when a shop is chosen would quietly shrink the total.
    """
    return await db.fetch(
        f"""
        WITH wages AS (
            SELECT 'salary'::text        AS kind,
                   ws.id                 AS id,
                   ws.ended_at           AS at,
                   'Աշխատավարձ'::text    AS purpose,
                   s.name                AS store_name,
                   {DISPLAY_NAME}        AS worker_name,
                   'cash'::text          AS method,
                   ws.salary_paid        AS amount,
                   ws.salary_unpaid      AS unpaid,
                   ws.store_session_id   AS store_session_id
              FROM work_sessions ws
              JOIN stores s  ON s.id = ws.store_id
              LEFT JOIN workers w ON w.id = ws.worker_id
             WHERE ws.owner_id = $1 AND ws.ended_at IS NOT NULL
               AND ws.salary_paid > 0
               AND (ws.ended_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
               AND ($5::bigint IS NULL OR ws.store_id = $5)
        ),
        bonuses AS (
            SELECT 'bonus', ws.id, ws.ended_at, 'Բոնուս', s.name, {DISPLAY_NAME},
                   'cash', ws.bonus_paid, ws.bonus_unpaid, ws.store_session_id
              FROM work_sessions ws
              JOIN stores s  ON s.id = ws.store_id
              LEFT JOIN workers w ON w.id = ws.worker_id
             WHERE ws.owner_id = $1 AND ws.ended_at IS NOT NULL
               AND coalesce(ws.bonus_paid, 0) > 0
               AND (ws.ended_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
               AND ($5::bigint IS NULL OR ws.store_id = $5)
        ),
        taken AS (
            SELECT 'withdrawal', m.id, m.created_at,
                   coalesce(m.note, 'Առանց նշման'), s.name,
                   CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END,
                   m.method, -m.amount, 0::numeric, m.store_session_id
              FROM cash_movements m
              JOIN stores s ON s.id = m.store_id
              LEFT JOIN workers w ON w.id = m.worker_id
             WHERE m.owner_id = $1 AND m.kind = 'withdrawal'
               AND (m.created_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
               AND ($5::bigint IS NULL OR m.store_id = $5)
        ),
        entered AS (
            SELECT 'expense', e.id, e.spent_on::timestamptz, e.purpose,
                   coalesce(s.name, 'Ամբողջ բիզնեսը'), c.name,
                   e.method, e.amount, 0::numeric, NULL::bigint
              FROM expenses e
              LEFT JOIN stores s ON s.id = e.store_id
              LEFT JOIN expense_categories c ON c.id = e.category_id
             WHERE e.owner_id = $1 AND e.spent_on BETWEEN $2 AND $3
        ),
        broken AS (
            SELECT 'breakage', wo.id, wo.created_at,
                   i.name || ' ×' || wo.quantity ||
                       coalesce(' — ' || wo.reason, ''),
                   s.name,
                   CASE WHEN w.id IS NULL THEN NULL ELSE {DISPLAY_NAME} END,
                   NULL, wo.total_cost, 0::numeric, wo.store_session_id
              FROM write_offs wo
              JOIN items i  ON i.id = wo.item_id
              JOIN stores s ON s.id = wo.store_id
              LEFT JOIN workers w ON w.id = wo.worker_id
             WHERE wo.owner_id = $1
               AND (wo.created_at AT TIME ZONE $4)::date BETWEEN $2 AND $3
               AND ($5::bigint IS NULL OR wo.store_id = $5)
        )
        SELECT * FROM wages
        UNION ALL SELECT * FROM bonuses
        UNION ALL SELECT * FROM taken
        UNION ALL SELECT * FROM entered
        UNION ALL SELECT * FROM broken
         ORDER BY at DESC, kind, id DESC
         LIMIT $6
        """,
        owner_id, since, until, settings.tzname, store_id, limit,
    )


def totals(rows: list[asyncpg.Record]) -> dict[str, Decimal]:
    """One figure per door, plus the whole. Summed here rather than in SQL because the
    rows are already in hand and a second query could see a different moment."""
    out: dict[str, Decimal] = {"total": Decimal(0)}
    for row in rows:
        amount = Decimal(row["amount"])
        out[row["kind"]] = out.get(row["kind"], Decimal(0)) + amount
        out["total"] += amount
    return out
