"""Collecte en tâche de fond (même processus que l'API).

- boucle rapide (toutes les N minutes) : flux live, cours, stops, alertes, règle automatique ;
- boucle lente (toutes les 5 minutes) : tâches lourdes, chacune à son propre rythme (13F, backtest, etc.).
Chaque boucle a son verrou PostgreSQL : une seule instance l'exécute à la fois."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from . import alerts, backtest, db, log, paper, rules
from .config import Settings
from .ingest import (amf, bafin, buybacks, congress, earnings, euronext, finra, fundamentals, funds, house,
                     prices, sec, senate)

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



SLOW_TICK_S = 300


class Ingestor:
    def __init__(self, get_settings, pool, price_client: prices.PriceClient, build_signals=None):
        """get_settings() : réglages à jour (variables + interface), relus à chaque cycle."""
        self.get_settings = get_settings
        self.pool = pool
        self.prices = price_client
        self.build_signals = build_signals
        self._ua = get_settings().sec_user_agent
        self.sec = sec.SecClient(self._ua)
        self.house = house.HouseClient(self._ua)
        self.amf = amf.AmfClient()
        self.history = backtest.HistoryClient()
        self.euronext = euronext.EuronextClient()
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.wake_slow = asyncio.Event()
        self.running = False
        self.running_slow = False
        self.tasks: list[asyncio.Task] = []

    @property
    def s(self) -> Settings:
        return self.get_settings()

    async def _refresh_clients(self) -> None:
        """Contact SEC modifié dans l'interface : nouveaux clients HTTP."""
        if self.s.sec_user_agent != self._ua:
            await self.sec.close()
            await self.house.close()
            self._ua = self.s.sec_user_agent
            self.sec = sec.SecClient(self._ua)
            self.house = house.HouseClient(self._ua)

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._loop(), name="ingest-fast"),
                      asyncio.create_task(self._slow_loop(), name="ingest-slow")]

    def trigger(self) -> bool:
        """Demande un cycle immédiat des deux boucles (bouton admin). False si la boucle rapide tourne déjà."""
        self.wake_slow.set()
        if self.running:
            return False
        self.wake.set()
        return True

    async def shutdown(self, timeout: float = 1.5) -> None:
        self.stop.set()
        self.wake.set()
        self.wake_slow.set()
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout / 2)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        for c in (self.sec, self.house, self.amf, self.history, self.euronext):
            await c.close()

    async def _loop(self) -> None:
        await asyncio.sleep(2)  # laisse le serveur finir de démarrer
        while not self.stop.is_set():
            try:
                await self._locked(db.INGEST_LOCK, "running", self._fast_steps, "cycle rapide")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log.exception("cycle rapide interrompu")
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.s.ingest_interval_min * 60)
            except asyncio.TimeoutError:
                pass

    async def _slow_loop(self) -> None:
        await asyncio.sleep(20)  # après le premier cycle rapide
        while not self.stop.is_set():
            try:
                await self._locked(db.SLOW_LOCK, "running_slow", self._slow_steps, "cycle lent")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _log.exception("cycle lent interrompu")
            self.wake_slow.clear()
            try:
                await asyncio.wait_for(self.wake_slow.wait(), timeout=SLOW_TICK_S)
            except asyncio.TimeoutError:
                pass

    async def run_cycle(self) -> None:  # compatibilité : un cycle rapide
        await self._locked(db.INGEST_LOCK, "running", self._fast_steps, "cycle rapide")

    async def _locked(self, lock_id: int, flag: str, steps, label: str) -> None:
        async with self.pool.connection() as lock_conn:
            await lock_conn.set_autocommit(True)
            cur = await lock_conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (lock_id,))
            if not (await cur.fetchone())["ok"]:
                _log.info("%s déjà en cours ailleurs, ignoré", label)
                await lock_conn.set_autocommit(False)
                return
            setattr(self, flag, True)
            t0 = time.monotonic()
            await self._refresh_clients()
            try:
                async with self.pool.connection() as conn:
                    await steps(conn)
            finally:
                setattr(self, flag, False)
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
                await lock_conn.set_autocommit(False)
                _log.info("%s terminé en %.1f s", label, time.monotonic() - t0)

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

    async def _due(self, conn, name: str, every: timedelta) -> bool:
        """Tâche lente à relancer ? (dernier passage, réussi ou non, plus vieux que `every`)."""
        cur = await conn.execute("SELECT last_run_at FROM job_state WHERE name=%s", (name,))
        r = await cur.fetchone()
        await conn.commit()
        return not r or not r["last_run_at"] or datetime.now(timezone.utc) - r["last_run_at"] >= every

    async def _slow(self, conn, name: str, every: timedelta, coro_factory) -> None:
        if await self._due(conn, name, every):
            await self._step(conn, name, coro_factory)

    # ------------------------------------------------------------ boucle rapide

    async def _fast_steps(self, conn) -> None:
        s = self.s

        async def form4_feed():
            return {"queued": await sec.poll_form4_feed(self.sec, conn)}

        async def backfill():
            st = await job_info(conn, "backfill")
            done = set((st["info"] or {}).get("days", []))
            added, new_days = await sec.backfill(self.sec, conn, s.backfill_days, done)
            return {"queued": added, "days": sorted(done | set(new_days))[-60:]}

        async def form4_queue():
            stats = await sec.process_queue(self.sec, conn, stop=self.stop)
            stats["pending"] = await _count(conn, "SELECT count(*) AS n FROM sec_filings WHERE status='pending'"
                                                  " AND form_type IN ('4','4/A')")
            return stats

        async def form144_feed():
            return {"queued": await sec.poll_feed(self.sec, conn, "144")}

        async def form144_queue():
            stats = await sec.process_queue(self.sec, conn, limit=200, stop=self.stop, form="144")
            stats["pending"] = await _count(conn, "SELECT count(*) AS n FROM sec_filings WHERE status='pending'"
                                                  " AND form_type='144'")
            stats["notices"] = stats.pop("trades")
            return stats

        async def ownership():
            return {"new": await sec.poll_ownership(self.sec, conn)}

        async def house_index():
            today = datetime.now(timezone.utc).date()
            years = [today.year - 1, today.year] if today.month <= 2 else [today.year]
            return {"queued": await house.poll_index(self.house, conn, years)}

        async def house_queue():
            stats = await house.process_queue(self.house, conn, stop=self.stop)
            stats["pending"] = await _count(conn, "SELECT count(*) AS n FROM house_filings WHERE status='pending'")
            return stats

        async def amf_list():
            return {"queued": await amf.poll_list(self.amf, conn)}

        async def amf_queue():
            stats = await amf.process_queue(self.amf, conn, stop=self.stop)
            stats["pending"] = await _count(conn, "SELECT count(*) AS n FROM amf_filings WHERE status='pending'")
            return stats

        async def congress_step():
            return {"new": await congress.fetch_and_store(s.quiver_api_key, conn, skip_house=s.house_enabled)}

        async def prices_step():
            return await prices.refresh_all(self.pool, self.prices, stop=self.stop)

        async def stops():
            return {"triggered": await paper.apply_stops(conn)}

        recent: list = []

        async def rule_step():
            if not recent:
                recent.extend(await self.build_signals(conn))
            return await rules.run(self.pool, self.prices, conn, recent, s.paper_start_cash)

        async def alerts_step():
            if not recent:
                recent.extend(await self.build_signals(conn))
            tg = alerts.Telegram(s.telegram_bot_token)
            try:
                res = await alerts.send_alerts(conn, tg, lambda _c: _async_value(recent), s.public_url)
                res.update(await alerts.send_digests(conn, tg, self.build_signals, s.public_url))
                return res
            finally:
                await tg.close()

        async def equity():
            return {"snapshots": await paper.snapshot_equity(conn)}

        await self._step(conn, "form4_feed", form4_feed)
        if s.backfill_days > 0:
            await self._step(conn, "backfill", backfill)
        await self._step(conn, "form4_queue", form4_queue)
        if s.form144_enabled:
            await self._step(conn, "form144_feed", form144_feed)
            await self._step(conn, "form144_queue", form144_queue)
        await self._step(conn, "ownership", ownership)
        if s.house_enabled:
            await self._step(conn, "house_index", house_index)
            await self._step(conn, "house_queue", house_queue)
        if s.quiver_api_key:
            await self._step(conn, "congress", congress_step)
        if s.amf_enabled:
            await self._step(conn, "amf_list", amf_list)
            await self._step(conn, "amf_queue", amf_queue)
        await self._step(conn, "prices", prices_step)
        await self._step(conn, "stops", stops)
        if self.build_signals:
            await self._step(conn, "rules", rule_step)
            if s.telegram_bot_token:
                await self._step(conn, "alerts", alerts_step)
        await self._step(conn, "equity", equity)

    # ------------------------------------------------------------ boucle lente

    async def _slow_steps(self, conn) -> None:
        s = self.s
        H = lambda n: timedelta(hours=n)  # noqa: E731
        M = lambda n: timedelta(minutes=n)  # noqa: E731

        await self._slow(conn, "tickers", H(24), lambda: _wrap("tickers", sec.refresh_tickers(self.sec, conn)))
        await self._slow(conn, "fundamentals", H(24), lambda: fundamentals.refresh(self.sec, conn))
        await self._slow(conn, "buybacks", H(2), lambda: buybacks.refresh_window(self.sec, conn, 560))
        await self._slow(conn, "earnings", M(55), lambda: earnings.refresh(conn, ahead_days=30, history_days=560))
        await self._slow(conn, "fx", H(6), lambda: euronext.refresh_fx(conn, self.euronext))
        if s.amf_enabled:
            await self._slow(conn, "eu_prices", M(25), lambda: euronext.refresh_prices(
                conn, self.euronext, backtest.store_history))
        if s.bafin_enabled:
            async def bafin_step():
                has = await _count(conn, "SELECT count(*) AS n FROM bafin_trades")
                return await bafin.refresh(conn, period=2 if has else 3)  # 1er passage : le dernier mois
            await self._slow(conn, "bafin", H(1), bafin_step)
        if s.senate_enabled:
            await self._slow(conn, "senate", H(1), lambda: senate.refresh(conn))
        if s.funds_13f:
            await self._slow(conn, "funds_13f", H(6), lambda: funds.refresh_all(self.sec, conn, s.funds_13f,
                                                                                 stop=self.stop))
            await self._slow(conn, "cusips", M(10), lambda: funds.map_cusips(conn))
        if s.finra_enabled:
            await self._slow(conn, "short_volume", M(60), lambda: finra.refresh(conn))
        if s.backtest_enabled:
            async def bt_import():
                res = await backtest.import_quarters(self.sec, conn, s.backtest_quarters)
                if res["have"] < res["available"]:  # trimestres restants : on repasse au prochain tour
                    res["_again"] = True
                return res
            if await self._due(conn, "bt_import", H(24)):
                await self._step(conn, "bt_import", bt_import)
                st = await job_info(conn, "bt_import")
                if (st["info"] or {}).get("_again"):
                    await conn.execute("UPDATE job_state SET last_run_at = NULL, info = info - '_again'"
                                       " WHERE name='bt_import'")
                    await conn.commit()
            await self._slow(conn, "bt_prices", M(9), lambda: backtest.refresh_histories(conn, self.history,
                                                                                          stop=self.stop))
            st = await job_info(conn, "bt_prices")
            fresh = (st["info"] or {}).get("fetched", 0)
            await self._slow(conn, "bt_report", H(6) if fresh else H(24), lambda: backtest.compute(conn))


async def _count(conn, sql: str) -> int:
    cur = await conn.execute(sql)
    n = (await cur.fetchone())["n"]
    await conn.commit()
    return n


async def _wrap(key: str, coro):
    return {key: await coro}


async def _async_value(v):
    return v

