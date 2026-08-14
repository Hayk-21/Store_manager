"""HTMX fragments.

Each of these renders one of the ``_``-prefixed templates and nothing else, so a
polled refresh costs a fragment rather than a page.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app import forms
from app.deps import CurrentUser, current_user
from app.errors import AppError
from app.repo import items as items_repo
from app.repo import money as money_repo
from app.repo import stores as stores_repo
from app.routes.pages import (
    _items_context,
    _spending_context,
    _status_context,
    _store_or_404,
)
from app.templating import render

router = APIRouter(prefix="/partials")


@router.get("/footer")
async def footer(request: Request, user: CurrentUser = Depends(current_user)):
    """Requirement 4: cash and card for every store, refreshed every 10 seconds."""
    return render(
        request, "_footer.html", {"stores": await money_repo.totals_by_store(user.id)}
    )


@router.get("/stores/{store_id}/status")
async def store_status(
    request: Request, store_id: int, user: CurrentUser = Depends(current_user)
):
    await _store_or_404(user.id, store_id)
    return render(request, "_store_status.html", await _status_context(user.id, store_id))


@router.get("/spending")
async def spending(
    request: Request,
    since: str,
    until: str,
    page: str,
    store_id: str = "",
    category: list[str] = Query(default=[]),
    user: CurrentUser = Depends(current_user),
):
    """The ring and the payments under it, for one period and one filter.

    What clicking a slice asks for. It used to ask for the whole page: thirteen
    statistics queries, half a megabyte of HTML and a jump back to the top, to
    change which rows a list further down was showing. This asks only the four
    questions that actually change.

    ``page`` is where the fragment is being shown, and it is the base for every
    link and every edit form inside it — so a correction made from the statistics
    page returns there rather than to /expenses. It arrives from the browser, so
    it is held to the same rule as any other redirect target: a path of ours, and
    nothing that a browser could read as another host.
    """
    if not page.startswith("/") or page.startswith("//") or "\\" in page:
        raise AppError("validation_error", "Անվավեր հասցե։")
    return render(
        request,
        "_spending_panel.html",
        {
            # Every row in here carries an edit and a delete, and each of those is a
            # form that needs the token. A fragment rendered without it renders forms
            # that are refused on submit.
            "user": user,
            **await _spending_context(
                user.id,
                forms.day(since, "Սկիզբ"),
                forms.day(until, "Ավարտ"),
                page,
                forms.optional_id(store_id),
                categories=category,
            ),
        },
    )


@router.get("/store-items")
async def store_items_options(
    request: Request,
    from_store_id: str = "",
    user: CurrentUser = Depends(current_user),
):
    """The ``<option>`` list for one shop's stock, for the transfer form.

    Its own fragment rather than data baked into the page: an item belongs to one
    shop, so a single list of everything would offer boxes that are not on the
    shelf being moved from.

    A blank or unknown shop renders the prompt rather than an error — the select
    starts empty, and its first change event is what asks for this.
    """
    store_id = forms.optional_id(from_store_id)
    items = []
    if store_id is not None and await stores_repo.get(user.id, store_id) is not None:
        items = await items_repo.list_for_store(user.id, store_id)
    return render(request, "_item_options.html", {"items": items})


@router.get("/stores/{store_id}/items")
async def store_items(
    request: Request,
    store_id: int,
    sort: str = items_repo.DEFAULT_SORT,
    desc: bool = False,
    user: CurrentUser = Depends(current_user),
):
    await _store_or_404(user.id, store_id)
    return render(request, "_items_table.html", await _items_context(user.id, store_id, sort, desc))
