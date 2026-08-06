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
from app.repo import items as items_repo
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.repo import workers as workers_repo
from app.schemas import (
    CheckinRequest,
    CloseStoreRequest,
    EndShiftRequest,
    OpenStoreRequest,
    SaleRequest,
    VoidRequest,
)
from app.services import sales as sales_service
from app.services import shifts as shifts_service
from app.services.geofence import match_store

log = logging.getLogger("storemanager.bot_api")

router = APIRouter(prefix="/api/bot/v1", dependencies=[Depends(require_bot_secret)])


async def _worker(telegram_id: int, telegram_name: str | None = None) -> shifts_service.Worker:
    """Resolve the caller. This is also what resolves the tenant: a telegram_id
    belongs to exactly one owner, and everything downstream is scoped by it.

    Registration stays closed. An id the owner has not entered on /workers is
    refused here, which is the whole access-control story for the bot — there is
    no self-registration and no way for a stranger standing outside a shop to
    become a worker.
    """
    row = await workers_repo.by_telegram_id(telegram_id)
    if row is None:
        raise BotError("unknown_worker")
    if not row["is_active"]:
        raise BotError("worker_inactive")

    # Learn the profile name so the owner never has to type one. Deliberately
    # after the checks above: an unknown id must not leave a trace.
    await workers_repo.remember_telegram_name(row["id"], telegram_name)

    return shifts_service.Worker(
        id=row["id"],
        owner_id=row["owner_id"],
        # Freshly reported name beats the stored one, which may be a request old.
        name=(row["name"] if row["name"] != f"ID {telegram_id}" else None)
        or telegram_name
        or f"ID {telegram_id}",
        salary_per_shift=Decimal(row["salary_per_shift"]),
    )


@router.get("/me")
async def me(
    telegram_id: int = Query(gt=0),
    telegram_name: str = Query(default="", max_length=200),
) -> dict:
    """Identity plus whatever shift is open. Safe to call on every /start.

    This is usually the first call the bot makes, so it is where a newly
    registered worker's name normally arrives.
    """
    worker = await _worker(telegram_id, telegram_name or None)
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
            "salary_per_shift": f"{worker.salary_per_shift:.2f}",
        },
        "session": session,
    }


@router.post("/checkin")
async def checkin(body: CheckinRequest) -> dict:
    """Where am I? Writes nothing.

    Returns 200 even when nothing is in range, with ``matched_store: null`` and
    the distances, so the bot can say "you are 1240 m from Store 2" instead of
    just refusing.
    """
    worker = await _worker(body.telegram_id, body.telegram_name)
    match = await match_store(worker.owner_id, body.lat, body.lng)
    return {
        "ok": True,
        "matched_store": match.matched.as_dict() if match.matched else None,
        "candidates": [c.as_dict() for c in match.candidates],
    }


@router.post("/store/open", status_code=201)
async def open_store(body: OpenStoreRequest) -> dict:
    """Requirement 8: location in, attached to a store."""
    worker = await _worker(body.telegram_id, body.telegram_name)
    return await shifts_service.open_store(
        worker, body.lat, body.lng, body.accuracy_m, body.idempotency_key
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
            }
            for row in rows
        ],
    }


@router.post("/sale", status_code=201)
async def sale(body: SaleRequest) -> JSONResponse:
    """Requirement 6: stock down, money up, in one transaction."""
    worker = await _worker(body.telegram_id, body.telegram_name)
    result = await sales_service.record_sale(
        worker,
        [
            {"item_id": line.item_id, "quantity": line.quantity, "unit_price": line.unit_price}
            for line in body.items
        ],
        body.payment_method,
        body.idempotency_key,
    )
    return JSONResponse(result, status_code=201)


@router.post("/sale/void")
async def void_sale(body: VoidRequest) -> dict:
    """Undo the worker's own most recent receipt in this shift."""
    worker = await _worker(body.telegram_id, body.telegram_name)
    return await sales_service.void_last_sale(worker, body.reason)


@router.post("/shift/end")
async def end_shift(body: EndShiftRequest) -> dict:
    """Requirement 5: pay the salary out of the till and close the shift."""
    worker = await _worker(body.telegram_id, body.telegram_name)
    return await shifts_service.end_shift(worker, body.lat, body.lng, body.idempotency_key)


@router.post("/store/close")
async def close_store(body: CloseStoreRequest) -> dict:
    """Close the store for everyone and settle the till."""
    worker = await _worker(body.telegram_id, body.telegram_name)
    return await shifts_service.close_store(worker, body.idempotency_key)
