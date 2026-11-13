"""Boucle de collecte en tâche de fond (même processus que l'API)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from . import db, log, paper
from .config import Settings
from .ingest import congress, house, prices, sec

_log = log.get("jobs")


async def record(conn, name: str, *, ok: bool, error: str | None = None, info: dict | None = None) -> None:
    await conn.execute(
        """INSERT INTO job_state(name, last_run_at, last_ok_at, last_error_at, last_error, info)
           VALUES (%(n)s, now(), CASE WHEN %(ok)s THEN now() END, CASE WHEN %(ok)s THEN NULL ELSE now() END,
                   %(err)s, COALESCE(%(info)s::jsonb, '{}'::jsonb))
           ON CONFLICT (name) DO UPDATE SET
             last_run_at = now(),
             last_ok_at = CASE WHEN %(ok)s THEN now() ELSE job_state.last_ok_at END,
             last_error_at = CASE WHEN %(ok)s THEN job_state.last_error_at ELSE now() END,
             last_error = CASE WHEN %(ok)s THEN job_state.last_error ELSE %(err)s END,
             info = CASE WHEN %(info)s::jsonb IS NULL THEN job_state.info ELSE job_state.info || %(info)s::jsonb END""",
        {"n": name, "ok": ok, "err": error, "info": json.dumps(info, default=str) if info is not None else None},
    )
    await conn.commit()


async def job_info(conn, name: str) -> dict:
    cur = await conn.execute("SELECT info, last_ok_at FROM job_state WHERE name=%s", (name,))
    row = await cur.fetchone()
    await conn.commit()
    return row or {"info": {}, "last_ok_at": None}


class Ingestor:
    def __init__(self, settings: Settings, pool, price_client: prices.PriceClient):
        self.s = settings
        self.pool = pool
        self.prices = price_client
        self.sec = sec.SecClient(settings.sec_user_agent)
        self.house = house.HouseClient(settings.sec_user_agent)
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.running = False
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="ingest")

    def trigger(self) -> bool:
        """Demande un cycle immédiat (bouton admin). False si un cycle tourne déjà."""
        if self.running:
            return False
        self.wake.set()
        return True

    async def shutdown(self, timeout: float = 1.5) -> None:
        self.stop.set()
        self.wake.set()
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        await self.sec.close()
        await self.house.close()

    async def _loop(self) -> None:
        await asyncio.sleep(2)  # laisse le serveur finir de démarrer
        while not self.stop.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log.exception("cycle de collecte interrompu")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.s.ingest_interval_min * 60)
            except asyncio.TimeoutError:
                pass

    async def run_cycle(self) -> None:
        async with self.pool.connection() as lock_conn:
            await lock_conn.set_autocommit(True)
            cur = await lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (db.INGEST_LOCK,))
            if not (await cur.fetchone())["ok"]:
                _log.info("collecte déjà en cours ailleurs, cycle ignoré")
                await lock_conn.set_autocommit(False)
                return
            self.running = True
            t0 = time.monotonic()
            try:
                async with self.pool.connection() as conn:
                    await self._steps(conn)
            finally:
                self.running = False
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (db.INGEST_LOCK,))
                await lock_conn.set_autocommit(False)
                _log.info("cycle de collecte terminé en %.1f s", time.monotonic() - t0)

    async def _step(self, conn, name: str, coro_factory) -> None:
        if self.stop.is_set():
            return
        t0 = time.monotonic()
        try:
            info = await coro_factory()
            info = dict(info or {})
            info["duration_s"] = round(time.monotonic() - t0, 1)
            await record(conn, name, ok=True, info=info)
            _log.info("étape %s : %s", name, info)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — chaque étape est isolée
            try:
                await conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            msg = f"{type(exc).__name__}: {exc}"[:500]
            _log.warning("étape %s en erreur : %s", name, msg)
            try:
                await record(conn, name, ok=False, error=msg)
            except Exception:  # noqa: BLE001
                _log.exception("impossible d'enregistrer l'erreur de %s", name)

    async def _steps(self, conn) -> None:
        async def tickers():
            st = await job_info(conn, "tickers")
            last = st["last_ok_at"]
            if last and datetime.now(timezone.utc) - last < timedelta(hours=24):
                return {"skipped": True}
            return {"tickers": await sec.refresh_tickers(self.sec, conn), "skipped": False}

        async def form4_feed():
            return {"queued": await sec.poll_form4_feed(self.sec, conn)}

        async def backfill():
            st = await job_info(conn, "backfill")
            done = set((st["info"] or {}).get("days", []))
            added, new_days = await sec.backfill(self.sec, conn, self.s.backfill_days, done)
            keep = sorted(done | set(new_days))[-60:]
            return {"queued": added, "days": keep}

        async def form4_queue():
            stats = await sec.process_queue(self.sec, conn, stop=self.stop)
            cur = await conn.execute("SELECT count(*) AS n FROM sec_filings WHERE status='pending'")
            stats["pending"] = (await cur.fetchone())["n"]
            await conn.commit()
            return stats

        async def ownership():
            return {"new": await sec.poll_ownership(self.sec, conn)}

        async def house_index():
            today = datetime.now(timezone.utc).date()
            years = [today.year - 1, today.year] if today.month <= 2 else [today.year]
            return {"queued": await house.poll_index(self.house, conn, years)}

        async def house_queue():
            stats = await house.process_queue(self.house, conn, stop=self.stop)
            cur = await conn.execute("SELECT count(*) AS n FROM house_filings WHERE status='pending'")
            stats["pending"] = (await cur.fetchone())["n"]
            await conn.commit()
            return stats

        async def congress_step():
            return {"new": await congress.fetch_and_store(self.s.quiver_api_key, conn,
                                                          skip_house=self.s.house_enabled)}

        async def prices_step():
            return await prices.refresh_all(self.pool, self.prices, stop=self.stop)

        async def stops():
            return {"triggered": await paper.apply_stops(conn)}

        await self._step(conn, "tickers", tickers)
        await self._step(conn, "form4_feed", form4_feed)
        if self.s.backfill_days > 0:
            await self._step(conn, "backfill", backfill)
        await self._step(conn, "form4_queue", form4_queue)
        await self._step(conn, "ownership", ownership)
        if self.s.house_enabled:
            await self._step(conn, "house_index", house_index)
            await self._step(conn, "house_queue", house_queue)
        if self.s.quiver_api_key:
            await self._step(conn, "congress", congress_step)
        await self._step(conn, "prices", prices_step)
        await self._step(conn, "stops", stops)
