"""The owner-facing pages: stores and stock."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse

from app import charts, forms
from app.config import settings
from app.db import db
from app.deps import CurrentUser, current_user, require_csrf
from app.errors import AppError
from app.repo import adjustments as adjustments_repo
from app.repo import audit as audit_repo
from app.repo import expenses as expenses_repo
from app.repo import items as items_repo
from app.repo import money as money_repo
from app.repo import sales as sales_repo
from app.repo import sessions as sessions_repo
from app.repo import spending as spending_repo
from app.repo import stats as stats_repo
from app.repo import stores as stores_repo
from app.repo import till as till_repo
from app.repo import transfers as transfers_repo
from app.repo import workers as workers_repo
from app.repo import write_offs as write_offs_repo
from app.schemas import PRICE_KINDS
from app.services import corrections, statistics
from app.services import money as money_service
from app.services import shifts as shifts_service
from app.services import till as till_service
from app.services import transfers as transfers_service
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
    store = await _store_or_404(owner_id, store_id)
    return {
        "store_id": store_id,
        "store_session": session,
        "totals": totals,
        "on_shift": await sessions_repo.workers_on_shift(store_id) if session else [],
        # Shown whether or not the store is open: closing settles the till, but
        # the shop still sold what it sold this morning.
        "day": await money_repo.day_totals_for_store(owner_id, store_id),
        # The shop's own float, and the story of how it got to that figure. Shown
        # whether or not the shop is open, because the cash is there either way —
        # closing settles the *till*, it does not empty the drawer.
        "till_balance": store["till_balance"],
        "drawer": await till_repo.last_count_for_store(owner_id, store_id),
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
    day_start_hour: str = Form(""),
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
        day_start_hour=forms.whole(
            day_start_hour, "Օրվա սկիզբ",
            default=settings.default_day_start_hour, minimum=0, maximum=23,
        ),
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
    day_start_hour: str = Form(""),
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
        day_start_hour=forms.whole(
            day_start_hour, "Օրվա սկիզբ",
            default=settings.default_day_start_hour, minimum=0, maximum=23,
        ),
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
    wholesale_price: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    await _store_or_404(user.id, store_id)
    clean_name = forms.text(name, "Անվանում", max_length=200)
    item_id = await items_repo.create(
        owner_id=user.id,
        store_id=store_id,
        name=clean_name,
        count=forms.whole(count, "Քանակ", default=0),
        self_price=forms.money(self_price, "Ինքնարժեք", default=None),
        sell_price=forms.money(sell_price, "Մանրածախ գին", default=None),
        wholesale_price=forms.optional_money(wholesale_price, "Մեծածախ գին"),
    )
    if item_id is None:
        # The name belongs to an item that is still on the list. Overwriting it
        # would replace somebody's prices without saying so.
        raise AppError(
            "validation_error",
            f"«{clean_name}» արդեն կա այս խանութում։ Խմբագրեք ցուցակի տողը։",
        )
    return render(
        request, "_items_table.html", await _items_context(user.id, store_id, "name", False)
    )


@router.post("/items/{item_id}")
async def edit_item(
    request: Request,
    item_id: int,
    name: str = Form(""),
    count: str = Form(""),
    self_price: str = Form(""),
    sell_price: str = Form(""),
    wholesale_price: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """One row, one save: the name, the prices and the count together.

    The count is written as typed rather than as a delta. It is what the owner
    just counted off the shelf, so it is the last word.
    """
    item = await items_repo.get(user.id, item_id)
    if item is None or not item["is_active"]:
        raise AppError("not_found", "Ապրանքը չի գտնվել։")
    await items_repo.update_details(
        owner_id=user.id,
        item_id=item_id,
        name=forms.text(name, "Անվանում", max_length=200),
        self_price=forms.money(self_price, "Ինքնարժեք"),
        sell_price=forms.money(sell_price, "Մանրածախ գին"),
        wholesale_price=forms.optional_money(wholesale_price, "Մեծածախ գին"),
        count=forms.whole(count, "Քանակ", minimum=0, maximum=1_000_000),
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
        # Required, like the reason on a cashier's withdrawal and on the report's own
        # «Ավելացնել գրառում»: an amount with no reason is the shortfall with a number
        # attached, and the schema has refused one on an owner-typed row since 018.
        note=forms.text(note, "Նշում", max_length=300),
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


_USERNAME_TAKEN = (
    "Այս Telegram օգտանունն արդեն գրանցված է։ Մեկ Telegram հաշիվը կարող է "
    "պատկանել միայն մեկ գործատուի։"
)
_BAD_USERNAME = (
    "Telegram օգտանունը սխալ է։ Գրեք @-ով, օրինակ՝ @օգտանուն "
    "(4–32 նիշ՝ լատինատառ, թվեր և _)։"
)


def _username(raw: str) -> str:
    cleaned = workers_repo.normalise_username(raw)
    if not cleaned:
        raise AppError("validation_error", _BAD_USERNAME)
    return cleaned


def _salary_period(raw: str) -> str:
    if raw not in workers_repo.SALARY_PERIODS:
        raise AppError("validation_error", "Ընտրեք աշխատավարձի պարբերականությունը։")
    return raw


def _bonus(threshold: str, amount: str, period: str) -> tuple[Decimal, Decimal, str] | None:
    """The bonus rule, or None when the owner has not set one.

    All three parts move together. A threshold with no amount would never pay
    and a bonus with no threshold would pay every shift, so a half-filled rule
    is refused at the form rather than stored as something nobody meant.
    """
    target = forms.optional_money(threshold, "Բոնուսի շեմ")
    reward = forms.optional_money(amount, "Բոնուսի գումար")
    if target is None and reward is None:
        return None
    if target is None or reward is None or target <= 0 or reward <= 0:
        raise AppError(
            "validation_error",
            "Բոնուսի համար լրացրեք և՛ շեմը, և՛ գումարը՝ երկուսն էլ զրոյից մեծ։",
        )
    if period not in {"day", "month"}:
        raise AppError("validation_error", "Բոնուսի ժամանակահատվածն անհայտ է։")
    return target, reward, period

@router.post("/workers")
async def create_worker(
    name: str = Form(""),
    telegram_username: str = Form(""),
    salary_amount: str = Form(""),
    salary_period: str = Form("shift"),
    bonus_threshold: str = Form(""),
    bonus_amount: str = Form(""),
    bonus_period: str = Form("day"),
    user: CurrentUser = Depends(require_csrf),
):
    """Register a @username. The name arrives from Telegram on first contact."""
    try:
        await workers_repo.create(
            owner_id=user.id,
            telegram_username=_username(telegram_username),
            salary_amount=forms.money(salary_amount, "Աշխատավարձ", default=Decimal("0.00")),
            salary_period=_salary_period(salary_period),
            name=forms.text(name, "Անուն", max_length=120, required=False),
            bonus=_bonus(bonus_threshold, bonus_amount, bonus_period),
        )
    except asyncpg.exceptions.UniqueViolationError:
        # Globally unique: it is what resolves an unbound worker to one owner.
        raise AppError("validation_error", _USERNAME_TAKEN) from None
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{worker_id}/delete")
async def delete_worker(worker_id: int, user: CurrentUser = Depends(require_csrf)):
    """Take a worker off the list.

    Somebody who never started a shift is removed outright. Anybody else is
    archived instead: their shifts and receipts are what past reports are made
    of, and the foreign keys cascade, so deleting the row would silently rewrite
    history. Either way their @username is released for reuse.
    """
    worker = await workers_repo.get(user.id, worker_id)
    if worker is None:
        raise AppError("not_found", "Աշխատողը չի գտնվել։")
    if await sessions_repo.open_for_worker(worker_id) is not None:
        raise AppError(
            "validation_error",
            "Աշխատողը հիմա հերթափոխի մեջ է։ Նախ ավարտեք հերթափոխը։",
        )

    if await workers_repo.has_history(user.id, worker_id):
        await workers_repo.archive(user.id, worker_id)
        log.info("worker %s archived by user %s", worker_id, user.id)
    else:
        await workers_repo.delete_outright(user.id, worker_id)
        log.info("worker %s deleted by user %s (never worked)", worker_id, user.id)
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{worker_id}/unbind")
async def unbind_worker(worker_id: int, user: CurrentUser = Depends(require_csrf)):
    """Forget which Telegram account this registration belongs to.

    For when the wrong person claimed the handle, or somebody left and their
    username was reused. The next matching account to make contact claims it.
    """
    if await workers_repo.get(user.id, worker_id) is None:
        raise AppError("not_found", "Աշխատողը չի գտնվել։")
    await workers_repo.unbind(user.id, worker_id)
    return RedirectResponse("/workers", status_code=303)


# -- expenses ----------------------------------------------------------------

def _month_range(raw: str | None) -> tuple[date, date, str]:
    """The month being looked at, defaulting to this one."""
    today = settings.local_day()
    try:
        year, month = (int(part) for part in (raw or "").split("-", 1))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first, last, first.strftime("%Y-%m")


@router.get("/expenses")
async def expenses_page(
    request: Request,
    month: str | None = None,
    category: list[str] = Query(default=[]),
    user: CurrentUser = Depends(current_user),
):
    """Rent, advertising, paying an influencer — money out with no customer.

    Kept off the store pages because most of it belongs to the business rather
    than to any one shop.
    """
    await expenses_repo.ensure_starter_categories(user.id)
    since, until, label = _month_range(month)
    return render(
        request,
        "expenses.html",
        {
            "user": user,
            "active": "expenses",
            "month": label,
            "since": since,
            "until": until,
            "expenses": await expenses_repo.list_between(user.id, since, until),
            "total": await expenses_repo.total_between(user.id, since, until),
            "by_category": await expenses_repo.by_category_between(user.id, since, until),
            "categories": await expenses_repo.categories(user.id),
            "stores": await stores_repo.list_for_owner(user.id),
            "today": settings.local_day(),
            # Everything the business paid out this month, not only what was typed on
            # this page: a wage and a cashier's petty cash are spending too, and this
            # was the page an owner came to for "what did the month cost".
            **await _spending_context(
                user.id, since, until, f"/expenses?month={label}", categories=category
            ),
        },
    )


def _expense_form(
    purpose: str, amount: str, spent_on: str, store_id: str, category_id: str,
    method: str, recurrence: str, note: str,
) -> dict:
    if method not in expenses_repo.METHODS:
        raise AppError("validation_error", "Անհայտ վճարման ձև։")
    if recurrence not in expenses_repo.RECURRENCES:
        raise AppError("validation_error", "Անհայտ պարբերականություն։")
    return {
        "purpose": forms.text(purpose, "Նպատակ", max_length=300),
        "amount": forms.money(amount, "Գումար"),
        "spent_on": forms.day(spent_on, "Ամսաթիվ"),
        "store_id": forms.whole(store_id, "Խանութ", default=0) or None,
        "category_id": forms.whole(category_id, "Կատեգորիա", default=0) or None,
        "method": method,
        "recurrence": recurrence,
        "note": forms.text(note, "Նշում", max_length=1000, required=False),
    }


@router.post("/expenses")
async def create_expense(
    purpose: str = Form(""),
    amount: str = Form(""),
    spent_on: str = Form(""),
    store_id: str = Form(""),
    category_id: str = Form(""),
    method: str = Form("cash"),
    recurrence: str = Form("once"),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    fields = _expense_form(
        purpose, amount, spent_on, store_id, category_id, method, recurrence, note
    )
    await expenses_repo.create(user.id, **fields)
    log.info("expense recorded by user %s: %s", user.id, fields["purpose"][:40])
    return RedirectResponse(f"/expenses?month={fields['spent_on']:%Y-%m}", status_code=303)


@router.post("/expenses/categories")
async def create_expense_category(
    name: str = Form(""), user: CurrentUser = Depends(require_csrf)
):
    """Registered above /expenses/{expense_id}: FastAPI matches in declaration
    order, so the parameterised route would otherwise swallow "categories" and
    fail trying to read it as a number."""
    await expenses_repo.create_category(
        user.id, forms.text(name, "Անվանում", max_length=80)
    )
    return RedirectResponse("/expenses", status_code=303)


@router.post("/expenses/{expense_id}")
async def edit_expense(
    expense_id: int,
    purpose: str = Form(""),
    amount: str = Form(""),
    spent_on: str = Form(""),
    store_id: str = Form(""),
    category_id: str = Form(""),
    method: str = Form("cash"),
    recurrence: str = Form("once"),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    if await expenses_repo.get(user.id, expense_id) is None:
        raise AppError("not_found", "Ծախսը չի գտնվել։")
    fields = _expense_form(
        purpose, amount, spent_on, store_id, category_id, method, recurrence, note
    )
    await expenses_repo.update(user.id, expense_id, **fields)
    return RedirectResponse(f"/expenses?month={fields['spent_on']:%Y-%m}", status_code=303)


@router.post("/expenses/{expense_id}/amount")
async def set_expense_amount(
    expense_id: int,
    amount: str = Form(""),
    back: str | None = None,
    user: CurrentUser = Depends(require_csrf),
):
    """Change only the figure, from the spending list.

    The full form has seven fields and lives on `/expenses`. Nine times out of ten what
    is wrong is the number, and sending somebody to another page to fix a digit is how a
    wrong number stays in the books.
    """
    existing = await expenses_repo.get(user.id, expense_id)
    if existing is None:
        raise AppError("not_found", "Ծախսը չի գտնվել։")
    await expenses_repo.update(
        user.id,
        expense_id,
        purpose=existing["purpose"],
        amount=forms.money(amount, "Գումար"),
        spent_on=existing["spent_on"],
        store_id=existing["store_id"],
        category_id=existing["category_id"],
        method=existing["method"],
        recurrence=existing["recurrence"],
        note=existing["note"],
    )
    return _back_to(back, f"/expenses?month={existing['spent_on']:%Y-%m}")


@router.post("/expenses/{expense_id}/delete")
async def delete_expense(
    expense_id: int, back: str | None = None, user: CurrentUser = Depends(require_csrf)
):
    existing = await expenses_repo.get(user.id, expense_id)
    if existing is None:
        raise AppError("not_found", "Ծախսը չի գտնվել։")
    await expenses_repo.delete(user.id, expense_id)
    return _back_to(back, f"/expenses?month={existing['spent_on']:%Y-%m}")


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
        counts = await till_repo.for_session(store_session_id)
        totals = await money_repo.totals_for_session(store_session_id)
        detail = {
            "session": session,
            "totals": totals,
            "shifts": await sessions_repo.shifts_in_session(store_session_id),
            "receipts": await sales_repo.receipts_in_store_session(store_session_id),
            "ledger": await money_repo.ledger_for_session(store_session_id),
# Named "stock", not "items": `d.items` in a template resolves to
            # dict.items and silently renders nothing.
            "stock": await items_repo.list_for_store(user.id, session["store_id"]),
            "history": await audit_repo.for_session(user.id, store_session_id),
            "write_offs": await write_offs_repo.for_session(store_session_id),
            # Counts a cashier corrected by hand. Shown because a stock number
            # that can be changed silently is one that can hide a shortfall.
            "adjustments": await adjustments_repo.for_session(store_session_id),
            # What was actually in the drawer, against what the books expected. The
            # gap is the figure with no other home.
            "till_counts": counts,
            # The two questions the header could not answer before: what the owner
            # should have received, and what stayed on the premises. From the last
            # count of the evening — a second count replaces the first rather than
            # adding to it, so summing them would double the day.
            #
            # Worked out here rather than read from the count's stored figure, so it
            # follows a sale corrected next week and so rows written under the old
            # rules — before the owner's share was floored at nothing — do not put a
            # negative in the header.
            **_the_owners_share(totals, counts),
            # What the shop actually made, which is not what it took. Same definition
            # as the statistics page — goods sold less what they cost, less wages,
            # petty cash and breakage — so a session's figure and the period that
            # contains it cannot disagree about the word "profit".
            **await _what_the_day_made(session, totals),
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


@router.get("/statistics")
async def statistics_page(
    request: Request,
    period: str | None = None,
    store_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    category: list[str] = Query(default=[]),
    user: CurrentUser = Depends(current_user),
):
    """What the business earned, and what that cost.

    ``store_id`` arrives as a string because the filter's «Բոլորը» option submits
    an empty one. See forms.optional_id.

    ``since``/``until`` are how clicking a bar on the chart works: the link asks for that
    one day, or that one week, by date. They win over ``period`` when present.
    """
    since, until, preset = statistics.range_for(
        period, forms.optional_day(since, "Սկիզբ"), forms.optional_day(until, "Ավարտ")
    )
    store_id = forms.optional_id(store_id)
    if store_id is not None:
        await _store_or_404(user.id, store_id)
    return render(
        request,
        "statistics.html",
        {
            "user": user,
            "active": "statistics",
            "preset": preset,
            "presets": statistics.PRESETS,
            **await statistics.overview(user.id, since, until, store_id),
            **await _spending_context(
                user.id, since, until,
                request.url.path + "?" + str(request.url.query),
                store_id, categories=category,
            ),
        },
    )


# -- corrections -------------------------------------------------------------

def _basket(
    item_ids: list[str],
    quantities: list[str],
    prices: list[str],
    kinds: list[str] | None = None,
) -> list[dict]:
    """Turn parallel form arrays into lines. Blank rows are simply dropped, so
    a form with spare rows on it does not need clearing before submitting.

    A row may name a price list instead of a price: leaving the amount empty and
    choosing «Մեծածախ» takes the item's wholesale price, resolved server-side by
    the same function the bot's write-up uses.
    """
    kinds = kinds or []
    lines = []
    for index, raw_id in enumerate(item_ids):
        if not (raw_id or "").strip():
            continue
        quantity = forms.whole(
            quantities[index] if index < len(quantities) else "", "Քանակ", minimum=1
        )
        price = (
            forms.optional_money(prices[index], "Գին") if index < len(prices) else None
        )
        kind = kinds[index] if index < len(kinds) else "retail"
        if kind not in PRICE_KINDS:
            raise AppError("validation_error", "Անհայտ գնացուցակ։")
        lines.append({
            "item_id": forms.whole(raw_id, "Ապրանք", minimum=1),
            "quantity": quantity,
            "unit_price": price,
            "price_kind": kind,
        })
    if not lines:
        raise AppError("validation_error", "Ապրանք ընտրված չէ։")
    return lines


@router.post("/sales/{sale_id}/void")
async def void_sale(
    sale_id: int, reason: str = Form(""), user: CurrentUser = Depends(require_csrf)
):
    await corrections.void_sale(
        user.id, user.id, sale_id, forms.text(reason, "Պատճառ", max_length=300,
                                              required=False)
    )
    return _back_to_report(await _session_of_sale(user.id, sale_id))


@router.post("/sales/{sale_id}/delete")
async def delete_sale(sale_id: int, user: CurrentUser = Depends(require_csrf)):
    """Remove a receipt from the books, rather than showing it struck through.

    Voiding is the honest correction for a returned sale. This is for a row that
    should never have been there -- a duplicate, a test entry -- where leaving it
    visible forever only makes the report harder to read. It is recorded in the
    history and can be undone.
    """
    store_session_id = await _session_of_sale(user.id, sale_id)
    await corrections.delete_sale(user.id, user.id, sale_id)
    return _back_to_report(store_session_id)


@router.post("/sales/{sale_id}/amend")
async def amend_sale(
    sale_id: int,
    item_id: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    unit_price: list[str] = Form(default=[]),
    price_kind: list[str] = Form(default=[]),
    payment_method: str = Form("cash"),
    reason: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    store_session_id = await _session_of_sale(user.id, sale_id)
    await corrections.amend_sale(
        user.id, user.id, sale_id, _basket(item_id, quantity, unit_price, price_kind),
        payment_method,
        forms.text(reason, "Պատճառ", max_length=300, required=False),
    )
    return _back_to_report(store_session_id)


@router.post("/store-sessions/{store_session_id}/sales")
async def add_sale(
    store_session_id: int,
    worker_id: str = Form(""),
    item_id: list[str] = Form(default=[]),
    quantity: list[str] = Form(default=[]),
    unit_price: list[str] = Form(default=[]),
    price_kind: list[str] = Form(default=[]),
    payment_method: str = Form("cash"),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    await corrections.add_sale(
        user.id, user.id, store_session_id,
        forms.whole(worker_id, "Աշխատող", minimum=1),
        _basket(item_id, quantity, unit_price, price_kind), payment_method,
        forms.text(note, "Նշում", max_length=300, required=False),
    )
    return _back_to_report(store_session_id)


@router.post("/store-sessions/{store_session_id}/movements")
async def add_movement(
    store_session_id: int,
    kind: str = Form("withdrawal"),
    method: str = Form("cash"),
    amount: str = Form(""),
    purpose: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """Money in or out that was not a sale — paying an influencer, say. The
    purpose is required: an amount with no reason is not a record of anything."""
    await corrections.add_movement(
        user.id, user.id, store_session_id, kind, method,
        forms.money(amount, "Գումար"),
        forms.text(purpose, "Նպատակ", max_length=300),
    )
    return _back_to_report(store_session_id)


@router.post("/movements/{movement_id}")
async def set_movement_amount(
    movement_id: int,
    amount: str = Form(""),
    back: str | None = None,
    user: CurrentUser = Depends(require_csrf),
):
    """Change what a withdrawal was for how much.

    A wrong figure here is a wrong figure in the drawer, and the only fix used to be
    deleting the row and typing it again — which loses the reason with it.
    """
    store_session_id = await corrections.set_movement_amount(
        user.id, user.id, movement_id, forms.money(amount, "Գումար")
    )
    return _back_to(back, f"/reports?store_session_id={store_session_id}")


@router.post("/movements/{movement_id}/delete")
async def delete_movement(
    movement_id: int, back: str | None = None, user: CurrentUser = Depends(require_csrf)
):
    store_session_id = await db.fetchval(
        "SELECT store_session_id FROM cash_movements WHERE id = $1 AND owner_id = $2",
        movement_id, user.id,
    )
    await corrections.delete_movement(user.id, user.id, movement_id)
    return _back_to(back, f"/reports?store_session_id={store_session_id}")


@router.post("/shifts/{work_session_id}/salary")
async def set_salary(
    work_session_id: int,
    salary: str = Form(""),
    back: str | None = None,
    user: CurrentUser = Depends(require_csrf),
):
    store_session_id = await db.fetchval(
        "SELECT store_session_id FROM work_sessions WHERE id = $1 AND owner_id = $2",
        work_session_id, user.id,
    )
    await corrections.set_salary(
        user.id, user.id, work_session_id, forms.money(salary, "Աշխատավարձ")
    )
    return _back_to(back, f"/reports?store_session_id={store_session_id}")


@router.post("/shifts/{work_session_id}/bonus")
async def set_bonus(
    work_session_id: int,
    bonus: str = Form(""),
    back: str | None = None,
    user: CurrentUser = Depends(require_csrf),
):
    """Correct a shift's bonus, including down to zero to remove one recorded by
    mistake — which is where deleting the ledger row on its own now sends the owner,
    because doing that alone left the shift claiming a bonus the ledger had no
    record of."""
    store_session_id = await db.fetchval(
        "SELECT store_session_id FROM work_sessions WHERE id = $1 AND owner_id = $2",
        work_session_id, user.id,
    )
    await corrections.set_bonus(
        user.id, user.id, work_session_id, forms.money(bonus, "Բոնուս")
    )
    return _back_to(back, f"/reports?store_session_id={store_session_id}")


@router.post("/history/{event_id}/revert")
async def revert_correction(event_id: int, user: CurrentUser = Depends(require_csrf)):
    """Undo one correction. Newest-first, so rewinding to a moment is undoing
    back to it a step at a time."""
    await corrections.revert(user.id, user.id, event_id)
    return RedirectResponse("/history", status_code=303)


@router.get("/history")
async def history_page(request: Request, user: CurrentUser = Depends(current_user)):
    return render(
        request,
        "history.html",
        {
            "user": user,
            "active": "history",
            "events": await audit_repo.recent(user.id),
            "undoable": await audit_repo.newest_pending(user.id),
            "labels": corrections.ACTION_LABELS,
        },
    )


async def _session_of_sale(owner_id: int, sale_id: int) -> int | None:
    return await db.fetchval(
        "SELECT store_session_id FROM sales WHERE id = $1 AND owner_id = $2",
        sale_id, owner_id,
    )


async def _what_the_day_made(session, totals) -> dict:
    """The two bottom lines for one session, which answer two different questions.

        Շրջանառություն  =  վաճառք − աշխատավարձ − բոնուս − դրամարկղից հանված
        Շահույթ         =  (վաճառք − ինքնարժեք) − աշխատավարձ − բոնուս
                           − դրամարկղից հանված − օրվա ծախսեր

    The first is about **cash**: what the day put into the business, counting the whole
    selling price, because the stock was paid for when it was bought. That is the figure
    to check the drawer against, and it was called «Շահույթ» — which it is not, and
    a shop selling 45,000 of stock that cost 40,000 would read it as a very good day.

    The second is about **the business**: the same day with what the goods actually cost
    taken off, and the owner's own typed expenses for that day with it. It is the figure
    that says whether the shop made money, and the two differ by exactly the margin —
    which is the whole reason both are worth showing.

    Only expenses attached to *this shop* are in it. One left as «Ամբողջ բիզնեսը» —
    rent, advertising — belongs to the business rather than to the branch that happened
    to be open, and counting it here would subtract the same rent from every shop
    trading that day.

    Wages in both are what the till **paid**, not what the shift cost. A report on one
    evening is a report about that evening's money, and a wage the drawer could not
    cover has not left the business yet — so the figure that belongs beside «Ղեկավարին»
    and «Մնաց խանութում», which are drawer questions, is the one that came out of the
    drawer. What is still owed is stated underneath, because both figures above will
    drop by it the day it is settled.
    """
    store_session_id = session["id"]
    wages = await sessions_repo.wages_for_session(store_session_id)
    # Out of the ledger, so they are what the drawer actually paid. Apart, because a
    # bonus is not a wage: an owner reads that tile against the rate they set.
    salary, bonus = Decimal(totals["salaries"]), Decimal(totals["bonuses"])
    # Net of reversals: a voided sale is not a sale, and a day where everything was
    # taken back used to read as a day that sold 7,000 and made a profit on it.
    takings = Decimal(totals["net_sales"])
    taken_out = Decimal(totals["withdrawn"])
    margin = Decimal(await stats_repo.profit_for_session(store_session_id))
    spending = Decimal(
        await expenses_repo.total_for_store_on(
            session["owner_id"],
            session["store_id"],
            settings.local_day(session["opened_at"]),
        )
    )
    return {
        "gross_profit": margin,
        "breakage_cost": Decimal(await write_offs_repo.cost_for_session(store_session_id)),
        "day_spending": spending,
        "wage_salary": salary,
        "wage_bonus": bonus,
        "wage_cost": Decimal(wages["cost"]),
        "wage_unpaid": Decimal(wages["unpaid"]),
        "turnover": takings - salary - bonus - taken_out,
        "day_profit": margin - salary - bonus - taken_out - spending,
    }


def _the_owners_share(totals, counts) -> dict:
    """What the owner should have received, and what stayed on the premises.

    From the last count's two readings, which is what makes the tile and the table agree
    — both of those figures are editable, and a header computed from the live ledger
    instead would sit there contradicting the row the owner had just corrected.

    Floored at nothing, so rows written before that rule cannot put a negative up here:
    the shop that closed on -4,500 has a count saying the owner is owed -7,000, and
    nobody can act on that. ``till_now`` carries the ledger's own figure alongside, so a
    count that has drifted from the books is visible rather than merely wrong.

    **When nobody counted**, both figures used to read zero — which is not what happened
    to the money. A worker who goes home without counting has not emptied the shop; the
    float stays exactly where it is, because nothing changes ``stores.till_balance``
    except a count. So the honest answer is the one the system will actually act on
    tomorrow: the shop keeps the float it carried in this morning, and the owner is owed
    the rest. A shop that took 24,900 on a 400 float is owed 24,500, not nothing.

    It is an inference rather than a declaration — nobody looked in the drawer — so the
    page says which it is, and offers the box to correct it.
    """
    till_now = Decimal(totals["cash"])
    if not counts:
        assumed = Decimal(totals["carried_in"])
        return {
            "owed_to_owner": max(Decimal(0), till_now - assumed),
            "left_in_store": assumed,
            "count_expected": till_now,
            "till_now": till_now,
            "count_is_stale": False,
            "nobody_counted": True,
        }
    last = counts[-1]
    left, was = Decimal(last["counted"]), Decimal(last["expected"])
    return {
        # From the books as they stand, not from the reading frozen on the count:
        #
        #     float carried in + cash sales − wages paid − cash taken out − left behind
        #
        # which is the drawer, minus what stays in the shop. Taking it from the count
        # instead meant a sale the owner added afterwards never reached the figure —
        # a shop whose drawer really held 2,000 was told the owner was owed nothing,
        # because the count had been made when only the 234 float was in it.
        "owed_to_owner": max(Decimal(0), till_now - left),
        "left_in_store": left,
        # The count's own reading, kept beside the ledger's: the two disagreeing is
        # worth saying out loud, and it is the row the owner edits to fix it.
        "count_expected": was,
        "till_now": till_now,
        "count_is_stale": was != till_now,
        "nobody_counted": False,
    }


def _filtered_by(page: str, labels: Sequence[str]) -> str:
    """``page`` with its category filter replaced by ``labels``.

    Built from the page's own URL rather than assembled from parts, so a link out of the
    ring keeps the month, the period and the shop the owner is already looking at. The
    filter is repeated rather than joined — a category name is free text and can hold
    whichever separator we would have chosen.
    """
    parts = urlsplit(page)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "category"
    ]
    query += [("category", label) for label in labels]
    return urlunsplit(("", "", parts.path, urlencode(query), ""))


async def _spending_context(
    owner_id: int,
    since: date,
    until: date,
    page: str,
    store_id: int | None = None,
    categories: Sequence[str] | None = None,
) -> dict:
    """Every payment of a period, for whichever page is showing them.

    ``page`` is the URL this is being rendered on. It is where an edit or a delete
    returns to — the same partial serves the statistics page and the expenses page, and
    a correction made on one should not land on the other — and it is the base every
    slice of the ring links to.

    ``categories`` is the slice the owner clicked. It narrows the list and the figure
    under it, and nothing else: the ring keeps every category so there is something to
    click back to, and the headline stays the period's own total so a filter cannot be
    mistaken for a shrinking month.
    """
    chosen = list(categories or [])
    here = _filtered_by(page, chosen)
    rows = await spending_repo.list_between(
        owner_id, since, until, store_id, categories=chosen or None
    )
    donut = charts.donut(
        await spending_repo.by_category_between(owner_id, since, until, store_id)
    )
    for segment in donut["segments"]:
        # A slice already showing goes back to the whole rather than nowhere: clicking
        # it a second time is how anybody expects to undo the first click.
        picked = set(segment["members"]) == set(chosen)
        segment["href"] = _filtered_by(page, [] if picked else segment["members"])
        segment["selected"] = picked
    return {
        "payments": rows,
        # Part-to-whole across all three doors, not only the typed expenses: a wage is a
        # category of spending in exactly the way rent is, and splitting the ring by
        # where a payment was entered would put most of a month in one nameless lump.
        "donut": donut,
        # Over the whole period, not over the rows above: the list is capped and the
        # money is not, and a total that quietly stopped at the five-hundredth payment
        # would be wrong in the one direction nobody checks.
        "payment_totals": await spending_repo.totals_between(
            owner_id, since, until, store_id
        ),
        "payment_count": await spending_repo.count_between(
            owner_id, since, until, store_id
        ),
        # What the list below is actually showing, which is the same thing whenever
        # nothing is filtered.
        "chosen": chosen,
        "chosen_total": (
            await spending_repo.totals_between(
                owner_id, since, until, store_id, categories=chosen
            )
        )["total"] if chosen else None,
        "chosen_count": await spending_repo.count_between(
            owner_id, since, until, store_id, categories=chosen
        ) if chosen else None,
        "everything_href": _filtered_by(page, []),
        "editable": spending_repo.EDITABLE,
        "deletable": spending_repo.DELETABLE,
        "back": here,
    }


def _back_to(where: str | None, fallback: str):
    """Return to the page the form was on, when it said which.

    Only a path of our own: an open redirect is a phishing tool, and the only thing a
    caller legitimately needs is "the page I was looking at".

    Rejecting a leading ``//`` is not enough on its own. Browsers following the URL
    spec treat a backslash in this position as a forward slash, so ``/\\evil.example``
    normalises to ``//evil.example`` — a protocol-relative URL, off this site
    entirely — after passing a check that only looked for two slashes. Anything with
    a backslash or a control character in it is refused outright rather than
    sanitised: there is no legitimate one in a path this application generates, so
    there is nothing to lose by declining the whole class.
    """
    if (
        where
        and where.startswith("/")
        and not where.startswith("//")
        and "\\" not in where
        and not any(ch < " " or ch == "\x7f" for ch in where)
    ):
        return RedirectResponse(where, status_code=303)
    return RedirectResponse(fallback, status_code=303)


def _back_to_report(store_session_id: int | None):
    target = f"/reports?store_session_id={store_session_id}" if store_session_id else "/reports"
    return RedirectResponse(target, status_code=303)


@router.post("/stores/{store_id}/till-balance")
async def set_till_balance(
    request: Request,
    store_id: int,
    amount: str = Form(""),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """Correct a shop's float.

    The balance is a real quantity somebody can be wrong about — a count typed with
    an extra nought, a drawer topped up in person, a shop that existed before anybody
    counted anything — and only the owner can settle it. Recorded as their correction
    rather than silently overwritten, so the history says who moved it.
    """
    await _store_or_404(user.id, store_id)
    await till_service.set_by_owner(
        user.id,
        store_id,
        forms.money(amount, "Գումար"),
        forms.text(note, "Նշում", max_length=300, required=False),
    )
    return render(
        request, "_store_status.html", await _status_context(user.id, store_id)
    )


@router.post("/till-counts/{count_id}/counted")
async def correct_a_till_count(
    count_id: int,
    counted: str = Form(""),
    expected: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """Change what a count says: what was left in the shop, and what the till held.

    Both are readings rather than claims about what happened to any money — one off a
    drawer at the door, one off the books at the same moment — so both are edited in
    place. The owner's share is the difference, so it follows either.

    ``expected`` may be negative: the till genuinely could hold less than nothing under
    the old rules, and a row saying so is a record of that evening.
    """
    row = await till_service.correct_a_count(
        user.id,
        count_id,
        forms.money(counted, "Մնացորդ"),
        # Optional, so a page cached from before this field existed still saves the
        # count it does know about instead of failing on a field it cannot send.
        forms.money(expected, "Դրամարկղում էր", allow_negative=True)
        if expected.strip()
        else None,
    )
    return _back_to_report(row["store_session_id"])


@router.post("/till-counts/{count_id}/delete")
async def delete_a_till_count(
    count_id: int, user: CurrentUser = Depends(require_csrf)
):
    """Remove a count from the record.

    Not a correction of a figure but a statement that the reading should never have
    been there: a duplicate, a count against the wrong shop, a row left behind by a
    rule that has since changed. The shop's float falls back to whatever count is now
    the latest.
    """
    row = await till_service.delete_count(user.id, user.id, count_id)
    return _back_to_report(row["store_session_id"])


@router.get("/transfers")
async def transfers_page(request: Request, user: CurrentUser = Depends(current_user)):
    """Move stock between shops, and the history of it having been moved."""
    return render(
        request,
        "transfers.html",
        {
            "user": user,
            "active": "transfers",
            "stores": await stores_repo.list_for_owner(user.id),
            "transfers": await transfers_repo.recent_for_owner(user.id),
        },
    )


@router.post("/transfers")
async def create_transfer(
    from_store_id: str = Form(""),
    to_store_id: str = Form(""),
    item_id: str = Form(""),
    quantity: str = Form(""),
    note: str = Form(""),
    user: CurrentUser = Depends(require_csrf),
):
    """The owner's own transfer. Applies immediately — there is nobody to ask."""
    await transfers_service.move_as_owner(
        owner_id=user.id,
        item_id=forms.whole(item_id, "Ապրանք", minimum=1),
        from_store_id=forms.whole(from_store_id, "Որտեղից", minimum=1),
        to_store_id=forms.whole(to_store_id, "Ուր", minimum=1),
        quantity=forms.whole(quantity, "Քանակ", minimum=1),
        note=forms.text(note, "Նշում", max_length=300, required=False),
    )
    return RedirectResponse("/transfers", status_code=303)


