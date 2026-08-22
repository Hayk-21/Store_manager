"""The HTTP client for the web service.

Two things here are load-bearing:

* **Retries never change the idempotency key.** One user action gets one key,
  reused for every attempt, so a retry after a timeout resolves to the original
  receipt instead of selling twice.
* **4xx is never retried.** "You are out of range" will not become true by
  asking again; only 5xx and transport failures are worth another attempt.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from app.config import settings
from app.texts import NETWORK_TROUBLE, UNEXPECTED

log = logging.getLogger("storemanager.bot.api")


class ApiError(Exception):
    """A refusal from the server, already carrying a printable Armenian sentence."""

    def __init__(self, code: str, message: str, details: dict | None = None,
                 status: int = 400) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status
        super().__init__(f"{code}: {message}")

    def human(self) -> str:
        """What to show the worker. The server owns this wording."""
        return self.message or UNEXPECTED


class ApiUnavailable(Exception):
    """The server could not be reached at all, after retries."""

    def human(self) -> str:
        return NETWORK_TROUBLE


def new_idempotency_key() -> str:
    """One per user action — minted when the action starts, not per attempt."""
    return uuid.uuid4().hex


class Api:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.http_timeout_s,
            headers={"X-Bot-Secret": settings.bot_shared_secret},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        last_error: Exception | None = None

        for attempt in range(1, settings.http_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("%s %s failed (attempt %d): %s", method, path, attempt, exc)
                await asyncio.sleep(0.4 * attempt)
                continue

            if response.status_code >= 500:
                # Worth another try: the request itself was fine.
                last_error = httpx.HTTPError(f"server returned {response.status_code}")
                log.warning("%s %s -> %d (attempt %d)", method, path,
                            response.status_code, attempt)
                await asyncio.sleep(0.4 * attempt)
                continue

            try:
                body = response.json()
            except ValueError:
                # A non-JSON body from a 2xx/4xx is a bug on our side, not a
                # network blip; retrying would not help.
                log.error("%s %s returned non-JSON: %r", method, path, response.text[:200])
                raise ApiError("internal", UNEXPECTED, status=response.status_code) from None

            if response.status_code < 400 and body.get("ok"):
                return body

            error = body.get("error") or {}
            raise ApiError(
                code=error.get("code", "internal"),
                message=error.get("message") or UNEXPECTED,
                details=error.get("details"),
                status=response.status_code,
            )

        log.error("%s %s gave up after %d attempts", method, path, settings.http_retries)
        raise ApiUnavailable(str(last_error))

    # -- endpoints ----------------------------------------------------------

    async def me(
        self,
        telegram_id: int,
        telegram_name: str | None = None,
        telegram_username: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"telegram_id": telegram_id}
        if telegram_name:
            params["telegram_name"] = telegram_name
        if telegram_username:
            params["telegram_username"] = telegram_username
        return await self._call("GET", "/me", params=params)

    async def open_store(
        self,
        telegram_id: int,
        lat: float,
        lng: float,
        accuracy_m: float | None,
        key: str,
        telegram_name: str | None = None,
        telegram_username: str | None = None,
        live_period: int | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "telegram_id": telegram_id, "lat": lat, "lng": lng, "idempotency_key": key
        }
        if accuracy_m is not None:
            payload["accuracy_m"] = accuracy_m
        # How long the device agreed to keep streaming. The server refuses to
        # open a shift without it: it is the only field that distinguishes a real
        # position from a pin dropped on the map.
        if live_period is not None:
            payload["live_period"] = live_period
        if telegram_name:
            payload["telegram_name"] = telegram_name
        # The owner registered a @username; this is what binds it to an account.
        if telegram_username:
            payload["telegram_username"] = telegram_username
        return await self._call("POST", "/store/open", json=payload)

    async def ping(
        self,
        telegram_id: int,
        lat: float,
        lng: float,
        accuracy_m: float | None = None,
    ) -> dict:
        """One reading from a running live location.

        No idempotency key: a repeated position costs a duplicate row and
        nothing else, and this is called often enough that the cheapest possible
        request is the right one.
        """
        payload: dict[str, Any] = {"telegram_id": telegram_id, "lat": lat, "lng": lng}
        if accuracy_m is not None:
            payload["accuracy_m"] = accuracy_m
        return await self._call("POST", "/location/ping", json=payload)

    async def search_items(self, telegram_id: int, query: str, limit: int = 8) -> dict:
        return await self._call(
            "GET", "/items", params={"telegram_id": telegram_id, "q": query, "limit": limit}
        )

    async def sell(
        self,
        telegram_id: int,
        item_id: int,
        quantity: int,
        payment_method: str,
        key: str,
        unit_price: str | None = None,
        price_kind: str = "retail",
        is_delivery: bool = False,
    ) -> dict:
        line: dict[str, Any] = {"item_id": item_id, "quantity": quantity,
                                "price_kind": price_kind}
        # A decimal string, never a float: the server refuses floats outright
        # because they have already lost digits by the time it sees them.
        if unit_price is not None:
            line["unit_price"] = unit_price
        return await self._call(
            "POST",
            "/sale",
            json={
                "telegram_id": telegram_id,
                "items": [line],
                "payment_method": payment_method,
                "is_delivery": is_delivery,
                "idempotency_key": key,
            },
        )

    async def close_out(
        self, telegram_id: int, lines: list[dict], key: str, counted: str | None = None
    ) -> dict:
        """End the shift and declare the day's sales in one call.

        One call rather than a sale per line then an end: the server applies the
        whole thing in a single transaction, so there is no state where the stock
        moved but the shift is still open.

        It never asks for the shop to be closed. Closing it is not a decision a
        cashier makes — the last one to leave closes it, which the server works
        out for itself.

        ``counted`` is what is being left in the drawer, and it is left out of the
        first attempt on purpose: only the server knows whether this close shuts the
        shop, so it refuses with ``till_count_required`` when it does and the same
        call is sent again with the figure. Sent as a decimal string, never a float.
        """
        payload: dict[str, Any] = {
            "telegram_id": telegram_id,
            "lines": lines,
            "idempotency_key": key,
        }
        if counted is not None:
            payload["counted"] = counted
        return await self._call("POST", "/shift/close-out", json=payload)

    async def void_last(
        self, telegram_id: int, reason: str | None = None, sale_id: int | None = None
    ) -> dict:
        """Reverse a receipt. Without ``sale_id`` it is the most recent one.

        The undo button under a confirmation names its own sale, so tapping it
        three sales later still reverses the right one.
        """
        payload: dict[str, Any] = {"telegram_id": telegram_id}
        if reason:
            payload["reason"] = reason
        if sale_id is not None:
            payload["sale_id"] = sale_id
        return await self._call("POST", "/sale/void", json=payload)

    async def add_item(
        self,
        telegram_id: int,
        name: str,
        count: int,
        self_price: str,
        sell_price: str,
        wholesale_price: str | None = None,
    ) -> dict:
        """Put a new product on the shelf of the store this shift belongs to.

        ``wholesale_price`` is omitted rather than sent as "0" when the item is
        not sold wholesale: a zero there would read as free.
        """
        payload: dict[str, Any] = {
            "telegram_id": telegram_id,
            "name": name,
            "count": count,
            "self_price": self_price,
            "sell_price": sell_price,
        }
        if wholesale_price is not None:
            payload["wholesale_price"] = wholesale_price
        return await self._call("POST", "/items", json=payload)

    async def count_till(self, telegram_id: int, counted: str, key: str) -> dict:
        """What the worker is leaving in the shop's drawer.

        Amount as a decimal string, never a float. The server makes it the store's
        float and books the rest out as handed to the owner.
        """
        return await self._call(
            "POST",
            "/shift/till",
            json={
                "telegram_id": telegram_id,
                "counted": counted,
                "idempotency_key": key,
            },
        )

    async def review_shift(self, telegram_id: int) -> dict:
        """The whole open shift: sales, breakage, corrections, cash, the drawer."""
        return await self._call(
            "GET", "/shift/review", params={"telegram_id": telegram_id}
        )

    async def transfer_sources(self, telegram_id: int) -> dict:
        """The owner's other shops — the ones a box could be asked for from."""
        return await self._call(
            "GET", "/transfers/stores", params={"telegram_id": telegram_id}
        )

    async def transfer_items(
        self, telegram_id: int, store_id: int, q: str = "", limit: int = 25
    ) -> dict:
        """What another shop has on its shelf. Names and counts, no prices."""
        return await self._call(
            "GET",
            "/transfers/items",
            params={"telegram_id": telegram_id, "store_id": store_id,
                    "q": q, "limit": limit},
        )

    async def request_transfer(
        self, telegram_id: int, from_store_id: int, item_id: int, quantity: int, key: str
    ) -> dict:
        """Ask for stock. Nothing moves until the other shop agrees."""
        return await self._call(
            "POST",
            "/transfers",
            json={
                "telegram_id": telegram_id,
                "from_store_id": from_store_id,
                "item_id": item_id,
                "quantity": quantity,
                "idempotency_key": key,
            },
        )

    async def pending_transfers(self, telegram_id: int) -> dict:
        return await self._call(
            "GET", "/transfers/pending", params={"telegram_id": telegram_id}
        )

    async def decide_transfer(
        self, telegram_id: int, transfer_id: int, approve: bool
    ) -> dict:
        """Approve or reject. No idempotency key: the row's own status is the
        guard — a second tap finds it already answered and is told so."""
        return await self._call(
            "POST",
            f"/transfers/{transfer_id}/decide",
            json={"telegram_id": telegram_id, "approve": approve},
        )

    async def adjust_stock(
        self, telegram_id: int, lines: list[dict], key: str, note: str | None = None
    ) -> dict:
        """Correct several counts at once — one tap of "confirm", one request.

        A request per product would let half a correction land, and the half that
        failed is invisible: the screen would show numbers the cashier believes
        they already fixed.
        """
        payload: dict[str, Any] = {
            "telegram_id": telegram_id,
            "lines": lines,
            "idempotency_key": key,
        }
        if note:
            payload["note"] = note
        return await self._call("POST", "/items/adjust", json=payload)

    async def undo_adjust_stock(self, telegram_id: int, external_id: str, key: str) -> dict:
        """Take back the batch of corrections ``external_id`` identifies.

        The batch, because the cashier counted the shelf once and confirmed once —
        that tap is the thing being undone. Its own key on top, because the undo is
        an action in its own right and a retry must not reverse twice.
        """
        return await self._call(
            "POST",
            "/items/adjust/undo",
            json={
                "telegram_id": telegram_id,
                "external_id": external_id,
                "idempotency_key": key,
            },
        )

    async def withdraw(
        self, telegram_id: int, amount: str, purpose: str, reason: str, key: str
    ) -> dict:
        """Cash out of the till. Amount as a decimal string, never a float.

        Both the code and the text: ``reason`` is what the server decides the
        ceiling from, and ``purpose`` is what an older service — which knows
        nothing about codes — would write on the row.
        """
        return await self._call(
            "POST",
            "/cash/withdraw",
            json={
                "telegram_id": telegram_id,
                "amount": amount,
                "purpose": purpose,
                "reason": reason,
                "idempotency_key": key,
            },
        )

    async def money_transfer_stores(self, telegram_id: int) -> dict:
        """The owner's other shops that are open right now, and what this till holds.

        Only open ones: money goes to a person. The available figure comes back with
        them so the question about the amount can say what there is to send.
        """
        return await self._call(
            "GET", "/cash/transfers/stores", params={"telegram_id": telegram_id}
        )

    async def send_money(
        self, telegram_id: int, to_store_id: int, amount: str, key: str
    ) -> dict:
        """Cash out of this till and on its way to another shop. Amount as a
        decimal string, never a float."""
        return await self._call(
            "POST",
            "/cash/transfers",
            json={
                "telegram_id": telegram_id,
                "to_store_id": to_store_id,
                "amount": amount,
                "idempotency_key": key,
            },
        )

    async def pending_money_transfers(self, telegram_id: int) -> dict:
        return await self._call(
            "GET", "/cash/transfers/pending", params={"telegram_id": telegram_id}
        )

    async def decide_money_transfer(
        self, telegram_id: int, transfer_id: int, accept: bool
    ) -> dict:
        """Confirm or deny an envelope. No idempotency key: the row's own status is
        the guard — a second tap finds it already answered and is told so."""
        return await self._call(
            "POST",
            f"/cash/transfers/{transfer_id}/decide",
            json={"telegram_id": telegram_id, "accept": accept},
        )

    async def write_off(
        self,
        telegram_id: int,
        item_id: int,
        quantity: int,
        key: str,
        reason: str | None = None,
    ) -> dict:
        """Damaged or expired stock, off the shelf without a sale."""
        payload: dict[str, Any] = {
            "telegram_id": telegram_id,
            "item_id": item_id,
            "quantity": quantity,
            "idempotency_key": key,
        }
        if reason:
            payload["reason"] = reason
        return await self._call("POST", "/write-off", json=payload)

    async def end_shift(
        self, telegram_id: int, key: str, lat: float | None = None, lng: float | None = None
    ) -> dict:
        payload: dict[str, Any] = {"telegram_id": telegram_id, "idempotency_key": key}
        if lat is not None and lng is not None:
            payload["lat"], payload["lng"] = lat, lng
        return await self._call("POST", "/shift/end", json=payload)



api = Api()
