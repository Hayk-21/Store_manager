"""Signing in.

A Telegram handle and a code the bot sends. That is the only way in through the
browser — there is no password to guess, phish or leak.

The way back in when Telegram is not available is ``manage.py user login-link``,
which mints a single-use URL from the command line. It needs database access
rather than a public form, which is the right shape for an escape hatch.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.config import settings
from app.deps import (
    PRE_SESSION_CSRF_COOKIE,
    SESSION_COOKIE,
    CurrentUser,
    client_ip,
    current_user,
    optional_user,
    require_pre_session_csrf,
    set_pre_session_csrf,
)
from app.errors import AppError
from app.repo import users as users_repo
from app.repo import workers as workers_repo
from app.security import generate_token, hash_token
from app.services import login as login_service
from app.templating import render

log = logging.getLogger("storemanager.auth")

router = APIRouter()

_BAD_HANDLE = "Գրեք ձեր Telegram օգտանունը՝ @-ով, օրինակ՝ @justhayk։"


def _bot_link() -> tuple[str, str]:
    """The @name and t.me link of the bot, for "go and press Start" messages."""
    name = settings.bot_username or "bot"
    return f"@{name}", f"https://t.me/{name}"


async def _start_session(request: Request, user_id: int) -> RedirectResponse:
    token = generate_token()
    await users_repo.create_session(
        token_hash=hash_token(token),
        user_id=user_id,
        csrf_token=generate_token(),
        user_agent=request.headers.get("user-agent"),
        ttl_days=settings.session_ttl_days,
    )
    await users_repo.touch_login(user_id)
    response = RedirectResponse("/stores", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        # Long-lived on purpose, and pushed further out on every visit, so
        # somebody who uses the site daily never has to sign in again.
        max_age=settings.session_ttl_days * 24 * 3600,
    )
    response.delete_cookie(PRE_SESSION_CSRF_COOKIE, path="/")
    log.info("login ok user=%s", user_id)
    return response


def _page(request: Request, template: str, **context) -> Response:
    """Render a pre-session page, minting the CSRF cookie if it is not there."""
    existing = request.cookies.get(PRE_SESSION_CSRF_COOKIE)
    token = existing or generate_token()
    status = context.pop("status_code", 200)
    bot_name, bot_link = _bot_link()
    response = render(
        request,
        template,
        {"csrf_token": token, "bot_name": bot_name, "bot_link": bot_link, **context},
        status_code=status,
    )
    set_pre_session_csrf(response, token, already_set=bool(existing))
    return response


@router.get("/")
async def index(user: CurrentUser | None = Depends(optional_user)) -> RedirectResponse:
    return RedirectResponse("/stores" if user else "/login", status_code=303)


@router.get("/login")
async def login_page(request: Request, user: CurrentUser | None = Depends(optional_user)):
    if user is not None:
        return RedirectResponse("/stores", status_code=303)
    return _page(request, "login.html", error=None, handle="")


@router.post("/login")
async def login_request_code(
    request: Request,
    telegram_username: str = Form(""),
    _: None = Depends(require_pre_session_csrf),
):
    handle = workers_repo.normalise_username(telegram_username)
    if not handle:
        return _page(request, "login.html", error=_BAD_HANDLE,
                     handle=telegram_username, status_code=400)

    try:
        await login_service.request_code(handle, client_ip(request))
    except login_service.NotBound:
        bot_name, bot_link = _bot_link()
        return _page(
            request,
            "login.html",
            error=None,
            not_bound=login_service.NOT_BOUND.format(bot_link=bot_link, bot_name=bot_name),
            handle=telegram_username,
            status_code=400,
        )
    except AppError as exc:
        return _page(request, "login.html", error=exc.message,
                     handle=telegram_username, status_code=exc.status)

    # Reached whether or not the handle exists, so the form cannot be used to
    # find out which handles do.
    return _page(request, "login_verify.html", error=None, handle=handle,
                 notice=login_service.SENT_NOTICE)


@router.post("/login/verify")
async def login_verify(
    request: Request,
    telegram_username: str = Form(""),
    code: str = Form(""),
    _: None = Depends(require_pre_session_csrf),
):
    handle = workers_repo.normalise_username(telegram_username)
    if not handle:
        return _page(request, "login.html", error=_BAD_HANDLE, handle="", status_code=400)

    try:
        user_id = await login_service.verify_code(handle, code)
    except AppError as exc:
        return _page(request, "login_verify.html", error=exc.message,
                     handle=handle, status_code=exc.status)

    return await _start_session(request, user_id)


@router.get("/login/link/{token}")
async def login_link(request: Request, token: str):
    """Spend a one-time link minted by ``manage.py user login-link``.

    Not advertised anywhere and useless without the token, which exists only in
    whatever terminal printed it and works once.
    """
    user_id = await users_repo.consume_login_link(hash_token(token))
    if user_id is None:
        return _page(
            request,
            "login.html",
            error="Հղումն արդեն օգտագործվել է կամ ժամկետանց է։",
            handle="",
            status_code=400,
        )
    log.warning("user %s signed in with a one-time link", user_id)
    return await _start_session(request, user_id)


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(current_user)):
    # No CSRF token required: a forged logout is an inconvenience at worst, and
    # demanding one would make the link fail after a session rotates.
    await users_repo.delete_session(user.token_hash)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
