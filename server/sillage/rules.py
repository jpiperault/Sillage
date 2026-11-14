"""Règle automatique en fictif : copie des signaux au-dessus d'un score, sortie à horizon ou au stop (−8 %).

Tourne à chaque cycle de collecte, sur le portefeuille « rule » de chaque compte qui l'a activée.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import alerts, log, paper
from .ingest import prices

_log = log.get("rules")
ISIN_RE = r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


async def quote_usd(pool, price_client, conn, ticker: str) -> tuple[float | None, str]:
    """Cours en dollars : ticker US via Yahoo/Nasdaq ; ISIN européen via le dernier cours Euronext × taux BCE."""
    if len(ticker) == 12 and ticker[:2].isalpha() and ticker[-1].isdigit():
        cur = await conn.execute(f"SELECT {paper.PRICE_USD_SQL} AS usd FROM prices pr WHERE pr.ticker=%s", (ticker,))
        r = await cur.fetchone()
        await conn.commit()
        return (float(r["usd"]) if r and r["usd"] is not None else None), "EUR"
    q = await prices.get_price(pool, price_client, ticker)
    return (q["price"] if q else None), "USD"


def signal_symbol(s: dict) -> str | None:
    return s.get("ticker") or s.get("isin")


async def run(pool, price_client, conn, recent_signals: list[dict], start_cash: float) -> dict:
    cur = await conn.execute("SELECT * FROM paper_rules WHERE enabled")
    rules = await cur.fetchall()
    await conn.commit()
    stats = {"rules": len(rules), "bought": 0, "sold": 0, "skipped": 0}
    if not rules:
        return stats
    now = datetime.now(timezone.utc)
    for rule in rules:
        uid = rule["user_id"]
        # 1. sorties à horizon
        cur = await conn.execute(
            "SELECT ticker FROM paper_positions WHERE user_id=%s AND book='rule'"
            " AND opened_at < now() - make_interval(days => %s)", (uid, rule["hold_days"]))
        for p in await cur.fetchall():
            px, _ = await quote_usd(pool, price_client, conn, p["ticker"])
            if px:
                await paper.sell_all(conn, uid, p["ticker"], px, reason="horizon", book="rule",
                                     note=f"horizon de {rule['hold_days']} jours atteint")
                await conn.commit()
                stats["sold"] += 1
        # 2. nouveaux signaux
        new_ids = await alerts.new_signal_ids(conn, rule["last_check"])
        sources = set(rule["sources"].split(","))
        picks = [s for s in recent_signals if s["id"] in new_ids and s["side"] == "buy"
                 and s["source"] in sources and s["score"] >= rule["min_score"] and signal_symbol(s)]
        picks.sort(key=lambda s: s["score"], reverse=True)
        for s in picks:
            sym = signal_symbol(s)
            cur = await conn.execute("SELECT count(*) AS n, bool_or(ticker=%s) AS held FROM paper_positions"
                                     " WHERE user_id=%s AND book='rule'", (sym, uid))
            r = await cur.fetchone()
            if r["held"] or r["n"] >= rule["max_positions"]:
                stats["skipped"] += 1
                continue
            px, currency = await quote_usd(pool, price_client, conn, sym)
            summ = await paper.summary(conn, uid, start_cash, "rule")
            if not px:
                stats["skipped"] += 1
                continue
            qty = math.floor(summ["equity"] * float(rule["alloc_pct"]) / 100 / px)
            if qty < 1:
                stats["skipped"] += 1
                continue
            try:
                await paper.buy(conn, uid, sym, qty, px, start_cash, signal_ref=s["id"], book="rule", reason="rule",
                                currency=currency, note=f"score {s['score']} — {s['person']}")
                await conn.commit()
                stats["bought"] += 1
            except paper.PaperError as exc:
                await conn.rollback()
                _log.info("règle de %s : %s non acheté (%s)", uid, sym, exc)
                stats["skipped"] += 1
        await conn.execute("UPDATE paper_rules SET last_check=%s WHERE user_id=%s", (now, uid))
        await conn.commit()
    return stats
