"""The owner-facing pages: stores and stock."""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import forms
from app.deps import CurrentUser, current_user, require_csrf
from app.errors import AppError
from app.repo import items as items_repo
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.repo import stores as stores_repo
from app.repo import workers as workers_repo
from app.services import money as money_service
from app.services import shifts as shifts_service
from app.templating import render

log = logging.getLogger("storemanager.pages")

router = APIRouter()


async def _store_or_404(owner_id: int, store_id: int):
    """A store belonging to somebody else reads as missing.

    Answering 403 would confirm the id exists, which is a small leak but a free
    one to avoid.
    """
    store = await stores_repo.get(owner_id, store_id)
    if store is None:
        raise AppError("not_found", "Խանութը չի գտնվել։")
    return store


async def _items_context(owner_id: int, store_id: int, sort: str, desc: bool) -> dict:
    return {
        "store_id": store_id,
        "items": await items_repo.list_for_store(owner_id, store_id, sort, desc),
        "summary": await items_repo.summary_for_store(owner_id, store_id),
        "sort": sort if sort in items_repo.SORTS else items_repo.DEFAULT_SORT,
        "desc": desc,
    }


async def _status_context(owner_id: int, store_id: int) -> dict:
    """Requirement 3: who is on shift here, and what is in the till."""
    session = await sessions_repo.open_for_store(store_id)
    totals = (
        await money_repo.totals_for_session(session["id"]) if session is not None else None
    )
    return {
        "store_id": store_id,
        "store_session": session,
        "totals": totals,
        "on_shift": await sessions_repo.workers_on_shift(store_id) if session else [],
    }


# -- stores ------------------------------------------------------------------

@router.get("/stores")
async def stores_page(request: Request, user: CurrentUser = Depends(current_user)):
    return render(
        request,
        "stores.html",
        {"user": user, "active": "stores", "stores": await money_repo.totals_by_store(user.id)},
    )


