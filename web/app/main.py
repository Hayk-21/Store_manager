"""The FastAPI application: lifespan, exception handling, router mounts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import db
from app.deps import NotLoggedIn, login_redirect
from app.errors import AppError, BotError
from app.logging_conf import setup_logging
from app.repo import users as users_repo
from app.routes import auth, bot_api, pages, partials
from app.services import shifts as shifts_service
from app.templating import render

log = logging.getLogger("vapestore.main")

STATIC_DIR = Path(__file__).parent / "static"

BOT_API_PREFIX = "/api/bot/v1"


HOUSEKEEPING_INTERVAL_S = 3600


async def _housekeeping() -> None:
    """Hourly: close stores somebody forgot about, and drop dead sessions.

    Auto-close matters more than it looks. ``one_open_session_per_worker`` means
    a worker who went home without pressing "close" could not start tomorrow.
    """
    while True:
        try:
            await asyncio.sleep(HOUSEKEEPING_INTERVAL_S)
            closed = await shifts_service.auto_close_stale()
            purged = await users_repo.purge_expired_sessions()
            if closed or purged:
                log.info("housekeeping: %d store(s) auto-closed, %d session(s) purged",
                         closed, purged)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a bad hour must not kill the loop
            log.exception("housekeeping pass failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    log.info("starting vapestore (tz=%s, base_url=%s)", settings.tzname, settings.base_url)
    await db.connect()
    task = asyncio.create_task(_housekeeping())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await db.close()


app = FastAPI(
    title="vapestore",
    lifespan=lifespan,
    # The owner UI is server-rendered and the bot API is documented in the README;
    # an interactive schema page is one more unauthenticated surface for nothing.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(partials.router)
app.include_router(bot_api.router)


def _wants_json(request: Request) -> bool:
    """Bot API callers get the JSON envelope; browsers get a rendered page."""
    return request.url.path.startswith(BOT_API_PREFIX)


@app.exception_handler(NotLoggedIn)
async def _not_logged_in(request: Request, exc: NotLoggedIn) -> Response:
    return login_redirect(request)


@app.exception_handler(AppError)
async def _app_error(request: Request, exc: AppError) -> Response:
    if isinstance(exc, BotError):
        log.info("bot error %s on %s: %s", exc.code, request.url.path, exc.message)
    if _wants_json(request):
        return JSONResponse(exc.payload(), status_code=exc.status)
    if exc.status == 401:
        return RedirectResponse("/login", status_code=303)
    return render(
        request,
        "error.html",
        {"status": exc.status, "message": exc.message, "user": None},
        status_code=exc.status,
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Normalise pydantic failures into the same envelope everything else uses."""
    error = AppError(
        "validation_error",
        details={"fields": [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]},
    )
    if _wants_json(request):
        return JSONResponse(error.payload(), status_code=error.status)
    return render(
        request,
        "error.html",
        {"status": error.status, "message": error.message, "user": None},
        status_code=error.status,
    )


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> Response:
    log.exception("unhandled error on %s", request.url.path)
    error = AppError("internal")
    if _wants_json(request):
        return JSONResponse(error.payload(), status_code=500)
    return render(
        request,
        "error.html",
        {"status": 500, "message": error.message, "user": None},
        status_code=500,
    )


@app.get("/health")
async def health() -> JSONResponse:
    """Railway's healthcheck. Touches the database, so a deploy that cannot reach
    Neon is reported unhealthy rather than quietly serving 500s."""
    ok = await db.healthy()
    return JSONResponse(
        {"ok": ok, "db": "up" if ok else "down"},
        status_code=200 if ok else 503,
    )
