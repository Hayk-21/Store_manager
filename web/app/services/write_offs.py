"""Stock that left without being sold.

A broken vape is not a sale at a price of zero. Recording it as one would put it
in the revenue figures, the receipt count and the worker's bonus progress — and
that last one would pay somebody for breaking things.

Nothing moves in the till either: the money left when the stock was bought. What
this costs is the price we paid, snapshotted at the moment of the write-off so a
later change to the item's cost cannot rewrite what the loss was worth.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import BotError
from app.repo import sessions as sessions_repo
from app.repo import write_offs as write_offs_repo
from app.services.shifts import Worker
from app.texts import insufficient_stock_message

log = logging.getLogger("storemanager.write_offs")

CENT = Decimal("0.01")
MAX_QUANTITY = 10_000


async def record(
    worker: Worker,
    item_id: int,
    quantity: int,
    reason: str | None = None,
    external_id: str | None = None,
) -> dict:
    """Take damaged stock off the shelf, from a worker's open shift."""
    if not 0 < quantity <= MAX_QUANTITY:
        raise BotError("validation_error", f"Քանակը պետք է լինի 1-ից {MAX_QUANTITY}։")

    if external_id is not None:
        replay = await write_offs_repo.by_external_id(worker.owner_id, external_id)
        if replay is not None:
            return _payload(replay, duplicate=True)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")

            # The same single statement the sale path uses: the lock and the
            # stock check are one thing, so there is no window between reading
            # the count and writing it.
            row = await conn.fetchrow(
                """
                UPDATE items
                   SET count = count - $4, updated_at = now()
                 WHERE id = $1 AND owner_id = $2 AND store_id = $3
                   AND is_active AND count >= $4
                RETURNING name, self_price, count AS remaining
                """,
                item_id, worker.owner_id, shift["store_id"], quantity,
            )
            if row is None:
                await _explain(conn, worker, shift["store_id"], item_id, quantity)

            unit_cost = Decimal(row["self_price"])
            write_off_id = await write_offs_repo.insert(
                conn,
                owner_id=worker.owner_id,
                store_id=shift["store_id"],
                item_id=item_id,
                worker_id=worker.id,
                work_session_id=shift["id"],
                store_session_id=shift["store_session_id"],
                quantity=quantity,
                unit_cost=unit_cost,
                total_cost=(unit_cost * quantity).quantize(CENT),
                reason=reason,
                external_id=external_id,
            )
    except asyncpg.exceptions.UniqueViolationError:
        # Two retries of one tap arrived together; the index is the arbiter.
        original = await write_offs_repo.by_external_id(worker.owner_id, external_id)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s wrote off %s × item %s (%s)", worker.id, quantity, item_id, reason or "—"
    )
    written = await write_offs_repo.get(worker.owner_id, write_off_id)
    return _payload(written, duplicate=False)


async def _explain(conn, worker: Worker, store_id: int, item_id: int, quantity: int):
    """Turn "the UPDATE matched nothing" into the reason it matched nothing."""
    item = await conn.fetchrow(
        "SELECT name, count, store_id, is_active FROM items WHERE id = $1 AND owner_id = $2",
        item_id, worker.owner_id,
    )
    if item is None or not item["is_active"] or item["store_id"] != store_id:
        raise BotError("unknown_item", details={"item_id": item_id})
    raise BotError(
        "insufficient_stock",
        insufficient_stock_message(item["name"], quantity, item["count"]),
        details={"name": item["name"], "requested": quantity, "available": item["count"]},
    )


def _payload(row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "write_off": {
            "id": row["id"],
            "item_id": row["item_id"],
            "name": row["name"],
            "quantity": row["quantity"],
            "total_cost": f"{Decimal(row['total_cost']):.2f}",
            "remaining_count": row["remaining_count"],
            "reason": row["reason"],
        },
    }
