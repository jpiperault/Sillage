"""Tester une règle sur l'historique : rejeu jour par jour des achats d'initiés du backtest.

Pour chaque achat dont le score atteint le seuil : achat à la première clôture qui suit la déclaration (le jour où
l'on pouvait agir), avec une part fixe de la valeur du portefeuille ; vente au stop (clôture sous −X %), à l'horizon
(jours calendaires) ou à la fin de la période. Frais appliqués à l'achat et à la vente. Comparaison avec le S&P 500.

Score v2 : le modèle a été entraîné sur les 70 % de signaux les plus anciens ; pour ne pas tricher, la simulation
en v2 ne porte que sur la période de test (signaux jamais vus par le modèle).
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta

from . import backtest, model as score_model


async def load(conn, score: str) -> tuple[list[dict], dict | None]:
    rows = await backtest.outcome_rows(conn)
    backtest.annotate_track(rows)
    mdl = None
    if score == "v2":
        cur = await conn.execute("SELECT model, metrics FROM score_models WHERE side='P' ORDER BY id DESC LIMIT 1")
        mdl = await cur.fetchone()
        await conn.commit()
    return [r for r in rows if r["code"] == "P"], mdl


def score_signals(rows: list[dict], score: str, mdl: dict | None) -> tuple[list[dict], date | None]:
    out, start = [], None
    if score == "v2" and mdl:
        start = date.fromisoformat(mdl["metrics"]["test_period"][0])
    for r in rows:
        if score == "v2":
            if not mdl or r["filed_date"] < start:
                continue
            s = score_model.percentile(mdl["model"], score_model.predict(mdl["model"], score_model.row_features(r)))
        else:
            s, _ = backtest.score_row(r)
        out.append({"ticker": r["ticker"], "entry_day": r["entry_day"], "score": s, "person": r["owner_name"]})
    return out, start


async def price_paths(conn, tickers: list[str], since: date) -> dict[str, tuple[list[date], list[float]]]:
    paths: dict[str, tuple[list[date], list[float]]] = {}
    async with conn.cursor(name="sim_prices") as cur:
        await cur.execute("SELECT ticker, day, close FROM price_history WHERE ticker = ANY(%s) AND day >= %s"
                          " ORDER BY ticker, day", (tickers, since))
        while batch := await cur.fetchmany(20000):
            for r in batch:
                d, c = paths.setdefault(r["ticker"], ([], []))
                d.append(r["day"])
                c.append(float(r["close"]))
    await conn.commit()
    return paths


def _price_on(path, day: date) -> float | None:
    days, closes = path
    i = bisect.bisect_right(days, day) - 1
    return closes[i] if i >= 0 else None


def run(signals: list[dict], paths: dict, bench: tuple, *, min_score: int, alloc_pct: float, hold_days: int,
        max_positions: int, stop_pct: float = 8.0, cost_pct: float = 0.1, start_cash: float = 100_000.0) -> dict:
    picks = sorted((s for s in signals if s["score"] >= min_score and s["ticker"] in paths), key=lambda s: s["entry_day"])
    if not picks:
        return {"error": "Aucun signal ne passe ces critères sur la période."}
    cal = [d for d in bench[0] if d >= picks[0]["entry_day"]]
    by_day: dict[date, list[dict]] = {}
    for s in picks:
        by_day.setdefault(s["entry_day"], []).append(s)
    cash, positions, trades, curve = start_cash, {}, [], []
    fee = cost_pct / 100
    peak, max_dd, exposure = start_cash, 0.0, []
    for day in cal:
        # 1. sorties : stop puis horizon
        for t in list(positions):
            p = positions[t]
            px = _price_on(paths[t], day)
            if px is None:
                continue
            reason = None
            if px <= p["entry"] * (1 - stop_pct / 100):
                reason = "stop"
            elif (day - p["day"]).days >= hold_days:
                reason = "horizon"
            if reason:
                cash += p["shares"] * px * (1 - fee)
                trades.append({"ticker": t, "entry_day": p["day"], "exit_day": day, "ret": px / p["entry"] - 1,
                               "reason": reason, "score": p["score"]})
                del positions[t]
        equity = cash + sum(p["shares"] * (_price_on(paths[t], day) or p["entry"]) for t, p in positions.items())
        # 2. entrées du jour (meilleurs scores d'abord)
        for s in sorted(by_day.get(day, []), key=lambda s: s["score"], reverse=True):
            if s["ticker"] in positions or len(positions) >= max_positions:
                continue
            px = _price_on(paths[s["ticker"]], day)
            amount = equity * alloc_pct / 100
            if not px or amount > cash or amount <= 0:
                continue
            cash -= amount
            positions[s["ticker"]] = {"shares": amount * (1 - fee) / px, "entry": px, "day": day, "score": s["score"]}
        equity = cash + sum(p["shares"] * (_price_on(paths[t], day) or p["entry"]) for t, p in positions.items())
        peak = max(peak, equity)
        max_dd = max(max_dd, 1 - equity / peak)
        exposure.append(1 - cash / equity if equity else 0)
        curve.append((day, equity))
    # fin de période : positions valorisées au dernier cours (non vendues)
    b0 = _price_on(bench, cal[0])
    b1 = _price_on(bench, cal[-1])
    final = curve[-1][1]
    wins = [t for t in trades if t["ret"] > 0]
    step = max(1, len(curve) // 120)
    return {
        "period": [cal[0], cal[-1]], "signals": len(picks), "trades": len(trades), "open": len(positions),
        "total_pct": round((final / start_cash - 1) * 100, 2),
        "bench_pct": round((b1 / b0 - 1) * 100, 2) if b0 and b1 else None,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_trade_pct": round(sum(t["ret"] for t in trades) / len(trades) * 100, 2) if trades else None,
        "exposure_pct": round(sum(exposure) / len(exposure) * 100, 1) if exposure else 0,
        "exits": {k: sum(1 for t in trades if t["reason"] == k) for k in ("stop", "horizon")},
        "curve": [{"d": d, "pct": round((e / start_cash - 1) * 100, 2),
                   "spy_pct": round(((_price_on(bench, d) or b0) / b0 - 1) * 100, 2) if b0 else None}
                  for i, (d, e) in enumerate(curve) if i % step == 0 or i == len(curve) - 1],
        "best": sorted(trades, key=lambda t: t["ret"], reverse=True)[:5],
        "worst": sorted(trades, key=lambda t: t["ret"])[:5],
    }


async def simulate(conn, *, score: str, min_score: int, alloc_pct: float, hold_days: int, max_positions: int,
                   stop_pct: float = 8.0, cost_pct: float = 0.1) -> dict:
    rows, mdl = await load(conn, score)
    if score == "v2" and not mdl:
        return {"error": "Pas encore de modèle v2 : le backtest doit d'abord être calculé."}
    signals, test_start = score_signals(rows, score, mdl)
    tickers = sorted({s["ticker"] for s in signals if s["score"] >= min_score})
    if not tickers:
        return {"error": "Aucun signal ne passe ces critères sur la période."}
    since = min(s["entry_day"] for s in signals) - timedelta(days=5)
    paths = await price_paths(conn, tickers + [backtest.BENCHMARK], since)
    bench = paths.pop(backtest.BENCHMARK, None)
    if not bench:
        return {"error": "Cours du S&P 500 indisponibles."}
    import asyncio
    res = await asyncio.to_thread(run, signals, paths, bench, min_score=min_score, alloc_pct=alloc_pct,
                                  hold_days=hold_days, max_positions=max_positions, stop_pct=stop_pct,
                                  cost_pct=cost_pct)
    res["score"] = score
    res["test_only_from"] = test_start
    return res
