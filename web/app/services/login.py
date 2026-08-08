"""Signing in with a Telegram handle and a one-time code.

The flow, and why it has the shape it does:

1. The admin types their handle. If that handle is not registered, nothing
   happens — but the page says the same thing either way, so the form cannot be
   used to find out which handles exist.
2. If it is registered *and bound to a chat*, a six-digit code goes out over the
   bot. Binding is the awkward part: a Telegram bot may only reply to somebody
   who has messaged it first, so an admin who has never pressed /start has no
   chat to receive anything, and is told exactly that.
3. They type the code. Five wrong guesses burn it.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.errors import AppError
from app.repo import users as users_repo
from app.security import constant_time_equals, generate_code, hash_code
from app.services import telegram

log = logging.getLogger("storemanager.login")

CODE_MESSAGE = (
    "🔐 <b>Store Manager</b>\n\n"
    "Ձեր մուտքի կոդը՝ <code>{code}</code>\n\n"
    "Գործում է {minutes} րոպե։\n"
    "Եթե դուք չեք փորձել մուտք գործել, պարզապես անտեսեք այս հաղորդագրությունը։"
)

# Deliberately the same answer for "no such handle", "not bound" is separate but
# "deactivated" is folded in here: whether an account exists is not something the
# login form should reveal.
SENT_NOTICE = (
    "Եթե այս օգտանունը գրանցված է, բոտը հենց նոր ուղարկեց մուտքի կոդը։ "
    "Ստուգեք Telegram-ը։"
)
NOT_BOUND = (
    "Այս օգտանունը գրանցված է, բայց բոտը դեռ չի կարող ձեզ գրել։\n\n"
    "Բացեք <a href=\"{bot_link}\">{bot_name}</a> Telegram-ում, սեղմեք "
    "«Start», ապա վերադարձեք և կրկին փորձեք։"
)
TOO_MANY = "Չափազանց շատ փորձեր։ Սպասեք մի փոքր և կրկին փորձեք։"
BAD_CODE = "Կոդը սխալ է կամ ժամկետանց։ Խնդրեք նորը։"


class NotBound(AppError):
    """Registered, but the bot has no chat to send anything to."""

    def __init__(self) -> None:
        super().__init__("validation_error", "not bound", status=400)


async def request_code(username: str, ip: str | None) -> None:
    """Send a login code, or quietly do nothing if the handle is not ours.

    Raises NotBound when the handle *is* ours but nobody has started the bot,
    because that is a real instruction the person can act on rather than
    information about who has an account.
    """
    user = await users_repo.by_telegram_username(username)
    if user is None or not user["is_active"]:
        # Same visible outcome as success. Spend no more time on it.
        log.info("login code requested for unknown or inactive handle @%s", username)
        return

    if user["telegram_id"] is None:
        raise NotBound()

    if await users_repo.codes_issued_since(user["id"], 60) >= settings.login_codes_per_hour:
        log.warning("login codes throttled for user %s", user["id"])
        raise AppError("validation_error", TOO_MANY, status=429)

    code = generate_code()
    await users_repo.replace_login_code(
        user["id"], hash_code(code), settings.login_code_ttl_minutes
    )

    try:
        await telegram.send_message(
            user["telegram_id"],
            CODE_MESSAGE.format(code=code, minutes=settings.login_code_ttl_minutes),
        )
    except telegram.Undeliverable as exc:
        if exc.blocked:
            raise NotBound() from exc
        raise AppError(
            "validation_error",
            "Կոդը չհաջողվեց ուղարկել։ Փորձեք մի փոքր ուշ։",
            status=502,
        ) from exc

    log.info("login code sent to user %s from %s", user["id"], ip)


async def verify_code(username: str, code: str) -> int:
    """Return the user id when the code is right, else raise.

    The same complaint for every failure, so a wrong code cannot be told apart
    from an unknown handle.
    """
    bad = AppError("validation_error", BAD_CODE, status=400)

    user = await users_repo.by_telegram_username(username)
    if user is None or not user["is_active"]:
        raise bad

    live = await users_repo.live_login_code(user["id"])
    if live is None:
        raise bad

    if live["attempts"] >= settings.max_code_attempts:
        await users_repo.consume_login_code(live["id"])
        raise AppError("validation_error", TOO_MANY, status=429)

    if not constant_time_equals(hash_code(code.strip()), live["code_hash"]):
        attempts = await users_repo.bump_code_attempts(live["id"])
        log.info("wrong login code for user %s (attempt %s)", user["id"], attempts)
        raise bad

    await users_repo.consume_login_code(live["id"])
    return user["id"]
