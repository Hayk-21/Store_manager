"""The bot's config is read at import time, so the environment comes first."""

from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("API_BASE_URL", "http://web.test/api/bot/v1")
os.environ.setdefault("BOT_SHARED_SECRET", "test-bot-secret")
os.environ.setdefault("HTTP_RETRIES", "3")
os.environ.setdefault("LOG_LEVEL", "WARNING")
