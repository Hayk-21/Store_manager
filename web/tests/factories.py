"""Helpers that put rows in the database for a test to work against.

Deliberately thin and explicit: a test should be able to see the whole world it
runs in from the few lines at the top of it.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.db import db

# Yerevan, Northern Avenue. Any two nearby points would do; real coordinates just
# make distances in a failing assertion easier to reason about.
YEREVAN_LAT = 40.177200
YEREVAN_LNG = 44.503200

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


async def make_owner(handle: str | None = None, *, bound_to: int | None = None) -> int:
    """An owner account, identified by Telegram handle.

    ``bound_to`` gives them a chat, which is what a login code needs; tests that
    only want *a* session use ``login()`` below and skip that entirely.
    """
    username = (handle or f"owner{_next()}").lstrip("@")
    user_id = await db.fetchval(
        """
        INSERT INTO users (telegram_username, activated_at)
        VALUES ($1, now()) RETURNING id
        """,
        username,
    )
    if bound_to is not None:
        await db.execute(
            "UPDATE users SET telegram_id = $2, telegram_bound_at = now() WHERE id = $1",
            user_id, bound_to,
        )
    return user_id


async def make_store(
    owner_id: int,
    name: str | None = None,
    *,
    lat: float | None = YEREVAN_LAT,
    lng: float | None = YEREVAN_LNG,
    radius_m: int = 120,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO stores (owner_id, name, lat, lng, radius_m)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
        """,
        owner_id,
        name or f"Խանութ {_next()}",
        lat,
        lng,
        radius_m,
    )


async def make_item(
    owner_id: int,
    store_id: int,
    name: str | None = None,
    *,
    count: int = 10,
    self_price: str = "1500.00",
    sell_price: str = "3500.00",
) -> int:
    return await db.fetchval(
        """
        INSERT INTO items (owner_id, store_id, name, count, self_price, sell_price)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """,
        owner_id,
        store_id,
        name or f"Ապրանք {_next()}",
        count,
        Decimal(self_price),
        Decimal(sell_price),
    )


async def make_worker(
    owner_id: int,
    name: str | None = None,
    *,
    telegram_id: int | None = None,
    telegram_username: str | None = None,
    salary_amount: str = "8000.00",
    salary_period: str = "shift",
    is_active: bool = True,
) -> tuple[int, int]:
    """An already-bound worker. Returns ``(worker_id, telegram_id)``.

    Registration in real life starts unbound (a @username and no id); that path
    has its own tests. Most tests want somebody who has already made contact.
    """
    tg = telegram_id if telegram_id is not None else 700_000_000 + _next()
    worker_id = await db.fetchval(
        """
        INSERT INTO workers (owner_id, name, telegram_id, telegram_username,
                             salary_amount, salary_period, is_active)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """,
        owner_id,
        name or f"Աշխատող {_next()}",
        tg,
        telegram_username or f"worker{_next()}",
        Decimal(salary_amount),
        salary_period,
        is_active,
    )
    return worker_id, tg


async def worked_a_full_shift(worker_id: int | None = None, hours: float = 9) -> None:
    """Backdate an open shift so it counts as a full day's work.

    A shift under eight hours is paid half, and a test that opens a shift and
    closes it on the next line has worked none. Most tests are not about the wage
    at all — they are about stock, or the till, or what a report renders — and
    every one of them would otherwise have to know the halving rule to write down
    a number. Calling this says "assume they did the day", which is what those
    tests always meant.

    Without a ``worker_id`` it applies to every open shift, which is what a test
    with two people in one shop wants — and what a test holding only a telegram_id
    can reach.

    The tests that *are* about short shifts are in test_short_shifts.py, and they
    backdate deliberately in the other direction.
    """
    await db.execute(
        """
        UPDATE work_sessions SET started_at = now() - $2::interval
         WHERE ended_at IS NULL AND ($1::bigint IS NULL OR worker_id = $1)
        """,
        worker_id,
        # A timedelta, not a string: asyncpg maps an interval parameter from
        # datetime.timedelta and refuses '9 hours' outright.
        timedelta(hours=hours),
    )


async def login(client, handle: str):
    """Sign in for tests that only need *a* session.

    Mints a session directly rather than driving the Telegram code flow: most
    tests care about what happens once you are in, not how you got there, and
    the code flow has its own tests in test_login.py.
    """
    from app.config import settings
    from app.deps import SESSION_COOKIE
    from app.repo import users as users_repo
    from app.security import generate_token, hash_token

    user = await users_repo.by_telegram_username(handle.lstrip("@"))
    assert user is not None, f"no owner account for {handle}"

    token = generate_token()
    await users_repo.create_session(
        token_hash=hash_token(token),
        user_id=user["id"],
        csrf_token=generate_token(),
        user_agent="tests",
        ttl_days=settings.session_ttl_days,
    )
    client.cookies.set(SESSION_COOKIE, token)
    return user["id"]
