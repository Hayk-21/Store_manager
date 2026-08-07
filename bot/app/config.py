"""Three secrets and two timeouts. That is the whole configuration.

No store list, no radius, no coordinates, no store names. The server geofences,
so there is nothing here to keep in sync with the website — which is the point.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """A required environment variable is missing."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(or set it in the Railway service variables)."
        )
    return value


class Settings:
    def __init__(self) -> None:
        self.bot_token = _required("BOT_TOKEN")
        # e.g. http://web.railway.internal:8080/api/bot/v1 on Railway's private
        # network, or the public https URL.
        self.api_base_url = _required("API_BASE_URL").rstrip("/")
        self.bot_shared_secret = _required("BOT_SHARED_SECRET")
        self.http_timeout_s = float(os.getenv("HTTP_TIMEOUT_S", "10"))
        self.http_retries = int(os.getenv("HTTP_RETRIES", "3"))
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        # Off by default: it forces the worker to share a *live* location from
        # the attachment menu rather than tapping the button, which is the only
        # thing Telegram offers that cannot be aimed at a chosen point.


settings = Settings()
