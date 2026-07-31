"""asyncpg connection pool plus the migration runner.

Migrations are plain, numbered `.sql` files applied inside a Postgres advisory
lock, so it is safe for several ECS tasks to boot at the same time: the first
one migrates, the rest wait and then see the work already recorded.
"""

from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Iterable
from typing import Any

import asyncpg

from .config import settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"

# Arbitrary but fixed application-wide lock ids.
MIGRATION_LOCK_ID = 947_112_001

_pool: asyncpg.Pool | None = None


# --------------------------------------------------------------------------- #
# Pool lifecycle
# --------------------------------------------------------------------------- #
async def _init_connection(conn: asyncpg.Connection) -> None:
    """Register codecs once per physical connection.

    pgvector arrives as text over the wire; converting it here means callers
    can pass and receive plain Python lists of floats.
    """
    await conn.set_type_codec(
        "vector",
        schema="public",
        encoder=_encode_vector,
        decoder=_decode_vector,
        format="text",
    )


def _encode_vector(value: Iterable[float]) -> str:
    return "[" + ",".join(f"{float(v):.7g}" for v in value) + "]"


def _decode_vector(value: str) -> list[float]:
    return [float(x) for x in value.strip("[]").split(",")] if value else []


async def _bootstrap_extensions() -> None:
    """Ensure pgvector exists before the pool registers its codec.

    The `vector` type must be present for `set_type_codec` to succeed, so this
    runs on a throwaway connection ahead of pool creation.
    """
    conn = await asyncpg.connect(dsn=settings.database_url)
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    finally:
        await conn.close()


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        await _bootstrap_extensions()
        log.info("opening database pool")
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            command_timeout=60,
            init=_init_connection,
        )
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised; call connect() first")
    return _pool


# --------------------------------------------------------------------------- #
# Thin query helpers
# --------------------------------------------------------------------------- #
async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async with pool().acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    async with pool().acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    async with pool().acquire() as conn:
        return await conn.fetchval(sql, *args)


async def execute(sql: str, *args: Any) -> str:
    async with pool().acquire() as conn:
        return await conn.execute(sql, *args)


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^(\d+)_")


def _discover_migrations() -> list[tuple[int, str, str]]:
    """Return [(version, name, sql)] ordered by version."""
    found: list[tuple[int, str, str]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _VERSION_RE.match(path.name)
        if not match:
            log.warning("skipping unversioned migration file %s", path.name)
            continue
        found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    return sorted(found, key=lambda row: row[0])


async def run_migrations() -> int:
    """Apply any migrations this database has not seen. Returns how many ran."""
    migrations = _discover_migrations()
    if not migrations:
        log.warning("no migration files found at %s", MIGRATIONS_DIR)
        return 0

    applied_count = 0
    async with pool().acquire() as conn:
        # Serialise concurrent boots. The lock is released when we disconnect,
        # but we release explicitly so the connection can return to the pool.
        await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_ID)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     integer PRIMARY KEY,
                    name        text        NOT NULL,
                    applied_at  timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            done = {
                row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for version, name, sql in migrations:
                if version in done:
                    continue
                log.info("applying migration %s", name)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                        version,
                        name,
                    )
                applied_count += 1
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_ID)

    log.info("migrations complete (%d applied)", applied_count)
    return applied_count


async def try_advisory_lock(conn: asyncpg.Connection, key: str) -> bool:
    """Best-effort named lock used to keep ingestion jobs from racing.

    Returns False immediately if another worker holds it, so a duplicate
    scheduled refresh becomes a no-op instead of a competing writer.
    """
    lock_id = _stable_lock_id(key)
    return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id))


async def advisory_unlock(conn: asyncpg.Connection, key: str) -> None:
    await conn.execute("SELECT pg_advisory_unlock($1)", _stable_lock_id(key))


def _stable_lock_id(key: str) -> int:
    """Map a name to a stable signed 64-bit int for pg_advisory_lock."""
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    value = int.from_bytes(digest, "big", signed=True)
    return value