@router.post("/reports/{store_session_id}/delete")
async def delete_report(
    store_session_id: int, user: CurrentUser = Depends(require_csrf)
):
    """Erase one shift's report: its sales, its ledger and its breakage.

    Stock is deliberately left alone. Deleting a report says "this record should
    not exist", which is not the same as "these goods came back" — putting the
    counts up would invent stock the shop does not have on its shelves. Reversing
    a sale is what the void button on each receipt is for, and it is still there.

    Only a closed session. Deleting one that is still running would take the
    ground out from under the workers standing in it: their open shifts, and every
    sale they are about to make, point at this row.
    """
    session = await sessions_repo.get_store_session(user.id, store_session_id)
    if session is None:
        raise AppError("not_found", "Հերթափոխը չի գտնվել։")
    if session["closed_at"] is None:
        raise AppError(
            "validation_error",
            "Բաց հերթափոխը հնարավոր չէ ջնջել։ Սկզբում փակեք խանութը։",
        )

    async with db.transaction() as conn:
        await write_offs_repo.delete_for_session(conn, user.id, store_session_id)
        await adjustments_repo.delete_for_session(conn, user.id, store_session_id)
        await till_repo.delete_for_session(conn, user.id, store_session_id)
        await sessions_repo.delete_store_session(conn, user.id, store_session_id)
    log.info("owner %s deleted store session %s", user.id, store_session_id)
    return RedirectResponse("/reports", status_code=303)


