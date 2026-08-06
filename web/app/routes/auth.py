"""Signing in.

The way in is a Telegram handle plus a one-time code the bot sends. The old
email-and-password form is still mounted at ``/login/password`` and is not linked
from anywhere: if Telegram is down, or the bot token is revoked, that is the way
back into your own admin panel. It is worth the twenty lines.
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
from app.security import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    normalise_email,
    verify_password,
)
from app.services import login as login_service
from app.templating import render

log = logging.getLogger("storemanager.auth")

router = APIRouter()

_BAD_CREDENTIALS = "Էլ. փոստը կամ գաղտնաբառը սխալ է։"
_THROTTLED = "Չափազանց շատ փորձեր։ Սպասեք մի քանի րոպե և կրկին փորձեք։"
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


# -- telegram code flow ------------------------------------------------------

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


# -- password fallback -------------------------------------------------------

@router.get("/login/password")
async def password_page(request: Request, user: CurrentUser | None = Depends(optional_user)):
    """Deliberately unlinked. The way in when Telegram is not an option."""
    if user is not None:
        return RedirectResponse("/stores", status_code=303)
    return _page(request, "login_password.html", error=None, email="")


@router.post("/login/password")
async def password_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    _: None = Depends(require_pre_session_csrf),
):
    address = normalise_email(email)
    ip = client_ip(request)

    def failure(message: str) -> Response:
        return _page(request, "login_password.html", error=message,
                     email=address, status_code=400)

    if await users_repo.recent_failures(address, ip, settings.login_window_minutes) >= (
        settings.login_max_attempts
    ):
        log.warning("password login throttled for %s from %s", address, ip)
        return failure(_THROTTLED)

    row = await users_repo.by_email(address)
    stored_hash = row["password_hash"] if row else None
    # Runs the full argon2 verification even for an unknown address, so response
    # time cannot be used to enumerate accounts.
    password_ok = verify_password(stored_hash, password)

    if row is None or not password_ok or not row["is_active"]:
        await users_repo.record_attempt(address, ip, succeeded=False)
        return failure(_BAD_CREDENTIALS)

    if needs_rehash(stored_hash):
        await users_repo.set_password(row["id"], hash_password(password))

    await users_repo.record_attempt(address, ip, succeeded=True)
    await users_repo.clear_failures(address)
    return await _start_session(request, row["id"])


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(current_user)):
    # No CSRF token required: a forged logout is an inconvenience at worst, and
    # demanding one would make the link fail after a session rotates.
    await users_repo.delete_session(user.token_hash)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
