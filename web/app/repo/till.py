"""Hand counts of the cash drawer.

The balance itself lives on ``stores.till_balance``. These rows are the record of how
it got to that figure: who said so, when, what the till held at the time, and what
went to the owner as a result.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from app.db import db
from app.repo.workers import DISPLAY_NAME


async def insert(
    conn,
    *,
    owner_id: int,
    store_id: int,
    store_session_id: int | None,
    work_session_id: int | None,
    worker_id: int | None,
    kind: str,
    counted: Decimal,
    expected: Decimal,
    handed_over: Decimal | None = None,
    note: str | None = None,
    external_id: str | None = None,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO till_counts
            (owner_id, store_id, store_session_id, work_session_id, worker_id,
             kind, counted, expected, handed_over, note, external_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
        """,
        owner_id, store_id, store_session_id, work_session_id, worker_id,
        kind, counted, expected, handed_over, note, external_id,
    )


async def by_external_id(owner_id: int, external_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        SELECT id, kind, counted, expected, handed_over FROM till_counts
         WHERE owner_id = $1 AND external_id = $2
        """,
        owner_id,
        external_id,
    )


async def lock(conn, owner_id: int, count_id: int) -> asyncpg.Record | None:
    """One count, held for the length of a correction.

    Every column, because the caller may be about to delete it and the undo has to
    rebuild a row that will not exist anywhere else.
    """
    return await conn.fetchrow(
        """
        SELECT id, store_id, store_session_id, work_session_id, worker_id, kind,
               counted, expected, handed_over, note, external_id, created_at
          FROM till_counts WHERE id = $1 AND owner_id = $2 FOR UPDATE
        """,
        count_id,
        owner_id,
    )


async def restore(conn, owner_id: int, row: dict) -> None:
    """Put a deleted count back, exactly as it was.

    ``created_at`` and ``external_id`` come back with it: the first so the row lands in
    its own place in the history rather than at the end of it, the second so the bot's
    idempotency key still points at something.
    """
    await conn.execute(
        """
        INSERT INTO till_counts
            (owner_id, store_id, store_session_id, work_session_id, worker_id,
             kind, counted, expected, handed_over, note, external_id, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        owner_id,
        row["store_id"],
        row["store_session_id"],
        row["work_session_id"],
        row["worker_id"],
        row["kind"],
        Decimal(row["counted"]),
        Decimal(row["expected"]),
        Decimal(row["handed_over"]) if row["handed_over"] is not None else None,
        row["note"],
        row["external_id"],
        row["created_at"],
    )


async def set_reading(
    conn,
    count_id: int,
    counted: Decimal,
    expected: Decimal,
    handed_over: Decimal | None,
) -> None:
    """Both halves of a count and the share that follows from them, in one write.

    Together, because the share is the difference: setting one without the other would
    leave a row whose three figures do not describe the same evening.
    """
    await conn.execute(
        """
        UPDATE till_counts SET counted = $2, expected = $3, handed_over = $4
         WHERE id = $1
        """,
        count_id,
        counted,
        expected,
        handed_over,
    )


async def latest_for_store(conn, owner_id: int, store_id: int) -> asyncpg.Record | None:
    """The count the shop's balance currently comes from.

    Read on the caller's connection: one caller has just deleted a row and needs the
    newest of what is left, and a pooled read would still see the deleted one.
    """
    return await conn.fetchrow(
        """
        SELECT id, counted FROM till_counts
         WHERE owner_id = $1 AND store_id = $2
         ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        owner_id,
        store_id,
    )


async def latest_for_session(store_session_id: int) -> asyncpg.Record | None:
    """The evening's last word on the drawer, or nothing when nobody looked.

    The same row the report header reads, asked for on its own so a correction can
    find out whether it is amending a reading or making the first one.
    """
    return await db.fetchrow(
        """
        SELECT id, counted, expected FROM till_counts
         WHERE store_session_id = $1
         ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        store_session_id,
    )


async def delete(conn, owner_id: int, count_id: int) -> None:
    await conn.execute(
        "DELETE FROM till_counts WHERE id = $1 AND owner_id = $2", count_id, owner_id
    )


async def last_count_for_store(owner_id: int, store_id: int) -> asyncpg.Record | None:
    """The most recent count of this shop's drawer, for a page to render.

    Not the balance — that is a column on the store now. This is who last set it and
    when. An owner's correction counts as a count here, because "the owner set it" is
    exactly what somebody reading the page needs to be told.
    """
    return await db.fetchrow(
        f"""
        SELECT t.counted, t.expected, t.handed_over, t.kind, t.created_at, t.note,
               {DISPLAY_NAME} AS worker_name
          FROM till_counts t
          LEFT JOIN workers w ON w.id = t.worker_id
         WHERE t.owner_id = $1 AND t.store_id = $2
         ORDER BY t.created_at DESC, t.id DESC
         LIMIT 1
        """,
        owner_id,
        store_id,
    )


async def for_session(store_session_id: int) -> list[asyncpg.Record]:
    """Every handover recorded during one session, for the report."""
    return await db.fetch(
        f"""
        SELECT t.id, t.kind, t.counted, t.expected, t.handed_over, t.created_at,
               {DISPLAY_NAME} AS worker_name
          FROM till_counts t
          LEFT JOIN workers w ON w.id = t.worker_id
         WHERE t.store_session_id = $1
         ORDER BY t.created_at, t.id
        """,
        store_session_id,
    )


async def delete_for_session(conn, owner_id: int, store_session_id: int) -> None:
    """Deleting a report takes its counts with it — the key sets null rather than
    cascading, which would strand them."""
    await conn.execute(
        "DELETE FROM till_counts WHERE store_session_id = $1 AND owner_id = $2",
        store_session_id,
        owner_id,
    )
