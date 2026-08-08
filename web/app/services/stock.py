"""Correcting what the shelf says, from the counter.

Not a sale and not breakage. The count on the screen drifts from the count on the
shelf — a delivery nobody entered, a customer's return, two of something
miscounted at the last close — and the cashier is the only person standing in
front of both numbers.

Every correction is logged with the worker's name against it. A count that can be
changed silently can be changed to cover a shortfall, so the owner sees each one
on the report beside the sales and the write-offs.
"""

from __future__ import annotations

import logging

from app.db import db
from app.errors import BotError
from app.repo import adjustments as adjustments_repo
from app.repo import sessions as sessions_repo

log = logging.getLogger("storemanager.stock")

# One tap of "confirm" may carry corrections to this many products. Well above a
# real counter session and low enough that a malformed request cannot ask the
# database to lock every item in the shop.
MAX_LINES = 100


async def adjust_by_worker(
    worker, lines: list[dict], idem_key: str, note: str | None = None
) -> dict:
    """Apply a batch of ``{item_id, delta}`` corrections to the worker's store.

    All or nothing. A cashier who counted four products and confirmed once must
    not end up with two of them corrected and two not, because the half that
    failed is invisible — the screen would simply show numbers they think they
    already fixed.
    """
    if not lines:
        raise BotError("validation_error", "Ոչ մի փոփոխություն նշված չէ։")
    if len(lines) > MAX_LINES:
        raise BotError("validation_error", "Չափազանց շատ ապրանք է նշված։")

    replay = await adjustments_repo.by_external_id(worker.owner_id, idem_key)
    if replay:
        return _payload(replay, duplicate=True)

    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        # Asked again, now that this worker's writes are serialised behind the
        # shift lock. The check above is the cheap path; this one is the guard,
        # because one batch is several rows under one key and no unique index can
        # express "these rows together were already written".
        replay = await conn.fetch(
            """
            SELECT sa.id, sa.item_id, sa.delta, sa.count_after, i.name
              FROM stock_adjustments sa
              JOIN items i ON i.id = sa.item_id
             WHERE sa.owner_id = $1 AND sa.external_id = $2
             ORDER BY sa.id
            """,
            worker.owner_id, idem_key,
        )
        if replay:
            return _payload(replay, duplicate=True)

        written = []
        # Ascending item order, like every other stock path here, so two
        # corrections touching overlapping lists queue rather than deadlock.
        for line in sorted(lines, key=lambda entry: entry["item_id"]):
            item_id, delta = line["item_id"], int(line["delta"])
            if delta == 0:
                continue

            # One conditional statement rather than a read then a write: the
            # guard is in the WHERE clause, so nothing can slip between them.
            row = await conn.fetchrow(
                """
                UPDATE items SET count = count + $4, updated_at = now()
                 WHERE id = $1 AND owner_id = $2 AND store_id = $3
                   AND is_active AND count + $4 >= 0
                RETURNING name, count
                """,
                item_id, worker.owner_id, shift["store_id"], delta,
            )
            if row is None:
                await _explain_refusal(conn, worker.owner_id, item_id, delta)

            row_id = await adjustments_repo.insert(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                item_id=item_id,
                worker_id=worker.id,
                work_session_id=shift["id"],
                store_session_id=shift["store_session_id"],
                delta=delta,
                count_after=row["count"],
                note=note,
                external_id=idem_key,
            )
            written.append(
                {"id": row_id, "item_id": item_id, "name": row["name"],
                 "delta": delta, "count_after": row["count"]}
            )

        if not written:
            raise BotError("validation_error", "Ոչ մի փոփոխություն նշված չէ։")

    log.info(
        "worker %s corrected %d product(s) in store %s",
        worker.id, len(written), shift["store_id"],
    )
    return {"ok": True, "duplicate": False, "adjusted": written}


async def _explain_refusal(conn, owner_id: int, item_id: int, delta: int) -> None:
    """Turn "the UPDATE matched nothing" into the reason it matched nothing.

    Always raises. Two reasons only: the product is not this shop's, or taking
    that many off would put the count below zero.
    """
    item = await conn.fetchrow(
        "SELECT name, count FROM items WHERE id = $1 AND owner_id = $2",
        item_id, owner_id,
    )
    if item is None:
        raise BotError("unknown_item")
    raise BotError(
        "validation_error",
        f"«{item['name']}» — պահեստում կա {item['count']} հատ, "
        f"ավելին հանել հնարավոր չէ։",
    )


def _payload(rows, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "adjusted": [
            {"id": row["id"], "item_id": row["item_id"], "name": row["name"],
             "delta": row["delta"], "count_after": row["count_after"]}
            for row in rows
        ],
    }
