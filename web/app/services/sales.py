"""Recording and voiding a sale.

This is the only place stock and money move together, so it is the only place a
wrong number can come from. Everything here happens in one transaction: a basket
applies whole or not at all.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.db import db
from app.errors import BotError
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.services.shifts import Worker
from app.texts import insufficient_stock_message

log = logging.getLogger("vapestore.sales")

MAX_LINES = 50
CENT = Decimal("0.01")


def _merge(lines: list[dict]) -> list[dict]:
    """Collapse repeated item ids and sort ascending.

    Merging keeps the ``UNIQUE (sale_id, item_id)`` constraint satisfied; the
    ascending order means two overlapping baskets always lock rows in the same
    sequence, so they queue instead of deadlocking.
    """
    merged: dict[int, dict] = {}
    for line in lines:
        existing = merged.get(line["item_id"])
        if existing is None:
            merged[line["item_id"]] = dict(line)
        else:
            existing["quantity"] += line["quantity"]
            # A second price for the same item in one basket is ambiguous; the
            # first one wins rather than silently averaging.
            if existing.get("unit_price") is None:
                existing["unit_price"] = line.get("unit_price")
    return [merged[key] for key in sorted(merged)]


async def record_sale(
    worker: Worker, lines: list[dict], payment_method: str, external_id: str
) -> dict:
    """Requirement 6: decrement stock and credit the till, atomically."""
    if not lines:
        raise BotError("empty_basket")
    if len(lines) > MAX_LINES:
        raise BotError("validation_error", f"Մեկ վաճառքում առավելագույնը {MAX_LINES} տող։")

    existing = await sales_repo.by_external_id(worker.id, external_id)
    if existing is not None:
        return await _sale_payload(existing["id"], duplicate=True)

    basket = _merge(lines)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")
            # The store comes from the open shift, never from the request: a bot
            # cannot sell into a store its worker is not standing in.
            store_id = shift["store_id"]

            applied, total = await _apply_lines(conn, worker, store_id, basket)

            sale_id = await sales_repo.insert_sale(
                conn,
                owner_id=worker.owner_id,
                store_id=store_id,
                worker_id=worker.id,
                work_session_id=shift["id"],
                store_session_id=shift["store_session_id"],
                payment_method=payment_method,
                total=total,
                external_id=external_id,
            )
            await sales_repo.insert_lines(conn, worker.owner_id, sale_id, applied)
            await money_repo.insert_movement(
                conn,
                owner_id=worker.owner_id,
                store_id=store_id,
                store_session_id=shift["store_session_id"],
                method=payment_method,
                kind="sale",
                amount=total,
                sale_id=sale_id,
                work_session_id=shift["id"],
                worker_id=worker.id,
            )
    except asyncpg.exceptions.UniqueViolationError:
        # Two retries of one tap arrived together and the other won. The unique
        # index is the arbiter; the pre-check above is only an optimisation.
        original = await sales_repo.by_external_id(worker.id, external_id)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return await _sale_payload(original["id"], duplicate=True)

    log.info("worker %s sold %s (%s lines, %s)", worker.id, total, len(applied), payment_method)
    return await _sale_payload(sale_id, duplicate=False)


async def _apply_lines(
    conn, worker: Worker, store_id: int, basket: list[dict]
) -> tuple[list[dict], Decimal]:
    """Take the stock, one item at a time, and price each line."""
    applied: list[dict] = []
    total = Decimal("0.00")

    for line in basket:
        item_id, quantity = line["item_id"], line["quantity"]
        # The lock and the stock check are a single statement: there is no window
        # between reading the count and writing it, and the check cannot drift
        # out of step with the update.
        row = await conn.fetchrow(
            """
            UPDATE items
               SET count = count - $4, updated_at = now()
             WHERE id = $1 AND owner_id = $2 AND store_id = $3
               AND is_active AND count >= $4
            RETURNING name, sell_price, self_price, count AS remaining
            """,
            item_id,
            worker.owner_id,
            store_id,
            quantity,
        )
        if row is None:
            # Nothing has been committed, so raising here undoes the whole basket.
            await _explain_failure(conn, worker, store_id, item_id, quantity)

        unit_price = (
            Decimal(line["unit_price"]) if line.get("unit_price") is not None
            else Decimal(row["sell_price"])
        )
        line_total = (unit_price * quantity).quantize(CENT)
        total += line_total
        applied.append(
            {
                "item_id": item_id,
                "name": row["name"],
                "quantity": quantity,
                "unit_price": unit_price,
                "unit_cost": Decimal(row["self_price"]),
                "line_total": line_total,
                "remaining": row["remaining"],
            }
        )

    return applied, total


async def _explain_failure(conn, worker: Worker, store_id: int, item_id: int, quantity: int):
    """Turn "the UPDATE matched nothing" into the reason it matched nothing."""
    item = await conn.fetchrow(
        "SELECT name, count, store_id, is_active FROM items WHERE id = $1 AND owner_id = $2",
        item_id,
        worker.owner_id,
    )
    if item is None or not item["is_active"] or item["store_id"] != store_id:
        raise BotError("unknown_item", details={"item_id": item_id})
    raise BotError(
        "insufficient_stock",
        insufficient_stock_message(item["name"], quantity, item["count"]),
        details={
            "item_id": item_id,
            "name": item["name"],
            "requested": quantity,
            "available": item["count"],
        },
    )


async def void_last_sale(worker: Worker, reason: str | None = None) -> dict:
    """Let a worker undo their own most recent receipt in this shift.

    Never deletes anything: the receipt keeps its row with ``voided_at`` set and
    a reversing ledger entry beside it, so the owner sees that it happened and
    who did it.
    """
    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        sale = await sales_repo.lock_last_voidable(conn, worker.id, shift["id"])
        if sale is None:
            raise BotError("nothing_to_void")

        lines = await sales_repo.lines_for_void(conn, sale["id"])
        for line in lines:  # already ordered by item_id
            await conn.execute(
                "UPDATE items SET count = count + $2, updated_at = now() WHERE id = $1",
                line["item_id"],
                line["quantity"],
            )

        await sales_repo.mark_voided(conn, sale["id"], worker.id, reason)
        await money_repo.insert_movement(
            conn,
            owner_id=worker.owner_id,
            store_id=sale["store_id"],
            store_session_id=sale["store_session_id"],
            method=sale["payment_method"],
            kind="void",
            amount=-Decimal(sale["total"]),
            sale_id=sale["id"],
            work_session_id=shift["id"],
            worker_id=worker.id,
            note=reason,
        )
        totals = await money_repo.totals_on(conn, sale["store_session_id"])

    log.info("worker %s voided sale %s (%s)", worker.id, sale["id"], sale["total"])
    return {
        "ok": True,
        "voided": {
            "sale_id": sale["id"],
            "total": f"{Decimal(sale['total']):.2f}",
            "payment_method": sale["payment_method"],
            "sold_at": sale["sold_at"].isoformat(),
            "restored": [
                {"item_id": line["item_id"], "quantity": line["quantity"]} for line in lines
            ],
        },
        "store_totals": {"cash": f"{totals['cash']:.2f}", "card": f"{totals['card']:.2f}"},
    }


async def _sale_payload(sale_id: int, *, duplicate: bool) -> dict:
    """Rebuild the response from the database, so a replay is byte-for-byte the
    same answer the original call gave."""
    sale = await db.fetchrow(
        """
        SELECT sa.id, sa.store_id, sa.work_session_id, sa.store_session_id,
               sa.payment_method, sa.total, sa.sold_at, sa.voided_at
          FROM sales sa WHERE sa.id = $1
        """,
        sale_id,
    )
    lines = await sales_repo.lines_for_sale(sale_id)
    totals = await money_repo.totals_for_session(sale["store_session_id"])
    return {
        "ok": True,
        "duplicate": duplicate,
        "sale": {
            "id": sale["id"],
            "store_id": sale["store_id"],
            "session_id": sale["work_session_id"],
            "payment_method": sale["payment_method"],
            "total": f"{Decimal(sale['total']):.2f}",
            "sold_at": sale["sold_at"].isoformat(),
            "voided": sale["voided_at"] is not None,
            "lines": [
                {
                    "item_id": line["item_id"],
                    "name": line["name"],
                    "quantity": line["quantity"],
                    "unit_price": f"{Decimal(line['unit_price']):.2f}",
                    "line_total": f"{Decimal(line['line_total']):.2f}",
                    "remaining_count": line["remaining_count"],
                }
                for line in lines
            ],
        },
        "store_totals": {"cash": f"{totals['cash']:.2f}", "card": f"{totals['card']:.2f}"},
    }
