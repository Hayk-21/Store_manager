"""The one place in the project that configures logging."""

from __future__ import annotations

import logging

from app.config import settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # These two are chatty at INFO and say nothing we do not already log.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    # httpx logs the full request URL at INFO, and Telegram puts the bot token
    # *in the path* — so leaving this on writes the token into the deploy log of
    # every login code sent. Nothing here is worth that.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _configured = True
