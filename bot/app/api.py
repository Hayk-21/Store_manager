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
                "idempotency_key": key,
            },
        )

    async def close_out(
        self,
        telegram_id: int,
        lines: list[dict],
        key: str,
        close_store: bool = False,
    ) -> dict:
        """End the shift and declare the day's sales in one call.

        One call rather than a sale per line then an end: the server applies the
        whole thing in a single transaction, so there is no state where the stock
        moved but the shift is still open.
        """
        return await self._call(
            "POST",
            "/shift/close-out",
            json={
                "telegram_id": telegram_id,
                "lines": lines,
                "idempotency_key": key,
                "close_store": close_store,
            },
        )

    async def void_last(self, telegram_id: int, reason: str | None = None) -> dict:
        payload: dict[str, Any] = {"telegram_id": telegram_id}
        if reason:
            payload["reason"] = reason
        return await self._call("POST", "/sale/void", json=payload)

    async def end_shift(
        self, telegram_id: int, key: str, lat: float | None = None, lng: float | None = None
    ) -> dict:
        payload: dict[str, Any] = {"telegram_id": telegram_id, "idempotency_key": key}
        if lat is not None and lng is not None:
            payload["lat"], payload["lng"] = lat, lng
        return await self._call("POST", "/shift/end", json=payload)

    async def close_store(self, telegram_id: int, key: str) -> dict:
        return await self._call(
            "POST", "/store/close",
            json={"telegram_id": telegram_id, "idempotency_key": key},
        )


api = Api()
