"""The HTTP contract the Telegram bot speaks.

Every response is either ``{"ok": true, ...}`` or
``{"ok": false, "error": {"code", "message", "details"}}``, where ``message`` is
already Armenian and display-ready — the bot prints it verbatim rather than
translating a code, so the two services cannot drift apart.

The bot holds no store list, no radius and no coordinates. It forwards a
telegram_id, a location and an item id; every decision is made here.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.deps import require_bot_secret
from app.errors import BotError
from app.repo import adjustments as adjustments_repo
from app.repo import items as items_repo
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import transfers as transfers_repo
from app.repo import users as users_repo
from app.repo import workers as workers_repo
from app.repo import write_offs as write_offs_repo
from app.schemas import (
    AdjustStockRequest,
    CheckinRequest,
    CloseoutRequest,
    CloseStoreRequest,
    EndShiftRequest,
    LocationPingRequest,
    NewItemRequest,
    OpenStoreRequest,
    SaleRequest,
    TillCountRequest,
    TransferDecision,
    TransferRequest,
    UndoAdjustRequest,
    VoidRequest,
    WithdrawRequest,
    WriteOffRequest,
)
from app.services import money as money_service
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services import stock as stock_service
from app.services import till as till_service
from app.services import transfers as transfers_service
from app.services import write_offs as write_offs_service
from app.services.geofence import match_store

log = logging.getLogger("storemanager.bot_api")

router = APIRouter(prefix="/api/bot/v1", dependencies=[Depends(require_bot_secret)])


async def _worker(
    telegram_id: int,
    telegram_name: str | None = None,
    telegram_username: str | None = None,
) -> shifts_service.Worker:
    """Resolve the caller, and resolve the tenant with it: a worker belongs to
    exactly one owner, and everything downstream is scoped by that.

    Registration stays closed. The owner writes a ``@username`` on /workers; an
    account matching nothing there is refused, so a stranger who finds the bot
    and stands outside a shop gets nowhere.

    The first message from a registered username *binds* it to that account's
    numeric id, and from then on the id is what identifies them. Usernames can be
    changed or handed to somebody else; a numeric id cannot.
    """
    row = await workers_repo.by_telegram_id(telegram_id)

    if row is None and telegram_username:
        # Never been seen before, but the owner may have written this handle down.
        row = await workers_repo.claim_by_username(telegram_username, telegram_id)
        if row is not None:
            log.info(
                "bound @%s to telegram_id %s (worker %s)",
                telegram_username, telegram_id, row["id"],
            )

    if row is None:
        raise BotError("unknown_worker")
    if not row["is_active"]:
        raise BotError("worker_inactive")

    # Learn the profile name so the owner never has to type one. Deliberately
    # after the checks above: an unrecognised account must not leave a trace.
    await workers_repo.remember_telegram_name(row["id"], telegram_name)

    stored = row["name"]
    generated = stored.startswith(("ID ", "@"))
    return shifts_service.Worker(
        id=row["id"],
        owner_id=row["owner_id"],
        # A freshly reported name beats a placeholder, but never a real one.
        name=(telegram_name or stored) if generated else stored,
        salary_amount=Decimal(row["salary_amount"]),
        salary_period=row["salary_period"],
    )


async def _bind_admin(
    telegram_id: int, telegram_name: str | None, telegram_username: str | None
) -> dict | None:
    """Attach an owner's Telegram account to their Store Manager login.

    A bot cannot open a conversation, so an owner who has never pressed /start
    has no chat a login code could be delivered to. This is where that gets
    fixed, and it is why /start has to work for somebody who is not a worker.
    """
    row = await users_repo.by_telegram_id(telegram_id)
    if row is None and telegram_username:
        row = await users_repo.claim_admin_by_username(
            telegram_username, telegram_id, telegram_name
        )
        if row is not None:
            log.info("bound admin @%s to telegram_id %s", telegram_username, telegram_id)
    if row is None or not row["is_active"]:
        return None
    await users_repo.refresh_telegram_name(row["id"], telegram_name)
    return {"id": row["id"], "label": row["label"]}


@router.get("/me")
async def me(
    telegram_id: int = Query(gt=0),
    telegram_name: str = Query(default="", max_length=200),
    telegram_username: str = Query(default="", max_length=32),
) -> dict:
    """Identity plus whatever shift is open. Safe to call on every /start.

    This is the first call the bot makes, so it is where both a worker's and an
    owner's ``@username`` get bound to their Telegram account.

    An owner who is not also a cashier is a legitimate caller here — binding
    their account is the whole point — so this endpoint answers for them too
    rather than refusing with ``unknown_worker``.
    """
    admin = await _bind_admin(
        telegram_id, telegram_name or None, telegram_username or None
    )

    try:
        worker = await _worker(telegram_id, telegram_name or None, telegram_username or None)
    except BotError:
        if admin is None:
            raise
        # Owner-only: recognised, but has no shifts to report.
        return {"ok": True, "worker": None, "admin": admin, "session": None}

    shift = await sessions_repo.open_for_worker(worker.id)

    session = None
    if shift is not None:
        sold = await sales_repo.summary_for_work_session(shift["id"])
        totals = await money_repo.totals_for_session(shift["store_session_id"])
        session = {
            "id": shift["id"],
            "store_id": shift["store_id"],
            "store_name": shift["store_name"],
            "started_at": shift["started_at"].isoformat(),
            "sales": {
                "receipts": sold["receipts"],
                "cash_total": f"{sold['cash_total']:.2f}",
                "card_total": f"{sold['card_total']:.2f}",
                "total": f"{sold['total']:.2f}",
            },
            "store_totals": {
                "cash": f"{totals['cash']:.2f}",
                "card": f"{totals['card']:.2f}",
            },
        }

    return {
        "ok": True,
        "worker": {
            "id": worker.id,
            "name": worker.name,
            "salary_amount": f"{worker.salary_amount:.2f}",
        },
        "admin": admin,
        "session": session,
    }


@router.post("/checkin")
async def checkin(body: CheckinRequest) -> dict:
    """Where am I? Writes nothing.

    Returns 200 even when nothing is in range, with ``matched_store: null`` and
    the distances, so the bot can say "you are 1240 m from Store 2" instead of
    just refusing.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    match = await match_store(worker.owner_id, body.lat, body.lng)
    return {
        "ok": True,
        "matched_store": match.matched.as_dict() if match.matched else None,
        "candidates": [c.as_dict() for c in match.candidates],
    }


