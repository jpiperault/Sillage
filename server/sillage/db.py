"""Pool PostgreSQL asynchrone et migrations."""
from __future__ import annotations

import asyncio
import time
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from . import log

_log = log.get("db")
MIGRATION_LOCK = 72_510_001
INGEST_LOCK = 72_510_002
SLOW_LOCK = 72_510_003


def make_pool(url: str) -> AsyncConnectionPool:
    # min_size=1, open sans attendre : le service démarre même si la base est absente,
    # et le pool se reconnecte seul quand elle revient (check à l'emprunt).
    return AsyncConnectionPool(
        url,
        min_size=1,
        max_size=8,
        open=False,
        timeout=5,
        reconnect_timeout=0,  # n'abandonne jamais la reconnexion
        kwargs={"row_factory": dict_row, "autocommit": False, "connect_timeout": 3},
        check=AsyncConnectionPool.check_connection,
    )


def _migration_files() -> list[tuple[str, str]]:
    folder = resources.files("sillage") / "migrations"
    files = sorted((p.name, p.read_text("utf-8")) for p in folder.iterdir() if p.name.endswith(".sql"))
    return files


async def run_migrations(url: str, deadline_s: float = 60.0) -> int:
    """Applique les migrations manquantes ; réessaie la connexion pendant `deadline_s` secondes."""
    start = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        try:
            return await _migrate_once(url)
        except psycopg.OperationalError as exc:
            if time.monotonic() - start > deadline_s:
                raise
            _log.warning("base inaccessible (essai %d), nouvel essai dans 2 s : %s", attempt, exc)
            await asyncio.sleep(2)


async def _migrate_once(url: str) -> int:
    applied = 0
    async with await psycopg.AsyncConnection.connect(url, autocommit=True, connect_timeout=3) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT name FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            for name, sql in _migration_files():
                if name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (name,))
                applied += 1
                _log.info("migration appliquée : %s", name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))
    _log.info("migrations : %d nouvelle(s)", applied)
    return applied
