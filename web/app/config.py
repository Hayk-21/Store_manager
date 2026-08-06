"""Configuration, read once from the environment at import time.

Two things in here are load-bearing and worth reading before anything else:

* ``clean_dsn`` — Neon hands out connection URLs asyncpg cannot use verbatim.
* ``Settings.timezone`` — used for *display only*. Money never lands in a bucket
  chosen by the clock; it lands in the store session that was open at the time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """A required environment variable is missing or unusable."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in "
            f"(or set it in the Railway service variables)."
        )
    return value


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer") from exc


@dataclass(frozen=True)
class Dsn:
    """A Postgres URL split into the parts asyncpg actually accepts."""

    url: str
    ssl: str | bool
    pooled: bool

    @property
    def statement_cache_size(self) -> int:
        """Neon's pooler is PgBouncer in transaction mode, which cannot keep
        prepared statements alive between queries. Leaving asyncpg's cache on
        against it produces intermittent
        ``prepared statement "__asyncpg_stmt_x__" does not exist`` errors.
        """
        return 0 if self.pooled else 100


def normalise_base_url(raw: str) -> str:
    """Make sure the public URL carries a scheme.

    Railway shows a generated domain without one, and pasting it verbatim is the
    obvious thing to do. Without a scheme ``urlsplit`` puts the whole thing in
    ``path`` and leaves ``netloc`` empty, so the CSRF same-origin check compares
    the real origin against ``('', '')`` and rejects every POST -- including the
    login form. Failing that way is silent and baffling, so it is repaired here.
    """
    value = raw.strip().rstrip("/")
    if not value:
        return "http://localhost:8000"
    if "://" not in value:
        # Anything on a real host is https; only localhost is plausibly plain.
        scheme = "http" if value.startswith(("localhost", "127.0.0.1", "[::1]")) else "https"
        return f"{scheme}://{value}"
    return value


def clean_dsn(dsn: str) -> Dsn:
    """Strip query parameters from a Postgres URL.

    Neon hands out URLs carrying ``sslmode=require&channel_binding=require``.
    asyncpg does not understand ``channel_binding`` and raises on it, so the
    parameters are removed here and TLS is passed to ``create_pool`` explicitly.
    """
    parts = urlsplit(dsn)
    params = parse_qs(parts.query)
    bare = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    sslmode = (params.get("sslmode") or ["require"])[0].lower()
    ssl_setting: str | bool = False if sslmode in {"disable", "allow"} else sslmode
    return Dsn(url=bare, ssl=ssl_setting, pooled="-pooler" in (parts.hostname or ""))


@dataclass(frozen=True)
class Settings:
    database_url: str
    direct_database_url: str | None
    session_secret: str
    bot_shared_secret: str
    # The same token the bot service uses. The web service needs it to deliver
    # login codes itself rather than relaying through the bot.
    telegram_bot_token: str | None
    # Used only to build "go and press Start" links on the login page.
    bot_username: str | None
    login_code_ttl_minutes: int
    login_codes_per_hour: int
    max_code_attempts: int
    timezone: ZoneInfo
    tzname: str
    base_url: str
    currency: str
    insecure_cookies: bool
    log_level: str
    db_pool_min: int
    db_pool_max: int
    session_ttl_days: int
    login_max_attempts: int
    login_window_minutes: int
    auto_close_hours: int
    max_accuracy_m: int

    @property
    def cookie_secure(self) -> bool:
        return not self.insecure_cookies

    def now(self) -> datetime:
        """Current time in the configured display timezone."""
        return datetime.now(self.timezone)

    def local_day(self, moment: datetime | None = None) -> date:
        """The local calendar date of a moment.

        Used for ``store_sessions.opened_day``, which exists purely so reports can
        group sessions under a heading a human recognises. Nothing in the money
        model reads it.
        """
        moment = moment or self.now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.timezone)
        return moment.astimezone(self.timezone).date()


def _load() -> Settings:
    tzname = os.getenv("APP_TIMEZONE", "Asia/Yerevan").strip() or "Asia/Yerevan"
    try:
        tz = ZoneInfo(tzname)
    except Exception as exc:  # noqa: BLE001 - surfaced as a config error
        raise ConfigError(f"APP_TIMEZONE={tzname!r} is not a valid timezone") from exc

    return Settings(
        database_url=_required("DATABASE_URL"),
        direct_database_url=os.getenv("DIRECT_DATABASE_URL", "").strip() or None,
        session_secret=_required("SESSION_SECRET"),
        bot_shared_secret=_required("BOT_SHARED_SECRET"),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@") or None,
        login_code_ttl_minutes=_int("LOGIN_CODE_TTL_MINUTES", 10),
        login_codes_per_hour=_int("LOGIN_CODES_PER_HOUR", 6),
        max_code_attempts=_int("MAX_CODE_ATTEMPTS", 5),
        timezone=tz,
        tzname=tzname,
        base_url=normalise_base_url(os.getenv("APP_BASE_URL", "http://localhost:8000")),
        currency=os.getenv("APP_CURRENCY", "֏").strip(),
        insecure_cookies=_flag("APP_INSECURE_COOKIES", default=False),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        db_pool_min=_int("DB_POOL_MIN", 1),
        db_pool_max=_int("DB_POOL_MAX", 5),
        # Long, and pushed further out on every visit: somebody who uses the
        # site regularly should never be asked to sign in again.
        session_ttl_days=_int("SESSION_TTL_DAYS", 90),
        login_max_attempts=_int("LOGIN_MAX_ATTEMPTS", 10),
        login_window_minutes=_int("LOGIN_WINDOW_MINUTES", 15),
        # A store left open longer than this is assumed forgotten and closed for
        # the owner, paying every attached shift's salary.
        auto_close_hours=_int("AUTO_CLOSE_HOURS", 16),
        # Telegram reports GPS accuracy; a reading vaguer than this cannot tell
        # two neighbouring stores apart, so it is refused rather than guessed at.
        max_accuracy_m=_int("MAX_ACCURACY_M", 100),
    )


settings = _load()
