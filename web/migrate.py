"""Apply the .sql files in migrations/ in filename order, exactly once each.

Run locally with `python migrate.py`; Railway runs it as the preDeployCommand, so a
deploy that cannot migrate never starts serving traffic.

Migrations are append-only: once a file has been deployed, never edit it -- add a
new one. There are no down-migrations; roll forward.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import asyncpg

from app.config import clean_dsn, settings
from app.logging_conf import setup_logging

log = logging.getLogger("storemanager.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Any constant works; it just has to be the same in every deploy of this app.
ADVISORY_LOCK_KEY = 7311902244


async def apply_all(dsn_url: str | None = None) -> int:
    """Apply every pending migration. Returns the number applied."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        log.warning("no migration files found in %s", MIGRATIONS_DIR)
        return 0

    # DDL against a transaction-mode pooler is unreliable, so prefer the direct URL.
    dsn = clean_dsn(dsn_url or settings.direct_database_url or settings.database_url)
    if dsn.pooled:
        log.warning(
            "migrating over Neon's pooled endpoint; set DIRECT_DATABASE_URL to the "
            "non-pooled connection string if this misbehaves"
        )

    conn = await asyncpg.connect(dsn.url, ssl=dsn.ssl, statement_cache_size=0)
    try:
        # Serialize overlapping deploys: two instances must not migrate at once.
        await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename   text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                r["filename"]
                for r in await conn.fetch("SELECT filename FROM schema_migrations")
            }

            pending = [f for f in files if f.name not in applied]
            if not pending:
                log.info("database is up to date (%d migration(s) applied)", len(applied))
                return 0

            for path in pending:
                log.info("applying %s", path.name)
                sql = path.read_text(encoding="utf-8")
                # One transaction per file: it applies whole, or not at all.
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)", path.name
                    )
                log.info("applied %s", path.name)

            log.info("applied %d migration(s)", len(pending))
            return len(pending)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await conn.close()


async def run() -> int:
    setup_logging()
    await apply_all()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except Exception:
        logging.getLogger("storemanager.migrate").exception("migration failed")
        sys.exit(1)
