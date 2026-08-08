"""Moving stock between two of one owner's shops.

One shop runs out of something a sister shop has a box of. Recording that as a
write-off at one end and a correction at the other tells the reports two lies —
breakage that never broke, a miscount that was not a miscount — so it is its own
thing.

Two ways in, and the difference is who can be trusted with the other shelf:

* A cashier asks. They cannot reach into a shelf they are not standing at, so the
  request waits until a worker at the source shop agrees the box exists and has
  left. Nothing moves before that.
* The owner moves it. They can see both shelves and answer to nobody, so it
  applies at once.

Both end in the same function, because "the stock actually moves" is one rule and
two copies of it would eventually disagree.

Nothing here touches money. The same box on a different shelf is not income and
not an expense, which is also why the cost price travels with it: revaluing stock
by moving it would quietly change what the business is worth.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from html import escape

import asyncpg

from app import texts
from app.db import db
from app.errors import AppError, BotError
from app.repo import items as items_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import transfers as transfers_repo
from app.repo import workers as workers_repo
from app.services import telegram

log = logging.getLogger("storemanager.transfers")

MAX_QUANTITY = 100_000


async def _move_stock(conn, owner_id: int, transfer) -> dict:
    """Take the goods off one shelf and put them on the other.

    Ascending store order for the two updates, the same discipline the rest of the
    stock paths use, so two transfers crossing in opposite directions between the
    same pair of shops queue instead of deadlocking.

    The receiving shop may already stock the product under its own row, or may
    never have seen it. Either way the quantity is *added* — a transfer tops up a
    shelf, it does not replace what is on it.
    """
    source = await conn.fetchrow(
        """
        UPDATE items SET count = count - $3, updated_at = now()
         WHERE id = $1 AND owner_id = $2 AND count >= $3 AND store_id = $4
        RETURNING name, self_price, sell_price, wholesale_price, store_id
        """,
        transfer["item_id"], owner_id, transfer["quantity"], transfer["from_store_id"],
    )
    if source is None:
        item = await conn.fetchrow(
            "SELECT name, count FROM items WHERE id = $1 AND owner_id = $2",
            transfer["item_id"], owner_id,
        )
        if item is None:
            raise BotError("unknown_item")
        raise BotError(
            "insufficient_stock",
            f"«{item['name']}» — {item['count']} հատ է մնացել, "
            f"{transfer['quantity']} հատ ուղարկել հնարավոր չէ։",
        )

    # The destination's own row for this product, matched by name because that is
    # what the shops have in common — item ids are per shop.
    destination_id = await conn.fetchval(
        """
        INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price,
                           wholesale_price)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (store_id, lower(btrim(name))) DO UPDATE
           SET count = items.count + EXCLUDED.count,
               is_active = true,
               updated_at = now()
        RETURNING id
        """,
        owner_id,
        transfer["to_store_id"],
        source["name"],
        transfer["quantity"],
        source["self_price"],
        source["sell_price"],
        source["wholesale_price"],
    )
    return {
        "name": source["name"],
        "unit_cost": Decimal(source["self_price"]),
        "destination_item_id": destination_id,
    }


# -- the cashier's side ------------------------------------------------------

async def request_by_worker(
    worker, from_store_id: int, item_id: int, quantity: int, idem_key: str
) -> dict:
    """Ask another shop for stock. Moves nothing yet.

    The item named is the *source* shop's row — the cashier is pointing at a
    product on somebody else's shelf, which is why they had to be shown that
    shelf's list to choose from.
    """
    if not (0 < quantity <= MAX_QUANTITY):
        raise BotError("validation_error", "Սխալ քանակ։")

    replay = await transfers_repo.by_external_id(worker.owner_id, idem_key)
    if replay is not None:
        return _payload(replay, duplicate=True)

    try:
        async with db.transaction() as conn:
            shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
            if shift is None:
                raise BotError("no_open_session")
            if from_store_id == shift["store_id"]:
                raise BotError(
                    "validation_error",
                    "Այս ապրանքն արդեն ձեր խանութում է։",
                )

            item = await conn.fetchrow(
                """
                SELECT id, name, count, self_price, store_id
                  FROM items
                 WHERE id = $1 AND owner_id = $2 AND store_id = $3 AND is_active
                """,
                item_id, worker.owner_id, from_store_id,
            )
            if item is None:
                raise BotError("unknown_item")
            if item["count"] < quantity:
                # Checked now as a courtesy and again at approval as the rule. The
                # box can be sold in between, and the shelf at approval time is the
                # only one that counts.
                raise BotError(
                    "insufficient_stock",
                    f"«{item['name']}» — այդ խանութում կա {item['count']} հատ։",
                )

            transfer_id = await transfers_repo.insert(
                conn,
                owner_id=worker.owner_id,
                item_id=item_id,
                from_store_id=from_store_id,
                to_store_id=shift["store_id"],
                quantity=quantity,
                unit_cost=Decimal(item["self_price"]),
                requested_by_worker_id=worker.id,
                external_id=idem_key,
            )
    except asyncpg.exceptions.UniqueViolationError:
        original = await transfers_repo.by_external_id(worker.owner_id, idem_key)
        if original is None:  # pragma: no cover - some other constraint
            raise
        return _payload(original, duplicate=True)

    log.info(
        "worker %s asked store %s for %s of item %s",
        worker.id, from_store_id, quantity, item_id,
    )
    row = await transfers_repo.get(worker.owner_id, transfer_id)
    await _tell_the_source_shop(row)
    return _payload(row, duplicate=False)


async def _tell_the_source_shop(transfer) -> None:
    """Nudge whoever is on shift at the shop being asked.

    A request nobody knows about is a shop waiting for a box that is not coming.
    They can also find it under «Փոխանցումներ», so this is a prompt and not the
    only route — which is why a failure to deliver is logged and swallowed rather
    than failing the request that has already been written.
    """
    for chat_id in await workers_repo.telegram_ids_on_shift(
        transfer["from_store_id"]
    ):
        try:
            await telegram.send_message(
                chat_id,
                texts.TRANSFER_REQUESTED.format(
                    quantity=transfer["quantity"],
                    item=escape(transfer["item_name"]),
                    store=escape(transfer["to_store_name"]),
                ),
            )
        except telegram.Undeliverable as exc:
            log.info(
                "could not notify chat %s about transfer %s: %s",
                chat_id, transfer["id"], exc.reason,
            )


async def decide_by_worker(worker, transfer_id: int, approve: bool) -> dict:
    """Answer a request, as somebody standing in the shop being asked.

    Approving and moving the stock are one step. An approval that did not move it
    would be a promise, and the shelf would disagree with the screen until somebody
    noticed.
    """
    async with db.transaction() as conn:
        shift = await sessions_repo.lock_open_for_worker(conn, worker.id)
        if shift is None:
            raise BotError("no_open_session")

        transfer = await transfers_repo.lock_pending(conn, worker.owner_id, transfer_id)
        if transfer is None:
            # Either it never existed, or a colleague answered it first. The same
            # sentence covers both, because from here they are the same situation.
            raise BotError(
                "validation_error", "Այս հարցումն արդեն պատասխանված է։"
            )
        if transfer["from_store_id"] != shift["store_id"]:
            # Only the shop being asked may answer. Reads as missing rather than
            # forbidden, like every other cross-shop lookup.
            raise BotError("unknown_item")

        if approve:
            await transfers_repo.decide(conn, transfer_id, "approved", worker_id=worker.id)
            await _move_stock(conn, worker.owner_id, transfer)
        else:
            await transfers_repo.decide(conn, transfer_id, "rejected", worker_id=worker.id)

    # Read *after* the transaction, never inside it. ``transfers_repo.get`` goes to
    # the pool, so a call from in here would run on a different connection and see
    # the row as it was before the decision — reporting a rejected request as still
    # pending. The tests cannot catch that: they bind ``db`` to the test's own
    # connection, which does see its uncommitted work.
    log.info(
        "worker %s %s transfer %s",
        worker.id, "approved" if approve else "rejected", transfer_id,
    )
    return _payload(
        await transfers_repo.get(worker.owner_id, transfer_id), duplicate=False
    )


# -- the owner's side --------------------------------------------------------

async def move_as_owner(
    owner_id: int,
    item_id: int,
    from_store_id: int,
    to_store_id: int,
    quantity: int,
    note: str | None = None,
) -> dict:
    """Move stock from the website. No approval — the owner is both shops.

    Recorded as a transfer all the same, and marked as decided by the owner, so the
    history reads the same whichever way the box travelled.
    """
    if from_store_id == to_store_id:
        raise AppError("validation_error", "Ընտրեք երկու տարբեր խանութ։")
    if not (0 < quantity <= MAX_QUANTITY):
        raise AppError("validation_error", "Սխալ քանակ։")
    for store_id in (from_store_id, to_store_id):
        if await stores_repo.get(owner_id, store_id) is None:
            raise AppError("not_found", "Խանութը չի գտնվել։")

    item = await items_repo.get(owner_id, item_id)
    if item is None or item["store_id"] != from_store_id:
        raise AppError("not_found", "Ապրանքը չի գտնվել այդ խանութում։")

    async with db.transaction() as conn:
        moved = await _move_stock(
            conn,
            owner_id,
            {
                "item_id": item_id,
                "quantity": quantity,
                "from_store_id": from_store_id,
                "to_store_id": to_store_id,
            },
        )
        transfer_id = await transfers_repo.insert(
            conn,
            owner_id=owner_id,
            item_id=item_id,
            from_store_id=from_store_id,
            to_store_id=to_store_id,
            quantity=quantity,
            unit_cost=moved["unit_cost"],
            requested_by_worker_id=None,
            status="approved",
            decided_by_owner=True,
            note=note,
        )
    log.info(
        "owner %s moved %s of item %s from store %s to %s",
        owner_id, quantity, item_id, from_store_id, to_store_id,
    )
    return {"id": transfer_id, "name": moved["name"], "quantity": quantity}


def _payload(row, *, duplicate: bool) -> dict:
    return {
        "ok": True,
        "duplicate": duplicate,
        "transfer": {
            "id": row["id"],
            "item_name": row["item_name"],
            "quantity": row["quantity"],
            "status": row["status"],
            "from_store": row["from_store_name"],
            "to_store": row["to_store_name"],
        },
    }
