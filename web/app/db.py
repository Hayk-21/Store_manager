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


# How many of a page's queries may be in flight at once. The pool holds ten, and a
# single page asking for thirteen connections would leave nothing for the footer
# poll, for the bot posting a sale, or for the second person looking at the same
# page — none of them would fail, they would queue, which is the sluggishness this
# was meant to remove rather than relocate.
#
# Five collects nearly all of the win: what costs the page is round-trips, and
# thirteen sequential ones become three waves. Going wider buys a fraction of one
# round-trip and spends the pool to do it.
FAN_OUT = 5


class Database:
    """Owns the connection pool for the process."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._bound: asyncpg.Connection | None = None
        # Created by bind(), and only there. See _run.
        self._one_at_a_time: asyncio.Lock | None = None

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

        The lock comes with the binding. One asyncpg connection cannot carry two
        statements at once — it raises «another operation is in progress» — and the
        read paths now gather their independent queries, so that a page waits for
        one network round-trip rather than nineteen. In production each of those
        lands on its own pooled connection and they genuinely overlap; here they
        queue and run one after another, which gives the same answers. Without it
        the concurrency would work in production and fail only under test, which is
        the worst way round.

        It is made here rather than in ``__init__`` because a lock belongs to the
        event loop it is first awaited in, and each test is given a fresh one — a
        lock built once at import is bound to the first test's loop and raises in
        every test after it.
        """
        self._bound = conn
        self._one_at_a_time = asyncio.Lock() if conn is not None else None

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
            assert self._one_at_a_time is not None  # noqa: S101 - set by bind()
            async with self._one_at_a_time, self._bound.transaction():
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

    async def fan_out(self, *queries: Any, limit: int = FAN_OUT) -> list[Any]:
        """Run independent reads together, a few at a time, in the order given.

        A page asks a dozen unrelated questions about the same period, and awaiting
        them one after another means paying the network round-trip a dozen times
        before any answer arrives. Against Neon that waiting *is* the page's speed.

        **Reads only.** Statements that have to be atomic belong in
        ``db.transaction()``, which keeps them on a single connection; scattering
        those across the pool would leave a sale half-applied and look, from the
        outside, like nothing had gone wrong at all.
        """
        guard = asyncio.Semaphore(limit)

        async def one(query: Any) -> Any:
            async with guard:
                return await query

        return await asyncio.gather(*(one(query) for query in queries))

    async def healthy(self) -> bool:
        try:
            return await asyncio.wait_for(self.fetchval("SELECT 1"), timeout=3.0) == 1
        except Exception as exc:  # noqa: BLE001 - health checks must not raise
            log.warning("health check failed: %s", exc)
            return False


db = Database()