@router.post("/stores")
async def create_store(
    request: Request,
    name: str = Form(""),
    address: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    radius_m: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    latitude, longitude = forms.coordinate_pair(lat, lng)
    store_id = await stores_repo.create(
        owner_id=user.id,
        name=forms.text(name, "Անվանում", max_length=120),
        address=forms.text(address, "Հասցե", max_length=300, required=False),
        lat=latitude,
        lng=longitude,
        radius_m=forms.whole(radius_m, "Շառավիղ", default=120, minimum=50, maximum=5000),
    )
    log.info("store %s created by user %s", store_id, user.id)
    return RedirectResponse(f"/stores/{store_id}", status_code=303)


@router.get("/stores/{store_id}")
async def store_page(
    request: Request,
    store_id: int,
    sort: str = "name",
    desc: bool = False,
    user: CurrentUser = Depends(current_user),
):
    store = await _store_or_404(user.id, store_id)
    return render(
        request,
        "store_detail.html",
        {
            "user": user,
            "active": "stores",
            "store": store,
            **await _items_context(user.id, store_id, sort, desc),
            "status": await _status_context(user.id, store_id),
        },
    )


@router.post("/stores/{store_id}")
async def edit_store(
    request: Request,
    store_id: int,
    name: str = Form(""),
    address: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    radius_m: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    await _store_or_404(user.id, store_id)
    latitude, longitude = forms.coordinate_pair(lat, lng)
    await stores_repo.update(
        owner_id=user.id,
        store_id=store_id,
        name=forms.text(name, "Անվանում", max_length=120),
        address=forms.text(address, "Հասցե", max_length=300, required=False),
        lat=latitude,
        lng=longitude,
        radius_m=forms.whole(radius_m, "Շառավիղ", default=120, minimum=50, maximum=5000),
    )
    return RedirectResponse(f"/stores/{store_id}", status_code=303)


@router.post("/stores/{store_id}/delete")
async def delete_store(
    store_id: int, user: CurrentUser = Depends(require_csrf)
):
    await _store_or_404(user.id, store_id)
    if await sessions_repo.open_for_store(store_id) is not None:
        raise AppError(
            "validation_error",
            "Խանութը բաց է։ Նախ փակեք հերթափոխը, ապա ջնջեք։",
        )
    await stores_repo.deactivate(user.id, store_id)
    log.info("store %s deactivated by user %s", store_id, user.id)
    return RedirectResponse("/stores", status_code=303)


# -- items -------------------------------------------------------------------

@router.post("/stores/{store_id}/items")
async def add_item(
    request: Request,
    store_id: int,
    name: str = Form(""),
    count: str = Form(""),
    self_price: str = Form(""),
    sell_price: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    await _store_or_404(user.id, store_id)
    await items_repo.create(
        owner_id=user.id,
        store_id=store_id,
        name=forms.text(name, "Անվանում", max_length=200),
        count=forms.whole(count, "Քանակ", default=0),
        self_price=forms.money(self_price, "Ինքնարժեք", default=None),
        sell_price=forms.money(sell_price, "Վաճառքի գին", default=None),
    )
    return render(
        request, "_items_table.html", await _items_context(user.id, store_id, "name", False)
    )


@router.post("/items/{item_id}")
async def edit_item(
    request: Request,
    item_id: int,
    name: str = Form(""),
    self_price: str = Form(""),
    sell_price: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    item = await items_repo.get(user.id, item_id)
    if item is None or not item["is_active"]:
        raise AppError("not_found", "Ապրանքը չի գտնվել։")
    # Count is intentionally not editable here; see items_repo.update_details.
    await items_repo.update_details(
        owner_id=user.id,
        item_id=item_id,
        name=forms.text(name, "Անվանում", max_length=200),
        self_price=forms.money(self_price, "Ինքնարժեք"),
        sell_price=forms.money(sell_price, "Վաճառքի գին"),
    )
    return render(
        request,
        "_items_table.html",
        await _items_context(user.id, item["store_id"], "name", False),
    )


@router.post("/items/{item_id}/restock")
async def restock_item(
    request: Request,
    item_id: int,
    delta: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    item = await items_repo.get(user.id, item_id)
    if item is None or not item["is_active"]:
        raise AppError("not_found", "Ապրանքը չի գտնվել։")
    amount = forms.whole(delta, "Քանակ", minimum=-1_000_000, maximum=1_000_000)
    if amount == 0:
        raise AppError("validation_error", "Քանակը 0 է — փոփոխելու բան չկա։")
    if await items_repo.restock(user.id, item_id, amount) is None:
        raise AppError(
            "validation_error",
            f"«{item['name']}» — պահեստում կա ընդամենը {item['count']} հատ։",
        )
    return render(
        request,
        "_items_table.html",
        await _items_context(user.id, item["store_id"], "name", False),
    )


@router.post("/items/{item_id}/delete")
async def delete_item(
    request: Request, item_id: int, user: CurrentUser = Depends(require_csrf)
):
    item = await items_repo.get(user.id, item_id)
    if item is None:
        raise AppError("not_found", "Ապրանքը չի գտնվել։")
    await items_repo.deactivate(user.id, item_id)
    return render(
        request,
        "_items_table.html",
        await _items_context(user.id, item["store_id"], "name", False),
    )


# -- the till ----------------------------------------------------------------

@router.post("/stores/{store_id}/movement")
async def record_movement(
    request: Request,
    store_id: int,
    amount: str = Form(""),
    method: str = Form("cash"),
    kind: str = Form("withdrawal"),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    await _store_or_404(user.id, store_id)
    await money_service.record_movement(
        owner_id=user.id,
        store_id=store_id,
        method=method,
        kind=kind,
        amount=forms.money(amount, "Գումար"),
        note=forms.text(note, "Նշում", max_length=300, required=False),
    )
    return render(request, "_store_status.html", await _status_context(user.id, store_id))


@router.post("/store-sessions/{store_session_id}/close")
async def close_store_session(
    request: Request, store_session_id: int, user: CurrentUser = Depends(require_csrf)
):
    """The force-close button, for when a worker went home without pressing it."""
    session = await sessions_repo.get_store_session(user.id, store_session_id)
    if session is None:
        raise AppError("not_found", "Հերթափոխը չի գտնվել։")
    await shifts_service.close_store_session_as_owner(user.id, store_session_id)
    return render(
        request, "_store_status.html", await _status_context(user.id, session["store_id"])
    )


# -- workers -----------------------------------------------------------------

@router.get("/workers")
async def workers_page(request: Request, user: CurrentUser = Depends(current_user)):
    return render(
        request,
        "workers.html",
        {
            "user": user,
            "active": "workers",
            "workers": await workers_repo.list_for_owner(user.id),
        },
    )


@router.post("/workers")
async def create_worker(
    name: str = Form(""),
    telegram_id: str = Form(""),
    salary_per_shift: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """Register a Telegram id. The name is optional — it arrives from Telegram
    the first time that person uses the bot."""
    try:
        await workers_repo.create(
            owner_id=user.id,
            name=forms.text(name, "Անուն", max_length=120, required=False),
            telegram_id=forms.whole(telegram_id, "Telegram ID", minimum=1,
                                    maximum=9_999_999_999_999),
            salary_per_shift=forms.money(salary_per_shift, "Աշխատավարձ",
                                         default=Decimal("0.00")),
        )
    except asyncpg.exceptions.UniqueViolationError:
        # telegram_id is globally unique: it is the only thing in a bot request
        # that identifies an owner, so it cannot be shared.
        raise AppError(
            "validation_error",
            "Այս Telegram ID-ն արդեն գրանցված է։ Մեկ Telegram հաշիվը կարող է "
            "պատկանել միայն մեկ գործատուի։",
        ) from None
    return RedirectResponse("/workers", status_code=303)


@router.get("/reports")
async def reports_page(
    request: Request,
    store_session_id: int | None = None,
    user: CurrentUser = Depends(current_user),
):
    """One row per time a store was open — the period the money actually uses."""
    detail = None
    if store_session_id is not None:
        session = await sessions_repo.get_store_session(user.id, store_session_id)
        if session is None:
            raise AppError("not_found", "Հերթափոխը չի գտնվել։")
        detail = {
            "session": session,
            "totals": await money_repo.totals_for_session(store_session_id),
            "shifts": await sessions_repo.shifts_in_session(store_session_id),
            "receipts": await sales_repo.receipts_in_store_session(store_session_id),
            "ledger": await money_repo.ledger_for_session(store_session_id),
        }
    return render(
        request,
        "reports.html",
        {
            "user": user,
            "active": "reports",
            "sessions": await sessions_repo.recent_store_sessions(user.id),
            "detail": detail,
        },
    )


@router.post("/workers/{worker_id}")
async def edit_worker(
    worker_id: int,
    name: str = Form(""),
    telegram_id: str = Form(""),
    salary_per_shift: str = Form(""),
    is_active: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    if await workers_repo.get(user.id, worker_id) is None:
        raise AppError("not_found", "Աշխատողը չի գտնվել։")
    try:
        await workers_repo.update(
            owner_id=user.id,
            worker_id=worker_id,
            name=forms.text(name, "Անուն", max_length=120, required=False),
            telegram_id=forms.whole(telegram_id, "Telegram ID", minimum=1,
                                    maximum=9_999_999_999_999),
            salary_per_shift=forms.money(salary_per_shift, "Աշխատավարձ"),
            is_active=is_active in {"1", "on", "true"},
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise AppError(
            "validation_error", "Այս Telegram ID-ն արդեն գրանցված է։"
        ) from None
    return RedirectResponse("/workers", status_code=303)
