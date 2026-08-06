"""Request-scoped dependencies: who is asking, and are they allowed to.

Three separate gates:

* ``current_user``      — a live website session, else a redirect to /login.
* ``require_csrf``      — every state-changing website POST.
* ``require_bot_secret``— every /api/bot/v1 request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse

from app.config import settings
from app.errors import AppError
from app.repo import users as users_repo
from app.security import constant_time_equals, hash_token

log = logging.getLogger("storemanager.deps")

SESSION_COOKIE = "vs_session"
# Used only on pages rendered before a session exists (the login form). Classic
# double-submit: the cookie and the form field must agree.
PRE_SESSION_CSRF_COOKIE = "vs_csrf"


class NotLoggedIn(Exception):
    """Raised by ``current_user``; turned into a redirect by the exception handler."""


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    display_name: str | None
    csrf_token: str
    token_hash: str

    @property
    def label(self) -> str:
        return self.display_name or self.email


async def optional_user(request: Request) -> CurrentUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = await users_repo.session_with_user(hash_token(token))
    if row is None or not row["is_active"]:
        return None
    await users_repo.touch_session(row["token_hash"])
    return CurrentUser(
        id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        csrf_token=row["csrf_token"],
        token_hash=row["token_hash"],
    )


async def current_user(user: CurrentUser | None = Depends(optional_user)) -> CurrentUser:
    if user is None:
        raise NotLoggedIn()
    return user


def login_redirect(request: Request) -> RedirectResponse:
    """Send an anonymous visitor to the login page and forget any stale cookie."""
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# -- CSRF ------------------------------------------------------------------

def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts using the headers a browser sets itself.

    A page on another origin can make the browser send our cookies, but it cannot
    forge Origin. Requests with neither header (curl, some older clients) are let
    through — the token check below is the real gate; this is defence in depth.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return True
    expected = urlsplit(settings.base_url)
    actual = urlsplit(source)
    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)


async def _submitted_token(request: Request) -> str:
    """The token from the HTMX header, or failing that the form field."""
    header = request.headers.get("x-csrf-token")
    if header:
        return header
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 - a body we cannot parse carries no token
        return ""
    return str(form.get("csrf_token") or "")


async def require_csrf(
    request: Request, user: CurrentUser = Depends(current_user)
) -> CurrentUser:
    """Guard every logged-in state-changing POST."""
    if not _same_origin(request):
        log.warning("cross-origin POST to %s rejected", request.url.path)
        raise AppError("forbidden")
    submitted = await _submitted_token(request)
    if not submitted or not constant_time_equals(submitted, user.csrf_token):
        raise AppError("forbidden")
    return user


async def require_pre_session_csrf(request: Request) -> None:
    """Guard a POST from a page rendered before any session existed (login)."""
    if not _same_origin(request):
        raise AppError("forbidden")
    cookie = request.cookies.get(PRE_SESSION_CSRF_COOKIE) or ""
    submitted = await _submitted_token(request)
    if not cookie or not submitted or not constant_time_equals(submitted, cookie):
        raise AppError("forbidden")


def set_pre_session_csrf(response, token: str, *, already_set: bool = False) -> None:
    """Attach the pre-session CSRF cookie, unless the browser already holds it."""
    if already_set:
        return
    response.set_cookie(
        PRE_SESSION_CSRF_COOKIE,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
        max_age=60 * 60 * 2,
    )


# -- bot ---------------------------------------------------------------------

async def require_bot_secret(request: Request) -> None:
    """Authenticate the Telegram bot service.

    Accepts either header shape so the bot can be written against whichever is
    more convenient without a coordinated deploy.
    """
    presented = request.headers.get("x-bot-secret") or ""
    if not presented:
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("bearer "):
            presented = authorization[7:]
    if not presented or not constant_time_equals(presented, settings.bot_shared_secret):
        raise AppError("unauthorized")


def client_ip(request: Request) -> str | None:
    """The caller's address, trusting Railway's proxy header when present."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None