@router.post("/store/open", status_code=201)
async def open_store(body: OpenStoreRequest) -> dict:
    """Requirement 8: live location in, attached to a store."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await shifts_service.open_store(
        worker, body.lat, body.lng, body.accuracy_m, body.idempotency_key,
        live_period=body.live_period,
    )


@router.post("/location/ping")
async def location_ping(body: LocationPingRequest) -> dict:
    """One reading from a live location, while the shift runs.

    Telegram edits the original message as the device moves and the bot forwards
    each edit here. Cheap on purpose: no idempotency key, no locking, one insert
    and one update — this is called every time a worker walks down the street.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await shifts_service.record_position(
        worker, body.lat, body.lng, body.accuracy_m
    )


@router.get("/items")
async def items(
    telegram_id: int = Query(gt=0),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Stock of the store where this worker's shift is open.

    Backs the bot's "type part of a name" flow. There is no store parameter: the
    open shift decides, so a worker cannot browse a store they are not in.
    """
    worker = await _worker(telegram_id)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    store_id = shift["store_id"]
    rows = (
        await items_repo.search_in_store(store_id, q, limit)
        if q.strip()
        else await items_repo.list_in_store_for_bot(store_id, limit, offset)
    )
    return {
        "ok": True,
        "store_id": store_id,
        "store_name": shift["store_name"],
        "total": await items_repo.count_in_store(store_id),
        "items": [
            {
                "id": row["id"],
                "name": row["name"],
                "count": row["count"],
                "sell_price": f"{Decimal(row['sell_price']):.2f}",
                # Null when this product is not sold wholesale, which is a real
                # answer and not a missing one. Its absence from this payload is
                # what made the bot's «Մեծածախ» button unreachable: the keyboard
                # only offers it when there is a price to offer, and there never
                # was one here to see.
                "wholesale_price": (
                    f"{Decimal(row['wholesale_price']):.2f}"
                    if row["wholesale_price"] is not None
                    else None
                ),
            }
            for row in rows
        ],
    }


@router.get("/transfers/stores")
async def transfer_sources(telegram_id: int = Query(gt=0)) -> dict:
    """The owner's other shops — the ones stock could be asked for from.

    The worker's own shop is left out: you cannot ask yourself for a box.
    """
    worker = await _worker(telegram_id)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    return {
        "ok": True,
        # list_for_owner already leaves out deactivated shops.
        "stores": [
            {"id": row["id"], "name": row["name"]}
            for row in await stores_repo.list_for_owner(worker.owner_id)
            if row["id"] != shift["store_id"]
        ],
    }


@router.get("/transfers/items")
async def transfer_items(
    telegram_id: int = Query(gt=0),
    store_id: int = Query(gt=0),
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
) -> dict:
    """What another of the owner's shops has on its shelf.

    Names and counts only — no prices. The cashier is choosing a box to ask for,
    not selling it, and what a sister shop charges is not part of that job.

    Reachable only while on shift, and only for a shop belonging to the same owner,
    so this is not a way to read somebody else's stock.
    """
    worker = await _worker(telegram_id)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")
    if store_id == shift["store_id"]:
        raise BotError("validation_error", "Ընտրեք այլ խանութ։")
    store = await stores_repo.get(worker.owner_id, store_id)
    if store is None:
        raise BotError("unknown_item")

    # Out of stock is filtered in the query, not here: there is nothing to send from
    # an empty shelf, and filtering an already-limited page would drop rows the
    # limit had counted — a shop whose first products are at zero would read as
    # having nothing when it has plenty.
    rows = (
        await items_repo.search_in_store(store_id, q, limit, in_stock_only=True)
        if q.strip()
        else await items_repo.list_in_store_for_bot(store_id, limit, 0, in_stock_only=True)
    )
    return {
        "ok": True,
        "store_id": store_id,
        "store_name": store["name"],
        "items": [
            {"id": row["id"], "name": row["name"], "count": row["count"]}
            for row in rows
        ],
    }


@router.post("/transfers", status_code=201)
async def request_transfer(body: TransferRequest) -> dict:
    """Ask another shop for stock. Moves nothing until somebody there agrees."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await transfers_service.request_by_worker(
        worker, body.from_store_id, body.item_id, body.quantity, body.idempotency_key
    )


