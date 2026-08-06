"""Sending a message to a Telegram chat, straight from the web service.

The web service talks to Telegram's API itself rather than asking the bot
service to relay. It is one HTTP POST, and routing it through the bot would add
a hop that can be down while the web service is perfectly healthy — which is the
worst possible time to be unable to deliver a login code.

Without ``TELEGRAM_BOT_TOKEN`` set, codes are written to the log instead. That
makes local development work with no bot at all; it is obviously not a
production configuration, and the log says so.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("storemanager.telegram")

API = "https://api.telegram.org"
TIMEOUT_S = 10.0


class Undeliverable(Exception):
    """Telegram refused the message, and retrying will not change that."""

    def __init__(self, reason: str, *, blocked: bool = False) -> None:
        self.reason = reason
        # True when the chat itself is unusable: the admin has not started the
        # bot, or has blocked it. The fix is theirs, not ours.
        self.blocked = blocked
        super().__init__(reason)


async def send_message(chat_id: int, text: str) -> None:
    """Deliver one message. Raises Undeliverable if Telegram says no."""
    if not settings.telegram_bot_token:
        log.warning(
            "TELEGRAM_BOT_TOKEN is not set; the message for chat %s was not sent. "
            "Its text was: %s",
            chat_id,
            text,
        )
        return

    url = f"{API}/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
    except httpx.HTTPError as exc:
        log.warning("could not reach Telegram: %s", exc)
        raise Undeliverable("telegram unreachable") from exc

    if response.status_code == 200 and response.json().get("ok"):
        return

    body = response.json() if response.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    description = str(body.get("description", response.text))[:200]

    # 403 is "the user blocked the bot" or "the bot can't initiate"; 400 with
    # "chat not found" is "they never pressed /start". Both need the person to
    # go and message the bot, so they are reported as the same thing.
    blocked = response.status_code == 403 or "chat not found" in description.lower()
    log.warning("Telegram refused a message to %s: %s", chat_id, description)
    raise Undeliverable(description, blocked=blocked)
