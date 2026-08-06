"""Test fixtures.

The whole suite runs against a real Postgres, because the schema is doing real
work — generated columns, partial unique indexes, composite foreign keys — and a
fake would not exercise any of it. Bring one up with:

    docker run -d --name vapestore-pg -e POSTGRES_PASSWORD=vapestore \
      -e POSTGRES_USER=vapestore -e POSTGRES_DB=vapestore -p 55432:5432 postgres:16

Each test gets an empty schema in about a millisecond: the migrations run once
per session, then every test opens one connection, begins a transaction, binds it
into ``app.db``, and rolls back at the end. Application code that opens its own
``db.transaction()`` still behaves correctly — asyncpg turns the nested call into
a SAVEPOINT.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

# Settings are read at import time, so the environment has to be complete before
# anything under app/ is imported.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://vapestore:vapestore@localhost:55432/vapestore_test?sslmode=disable",
)
os.environ.setdefault("DIRECT_DATABASE_URL", os.environ["DATABASE_URL"])
os.environ.setdefault("SESSION_SECRET", "test-session-secret-0123456789abcdef")
os.environ.setdefault("BOT_SHARED_SECRET", "test-bot-secret-0123456789abcdef")
os.environ.setdefault("APP_TIMEZONE", "Asia/Yerevan")
os.environ.setdefault("APP_BASE_URL", "http://test")
os.environ.setdefault("APP_INSECURE_COOKIES", "1")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import httpx  # noqa: E402

from app.config import clean_dsn, settings  # noqa: E402
from app.db import db  # noqa: E402
from app.main import app  # noqa: E402

BOT_SECRET = os.environ["BOT_SHARED_SECRET"]


async def _create_database_if_missing() -> None:
    """Create the test database itself, so a fresh checkout needs one docker run."""
    dsn = clean_dsn(settings.database_url)
    target = dsn.url.rsplit("/", 1)[-1]
    admin_url = dsn.url.rsplit("/", 1)[0] + "/postgres"
    conn = await asyncpg.connect(admin_url, ssl=dsn.ssl, statement_cache_size=0)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    """Bring the test database up to date once for the whole run.

    Synchronous on purpose: it drives its own event loop, so it does not have to
    agree with pytest-asyncio about fixture loop scopes.
    """
    from migrate import apply_all

    async def go() -> None:
        await _create_database_if_missing()
        await apply_all(settings.database_url)

    asyncio.run(go())


@pytest.fixture
async def conn(migrated) -> asyncpg.Connection:
    """One connection per test, inside a transaction that is always rolled back."""
    dsn = clean_dsn(settings.database_url)
    connection = await asyncpg.connect(dsn.url, ssl=dsn.ssl, statement_cache_size=0)
    tx = connection.transaction()
    await tx.start()
    db.bind(connection)
    try:
        yield connection
    finally:
        db.bind(None)
        await tx.rollback()
        await connection.close()


@pytest.fixture
async def client(conn) -> httpx.AsyncClient:
    """An HTTP client speaking to the ASGI app directly — no server, no port.

    The lifespan is deliberately not run: ``db`` is already bound to the test's
    transaction, so opening a pool would be pointless and would leak connections.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as http:
        yield http


@pytest.fixture
def bot_headers() -> dict[str, str]:
    return {"X-Bot-Secret": BOT_SECRET}
