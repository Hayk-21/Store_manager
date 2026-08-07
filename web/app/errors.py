"""The error types the whole application raises, and their HTTP mapping.

Two kinds:

``AppError``  — something the owner did on the website.
``BotError``  — something a worker did through the Telegram bot. Carries a stable
                machine code plus a display-ready Armenian sentence, because the
                bot prints ``message`` verbatim. Keeping the wording on this side
                means it can never drift between the two services.
"""

from __future__ import annotations

from typing import Any

from app.texts import ERROR_MESSAGES

# Machine code -> HTTP status. Anything not listed is a 400.
STATUS_BY_CODE: dict[str, int] = {
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "validation_error": 422,
    "internal": 500,
    # bot-facing
    "unknown_worker": 404,
    "worker_inactive": 403,
    "no_store_in_range": 422,
    "no_stores_located": 422,
    "location_too_vague": 422,
    "location_not_live": 422,
    "session_already_open": 409,
    "no_open_session": 409,
    "store_not_open": 409,
    "unknown_item": 404,
    "insufficient_stock": 409,
    "nothing_to_void": 409,
    "empty_basket": 422,
}


class AppError(Exception):
    """A request the owner made that cannot be honoured."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, ERROR_MESSAGES["internal"])
        self.details = details or {}
        self.status = status or STATUS_BY_CODE.get(code, 400)
        super().__init__(f"{code}: {self.message}")

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"ok": False, "error": body}


class BotError(AppError):
    """A request the Telegram bot made that cannot be honoured.

    Identical shape to AppError; the separate type exists so route handlers can
    tell "the worker did something impossible" from "the owner did", and log them
    differently.
    """