@router.get("/transfers/pending")
async def pending_transfers(telegram_id: int = Query(gt=0)) -> dict:
    """Both sides of the worker's shop: what it is being asked for, and what it has
    asked for and is still waiting on."""
    worker = await _worker(telegram_id)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    incoming = await transfers_repo.pending_for_store(worker.owner_id, shift["store_id"])
    return {
        "ok": True,
        "store_name": shift["store_name"],
        "incoming": [
            {
                "id": row["id"],
                "item_name": row["item_name"],
                "quantity": row["quantity"],
                "to_store": row["to_store_name"],
            }
            for row in incoming
        ],
    }


@router.post("/transfers/{transfer_id}/decide")
async def decide_transfer(transfer_id: int, body: TransferDecision) -> dict:
    """Approve or reject a request, as somebody standing in the shop being asked.

    Approving moves the stock in the same transaction. An approval that did not
    move it would be a promise, and the shelf would disagree with the screen.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await transfers_service.decide_by_worker(worker, transfer_id, body.approve)


@router.post("/sale", status_code=201)
async def sale(body: SaleRequest) -> JSONResponse:
    """Requirement 6: stock down, money up, in one transaction."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    result = await sales_service.record_sale(
        worker,
        [
            {
                "item_id": line.item_id,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "price_kind": line.price_kind,
            }
            for line in body.items
        ],
        body.payment_method,
        body.idempotency_key,
        body.is_delivery,
    )
    return JSONResponse(result, status_code=201)