@router.post("/write-offs/{write_off_id}/delete")
async def delete_write_off(
    write_off_id: int, back: str | None = None,
    user: CurrentUser = Depends(require_csrf),
):
    """Remove a write-off and put the stock back.

    Unlike deleting a report, this *does* restore the count, because the two
    deletions mean different things. A write-off is a claim that goods were
    destroyed; deleting it says the claim was wrong, and if the vape did not break
    then it is still on the shelf. Leaving the count down would keep the error and
    lose the record of it.

    Nothing to undo on the money side — breakage never touched the till.
    """
    write_off = await write_offs_repo.get(user.id, write_off_id)
    if write_off is None:
        raise AppError("not_found", "Խոտանի գրառումը չի գտնվել։")

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE items SET count = count + $2, updated_at = now() WHERE id = $1",
            write_off["item_id"],
            write_off["quantity"],
        )
        await write_offs_repo.delete(conn, user.id, write_off_id)
    log.info(
        "owner %s deleted write-off %s and restored %s of item %s",
        user.id, write_off_id, write_off["quantity"], write_off["item_id"],
    )
    return _back_to(
        back, f"/reports?store_session_id={write_off['store_session_id']}"
    )


@router.post("/workers/{worker_id}")
async def edit_worker(
    worker_id: int,
    name: str = Form(""),
    telegram_username: str = Form(""),
    salary_amount: str = Form(""),
    salary_period: str = Form("shift"),
    is_active: str = Form(""),
    bonus_threshold: str = Form(""),
    bonus_amount: str = Form(""),
    bonus_period: str = Form("day"),
    user: CurrentUser = Depends(require_csrf),
):
    """Salary and its period are editable at any time, as are the name and handle."""
    if await workers_repo.get(user.id, worker_id) is None:
        raise AppError("not_found", "Աշխատողը չի գտնվել։")
    try:
        await workers_repo.update(
            owner_id=user.id,
            worker_id=worker_id,
            name=forms.text(name, "Անուն", max_length=120, required=False),
            telegram_username=_username(telegram_username),
            salary_amount=forms.money(salary_amount, "Աշխատավարձ"),
            salary_period=_salary_period(salary_period),
            is_active=is_active in {"1", "on", "true"},
            bonus=_bonus(bonus_threshold, bonus_amount, bonus_period),
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise AppError("validation_error", _USERNAME_TAKEN) from None
    return RedirectResponse("/workers", status_code=303)
