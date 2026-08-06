"""Login and logout.

Deliberately only these two paths. When the emailed-code flow arrives it becomes
a separate ``auth_email.py`` mounted at /start, /verify and /set-password —
nothing here has to move.
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
from app.repo import users as users_repo
from app.security import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    normalise_email,
    verify_password,
)
from app.templating import render

log = logging.getLogger("storemanager.auth")

router = APIRouter()

# Deliberately identical for "no such address", "wrong password" and "deactivated
# account": telling them apart would let anyone test which emails exist.
_BAD_CREDENTIALS = "Էլ. փոստը կամ գաղտնաբառը սխալ է։"
_THROTTLED = "Չափազանց շատ փորձեր։ Սպասեք մի քանի րոպե և կրկին փորձեք։"


@router.get("/")
async def index(user: CurrentUser | None = Depends(optional_user)) -> RedirectResponse:
    return RedirectResponse("/stores" if user else "/login", status_code=303)


@router.get("/login")
async def login_page(request: Request, user: CurrentUser | None = Depends(optional_user)):
    if user is not None:
        return RedirectResponse("/stores", status_code=303)
    existing = request.cookies.get(PRE_SESSION_CSRF_COOKIE)
    token = existing or generate_token()
    response = render(request, "login.html", {"csrf_token": token, "error": None})
    set_pre_session_csrf(response, token, already_set=bool(existing))
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    _: None = Depends(require_pre_session_csrf),
):
    address = normalise_email(email)
    ip = client_ip(request)

    def failure(message: str) -> Response:
        response = render(
            request,
            "login.html",
            {
                "csrf_token": request.cookies.get(PRE_SESSION_CSRF_COOKIE) or "",
                "error": message,
                "email": address,
            },
            status_code=400,
        )
        return response

    if await users_repo.recent_failures(address, ip, settings.login_window_minutes) >= (
        settings.login_max_attempts
    ):
        log.warning("login throttled for %s from %s", address, ip)
        return failure(_THROTTLED)

    row = await users_repo.by_email(address)
    stored_hash = row["password_hash"] if row else None
    # Runs the full argon2 verification even when there is no such user, so the
    # response time cannot be used to enumerate addresses.
    password_ok = verify_password(stored_hash, password)

    if row is None or not password_ok or not row["is_active"]:
        await users_repo.record_attempt(address, ip, succeeded=False)
        return failure(_BAD_CREDENTIALS)

    if needs_rehash(stored_hash):
        await users_repo.set_password(row["id"], hash_password(password))

    token = generate_token()
    await users_repo.create_session(
        token_hash=hash_token(token),
        user_id=row["id"],
        csrf_token=generate_token(),
        user_agent=request.headers.get("user-agent"),
        ttl_days=settings.session_ttl_days,
    )
    await users_repo.record_attempt(address, ip, succeeded=True)
    await users_repo.clear_failures(address)
    await users_repo.touch_login(row["id"])

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
    log.info("login ok user=%s", row["id"])
    return response


@router.post("/logout")
async def logout(request: Request, user: CurrentUser = Depends(current_user)):
    # No CSRF token required: the worst a forged logout can do is inconvenience,
    # and demanding one would make the "sign me out" link fail after a session
    # rotates.
    await users_repo.delete_session(user.token_hash)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
