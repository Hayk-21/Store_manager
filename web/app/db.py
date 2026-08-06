"""asyncpg pool and the thin query helpers the rest of the app uses.

This is the only module that deals with connection plumbing. Everything above it
works in terms of ``db.fetch(...)`` / ``db.transaction()``.

``bind()`` is the affordance that makes the whole project testable: a test opens
one connection, begins a transaction, binds it here, and every query the
application makes lands inside that transaction. Rolling it back at the end of
the test restores an identical empty schema in microseconds. Application code
that opens its own ``transaction()`` still works — asyncpg turns a nested
``conn.transaction()`` into a SAVEPOINT, so commits and rollbacks inside the
application behave exactly as they do in production.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from app.config import clean_dsn, settings

log = logging.getLogger("storemanager.db")

# Neon scales a compute to zero when idle, so the first query after a quiet spell
# can find a connection the server has already dropped. These are the errors that
# looks like, and they are safe to retry once on a fresh connection.
_RETRYABLE = (
    asyncpg.exceptions.ConnectionDoesNotExistError,
    asyncpg.exceptions.InterfaceError,
    asyncpg.exceptions.TooManyConnectionsError,
    ConnectionResetError,
)


class Database:
    """Owns the connection pool for the process."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._bound: asyncpg.Connection | None = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database pool is not open; call connect() first")
        return self._pool

    async def connect(self) -> None:
        if self._pool is not None:
            return
        dsn = clean_dsn(settings.database_url)
        log.info(
            "opening pool (pooled_endpoint=%s, min=%d, max=%d)",
            dsn.pooled,
            settings.db_pool_min,
            settings.db_pool_max,
        )
        self._pool = await asyncpg.create_pool(
            dsn=dsn.url,
            ssl=dsn.ssl,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            statement_cache_size=dsn.statement_cache_size,
            # Neon drops idle connections; expire them before it does.
            max_inactive_connection_lifetime=300.0,
            command_timeout=15.0,
            timeout=20.0,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            log.info("pool closed")

    # -- test hook ---------------------------------------------------------

    def bind(self, conn: asyncpg.Connection | None) -> None:
        """Route every query through ``conn`` (tests only).

        Pass ``None`` to release. While bound, the pool is not touched at all, so
        a test suite needs no pool and no server.
        """
        self._bound = conn

    @property
    def is_bound(self) -> bool:
        return self._bound is not None

    # -- queries -----------------------------------------------------------

    async def _run(self, method: str, query: str, *args: Any) -> Any:
        if self._bound is not None:
            # A savepoint per statement, so a caught error behaves the way it
            # does in production. Without it, one expected UniqueViolation would
            # abort the test's outer transaction and every later query in that
            # test would fail with "current transaction is aborted" — an artefact
            # of the harness, not of the code under test.
            async with self._bound.transaction():
                return await getattr(self._bound, method)(query, *args)
        try:
            async with self.pool.acquire() as conn:
                return await getattr(conn, method)(query, *args)
        except _RETRYABLE as exc:
            log.warning("retrying %s after connection error: %s", method, exc)
            await asyncio.sleep(0.2)
            async with self.pool.acquire() as conn:
                return await getattr(conn, method)(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        return await self._run("fetch", query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        return await self._run("fetchrow", query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._run("fetchval", query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        return await self._run("execute", query, *args)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """Run several statements atomically.

        Every write that moves stock or money goes through this: a sale either
        applies in full or not at all. The connection is yielded directly, so
        callers use ``conn.fetch(...)`` rather than ``db.fetch(...)`` inside the
        block — that is what keeps the statements on one connection and therefore
        in one transaction.
        """
        if self._bound is not None:
            # Nested inside the test's outer transaction: asyncpg emits a
            # SAVEPOINT, so a rollback in here behaves like a real one.
            async with self._bound.transaction():
                yield self._bound
            return
        async with self.pool.acquire() as conn, conn.transaction():
            yield conn

    async def healthy(self) -> bool:
        try:
            return await asyncio.wait_for(self.fetchval("SELECT 1"), timeout=3.0) == 1
        except Exception as exc:  # noqa: BLE001 - health checks must not raise
            log.warning("health check failed: %s", exc)
            return False


db = Database()