@router.post("/sale/void")
async def void_sale(body: VoidRequest) -> dict:
    """Undo the worker's own most recent receipt in this shift."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await sales_service.void_last_sale(worker, body.reason, body.sale_id)


@router.post("/items", status_code=201)
async def add_item(body: NewItemRequest) -> dict:
    """Put a new product on the shelf, from the counter.

    The store comes from the open shift, like every other bot write: a cashier
    cannot stock a shop they are not standing in.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    item_id = await items_repo.create(
        owner_id=worker.owner_id,
        store_id=shift["store_id"],
        name=body.name.strip(),
        count=body.count,
        self_price=body.self_price,
        sell_price=body.sell_price,
        wholesale_price=body.wholesale_price,
    )
    if item_id is None:
        # The name belongs to something already on the list. Saying so beats
        # silently replacing a colleague's prices.
        raise BotError(
            "validation_error",
            f"«{body.name.strip()}» արդեն կա այս խանութի ցուցակում։",
        )

    log.info("worker %s added item %s to store %s", worker.id, item_id, shift["store_id"])
    item = await items_repo.get(worker.owner_id, item_id)
    return {
        "ok": True,
        "item": {
            "id": item["id"],
            "name": item["name"],
            "count": item["count"],
            "sell_price": f"{Decimal(item['sell_price']):.2f}",
            # Null, not "0.00", when the item is not sold wholesale — the bot
            # prints this line only when there is one.
            "wholesale_price": (
                f"{Decimal(item['wholesale_price']):.2f}"
                if item["wholesale_price"] is not None
                else None
            ),
        },
    }


@router.post("/shift/till", status_code=201)
async def count_the_till(body: TillCountRequest) -> dict:
    """What the worker is leaving in the shop's drawer.

    Three things at once, in one transaction: the count is recorded, it becomes the
    store's float for tomorrow, and everything above it is booked out of the till as
    handed to the owner.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await till_service.declare_close(
        worker, body.counted, body.idempotency_key
    )


@router.get("/shift/review")
async def review_this_shift(telegram_id: int = Query(gt=0)) -> dict:
    """The worker's whole shift, read back to them before they end it.

    Not just the sales. A shift accounts for everything that moved while they were
    on it — what went out at the counter, what was thrown away, what was corrected
    on the shelf, what came out of the drawer and what the drawer holds now — and a
    review that showed one of those and settled all of them is not a review. This
    is the last moment anything can be corrected without the owner, so it has to be
    the whole picture.

    Sales already rung up matter twice over: the write-up is only for what is *not*
    in that list, and without seeing it the only thing telling the two apart is
    memory — a product declared twice comes off the shelf twice.
    """
    worker = await _worker(telegram_id)
    shift = await sessions_repo.open_for_worker(worker.id)
    if shift is None:
        raise BotError("no_open_session")

    lines = await sales_repo.lines_in_work_session(shift["id"])
    totals = await sales_repo.summary_for_work_session(shift["id"])
    store = await stores_repo.get(worker.owner_id, shift["store_id"])
    till = await money_repo.totals_for_session(shift["store_session_id"])
    return {
        "ok": True,
        "store_name": shift["store_name"],
        "started_at": shift["started_at"].isoformat(),
        "sold": [
            {
                "name": row["name"],
                "quantity": row["quantity"],
                "total": f"{Decimal(row['total']):.2f}",
            }
            for row in lines
        ],
        "totals": {
            "receipts": totals["receipts"],
            "cash": f"{totals['cash_total']:.2f}",
            "card": f"{totals['card_total']:.2f}",
            "total": f"{totals['total']:.2f}",
        },
        "written_off": [
            {"name": row["name"], "quantity": row["quantity"], "reason": row["reason"]}
            for row in await write_offs_repo.for_work_session(shift["id"])
        ],
        "stock_fixed": [
            {"name": row["name"], "delta": row["delta"], "count_after": row["count_after"]}
            for row in await adjustments_repo.for_work_session(shift["id"])
        ],
        "taken_out": [
            {"amount": f"{Decimal(row['amount']):.2f}", "purpose": row["note"]}
            for row in await money_repo.taken_out_during_shift(shift["id"])
        ],
        # Stock that moved between shops while they were on. An arrival was decided by
        # somebody at the other shop rather than by them, but it changed the shelf they
        # are about to be held to.
        "transfers": [
            {
                "name": row["item_name"],
                "quantity": row["quantity"],
                "incoming": row["to_store_id"] == shift["store_id"],
                "other_store": (
                    row["from_store_name"] if row["to_store_id"] == shift["store_id"]
                    else row["to_store_name"]
                ),
            }
            for row in await transfers_repo.settled_for_store_since(
                worker.owner_id, shift["store_id"], shift["started_at"]
            )
        ],
        # The drawer as it stands: the whole store session's cash, not this
        # worker's share of it. That is the money they are about to count, and
        # splitting it per person would describe a drawer that does not exist.
        "till": {
            "cash": f"{Decimal(till['cash']):.2f}",
            "card": f"{Decimal(till['card']):.2f}",
        },
        # What the shop keeps when the day is settled. Quoted here so the count
        # that follows is an informed number rather than a guess.
        "store_float": f"{Decimal(store['till_balance'] if store else 0):.2f}",
        # What ending the shift will cost, worked out here rather than in the bot:
        # the halving rule for a short shift lives on this side and must not be
        # reimplemented on a phone.
        "salary": {
            "due": f"{shifts_service.salary_for_hours_worked(worker.salary_due_at_shift_end, shift['started_at']):.2f}",
            "full": f"{worker.salary_due_at_shift_end:.2f}",
            "full_shift_hours": shifts_service.FULL_SHIFT_HOURS,
        },
    }


@router.post("/items/adjust", status_code=201)
async def adjust_stock(body: AdjustStockRequest) -> dict:
    """Correct what the shelf says, from the counter.

    Neither a sale nor breakage — the count on the screen had drifted from the
    count on the shelf. Every correction is logged against the worker who made it,
    because a number that changes silently is a number that can hide a shortfall.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await stock_service.adjust_by_worker(
        worker,
        [{"item_id": line.item_id, "delta": line.delta} for line in body.lines],
        body.idempotency_key,
        body.note,
    )


