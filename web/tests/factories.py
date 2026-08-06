"""Helpers that put rows in the database for a test to work against.

Deliberately thin and explicit: a test should be able to see the whole world it
runs in from the few lines at the top of it.
"""

from __future__ import annotations

from decimal import Decimal

from app.db import db
from app.security import hash_password

# Yerevan, Northern Avenue. Any two nearby points would do; real coordinates just
# make distances in a failing assertion easier to reason about.
YEREVAN_LAT = 40.177200
YEREVAN_LNG = 44.503200

DEFAULT_PASSWORD = "correct-horse-battery"

_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


async def make_owner(email: str | None = None, password: str = DEFAULT_PASSWORD) -> int:
    address = email or f"owner{_next()}@example.com"
    return await db.fetchval(
        """
        INSERT INTO users (email, password_hash, activated_at, password_changed_at)
        VALUES ($1, $2, now(), now()) RETURNING id
        """,
        address,
        hash_password(password),
    )


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


async def login(client, email: str, password: str = DEFAULT_PASSWORD):
    """Drive the real login flow so the test exercises CSRF and cookies too."""
    page = await client.get("/login")
    token = client.cookies.get("vs_csrf")
    assert token, "the login page did not set a pre-session CSRF cookie"
    assert page.status_code == 200
    return await client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}
    )