@router.post("/items/adjust/undo", status_code=201)
async def undo_adjust_stock(body: UndoAdjustRequest) -> dict:
    """Take back a correction the cashier has just made.

    The rows they wrote stay and the opposite correction is written beside them —
    the same rule a voided sale follows, where the receipt is kept and struck
    through rather than removed. A worker undoing their own slip must not be able
    to make it disappear.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await stock_service.undo_by_worker(
        worker, body.external_id, body.idempotency_key
    )


@router.post("/cash/withdraw", status_code=201)
async def withdraw(body: WithdrawRequest) -> dict:
    """Money out of the till at the counter, with the reason written down.

    It leaves whether or not the system knows, so the choice is between an
    unexplained shortfall at close and a row saying where it went.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await money_service.withdraw_by_worker(
        worker, body.amount, body.purpose, body.idempotency_key
    )


@router.post("/write-off", status_code=201)
async def write_off(body: WriteOffRequest) -> dict:
    """Damaged or expired stock, taken off the shelf.

    Deliberately not a sale of zero: that would land in the revenue figures, the
    receipt count and the worker's bonus progress, and the last of those would
    pay somebody for breaking things.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await write_offs_service.record(
        worker, body.item_id, body.quantity, body.reason, body.idempotency_key
    )


@router.post("/shift/close-out")
async def close_out(body: CloseoutRequest) -> dict:
    """End the shift and record everything sold during it, atomically.

    This is the normal way a sale reaches the system: the cashier serves
    customers without touching the bot and writes the day up once, at the end.
    """
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await shifts_service.close_out_shift(
        worker,
        [
            {
                "item_id": line.item_id,
                "quantity": line.quantity,
                "unit_price": line.unit_price,
                "payment_method": line.payment_method,
                "is_delivery": line.is_delivery,
            }
            for line in body.lines
        ],
        body.idempotency_key,
        lat=body.lat,
        lng=body.lng,
        close_store_too=body.close_store,
    )


@router.post("/shift/end")
async def end_shift(body: EndShiftRequest) -> dict:
    """Requirement 5: pay the salary out of the till and close the shift."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await shifts_service.end_shift(worker, body.lat, body.lng, body.idempotency_key)


@router.post("/store/close")
async def close_store(body: CloseStoreRequest) -> dict:
    """Close the store for everyone and settle the till."""
    worker = await _worker(body.telegram_id, body.telegram_name, body.telegram_username)
    return await shifts_service.close_store(worker, body.idempotency_key)
