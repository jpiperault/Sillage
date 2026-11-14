"""Backtest du score : que sont devenus les signaux d'initiés passés ?

1. Import des jeux de données trimestriels de la SEC (tous les Form 4, fichiers TSV dans un ZIP d'environ 11 Mo) :
   https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
   Une ligne par déclaration et par code (P achat, S vente), comme dans le flux live regroupé.
2. Cours de clôture quotidiens via l'API (non officielle) de Nasdaq, SPY compris (référence S&P 500).
3. Pour chaque signal : achat fictif à la première clôture qui suit la date de déclaration (le jour où l'on pouvait
   agir), puis performance à 21, 63 et 126 séances (~1, 3, 6 mois), en écart avec SPY sur la même période.
4. Rapport : performance moyenne (écrêtée), médiane et part de gagnants, par tranche de score et par critère.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import statistics
import time
import zipfile
from datetime import date, datetime, timedelta

import httpx

from . import log, scoring
from .ingest.fundamentals import cap_bucket
from .ingest.prices import BROWSER_UA

_log = log.get("backtest")

DATASETS_PAGE = "https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets"
HISTORY_URL = ("https://api.nasdaq.com/api/quote/{ticker}/historical?assetclass={asset}"
               "&fromdate={start}&todate={end}&limit=9999")
HORIZONS = (21, 63, 126)
BENCHMARK = "SPY"
SMALL_BENCHMARK = "IWM"  # Russell 2000 : référence des petites et moyennes valeurs
SMALL_CAP = 2e9          # sous 2 Md$ de capitalisation (ou inconnue) : comparaison au Russell 2000
HISTORY_YEARS = 2
HISTORY_REFRESH_DAYS = 7
HISTORY_BATCH = 60  # ~2,5 s par titre chez Nasdaq : environ 2,5 min par cycle
CLIP = (-1.0, 2.0)  # écrêtage des rendements pour la moyenne (actions à quelques cents qui font ×10)

csv.field_size_limit(10_000_000)


# ---------------------------------------------------------------- 1. import SEC


def list_quarters(page_html: str) -> list[tuple[str, str]]:
    """[(« 2026q2 », url absolue), …] du plus récent au plus ancien."""
    seen: dict[str, str] = {}
    for href, q in re.findall(r'href="([^"]*?/(\d{4}q[1-4])_form345\.zip)"', page_html):
        url = href if href.startswith("http") else "https://www.sec.gov" + href
        seen.setdefault(q, url)
    return sorted(seen.items(), reverse=True)


def _d(v: str) -> date | None:
    v = (v or "").strip()
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _f(v: str) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def _rows(z: zipfile.ZipFile, name: str):
    with z.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace"), delimiter="\t",
                                  quoting=csv.QUOTE_NONE)


def parse_dataset(content: bytes, quarter: str) -> list[tuple]:
    """Agrège les transactions non dérivées P et S par (déclaration, code)."""
    z = zipfile.ZipFile(io.BytesIO(content))
    subs: dict[str, dict] = {}
    for r in _rows(z, "SUBMISSION.tsv"):
        if r.get("DOCUMENT_TYPE") != "4":
            continue
        t = (r.get("ISSUERTRADINGSYMBOL") or "").strip().upper()
        subs[r["ACCESSION_NUMBER"]] = {
            "ticker": t if t and t not in ("NONE", "N/A", "NA") else None,
            "issuer_cik": int(r["ISSUERCIK"]) if (r.get("ISSUERCIK") or "").isdigit() else None,
            "filed": _d(r.get("FILING_DATE")),
            "plan": (r.get("AFF10B5ONE") or "").strip().lower() in ("1", "true"),
        }
    owners: dict[str, dict] = {}
    for r in _rows(z, "REPORTINGOWNER.tsv"):
        acc = r["ACCESSION_NUMBER"]
        if acc not in subs:
            continue
        if acc in owners:
            o = owners[acc]
            if not o["name"].endswith(" et al."):
                o["name"] += " et al."
            o["rel"] = ",".join(sorted(set(o["rel"].split(",")) | set((r.get("RPTOWNER_RELATIONSHIP") or "").split(","))))
            o["title"] = o["title"] or (r.get("RPTOWNER_TITLE") or "").strip() or None
            continue
        owners[acc] = {"cik": int(r["RPTOWNERCIK"]) if (r.get("RPTOWNERCIK") or "").isdigit() else None,
                       "name": (r.get("RPTOWNERNAME") or "").strip() or "Inconnu",
                       "rel": (r.get("RPTOWNER_RELATIONSHIP") or "").strip(),
                       "title": (r.get("RPTOWNER_TITLE") or "").strip() or None}
    agg: dict[tuple[str, str], dict] = {}
    for r in _rows(z, "NONDERIV_TRANS.tsv"):
        acc, code = r["ACCESSION_NUMBER"], (r.get("TRANS_CODE") or "").strip().upper()
        if code not in ("P", "S") or acc not in subs:
            continue
        shares, price = _f(r.get("TRANS_SHARES")), _f(r.get("TRANS_PRICEPERSHARE"))
        a = agg.setdefault((acc, code), {"shares": 0.0, "value": 0.0, "after": None, "date": None, "sk": -1})
        if shares:
            a["shares"] += shares
            if price:
                a["value"] += shares * price
        sk = int(r["NONDERIV_TRANS_SK"]) if (r.get("NONDERIV_TRANS_SK") or "").isdigit() else 0
        if sk > a["sk"]:  # dernière ligne : actions détenues après l'ensemble des transactions
            a["sk"], a["after"] = sk, _f(r.get("SHRS_OWND_FOLWNG_TRANS"))
        td = _d(r.get("TRANS_DATE"))
        if td and (a["date"] is None or td > a["date"]):
            a["date"] = td
    out = []
    for (acc, code), a in agg.items():
        s, o = subs[acc], owners.get(acc, {"cik": None, "name": "Inconnu", "rel": "", "title": None})
        if not s["ticker"] or not a["shares"]:
            continue
        price = a["value"] / a["shares"] if a["value"] else None
        out.append((acc, code, quarter, s["ticker"], s["issuer_cik"], o["cik"], o["name"], o["rel"], o["title"],
                    a["shares"], a["value"] or None, price, a["after"], a["date"], s["filed"], s["plan"]))
    return out


async def import_quarters(client, conn, max_quarters: int) -> dict:
    """Importe les trimestres manquants parmi les `max_quarters` plus récents."""
    resp = await client.get(DATASETS_PAGE)
    resp.raise_for_status()
    wanted = list_quarters(resp.text)[:max_quarters]
    cur = await conn.execute("SELECT quarter FROM bt_quarters")
    have = {r["quarter"] for r in await cur.fetchall()}
    await conn.commit()
    imported = []
    for q, url in wanted:
        if q in have:
            continue
        z = await client.get(url)
        z.raise_for_status()
        rows = await asyncio.to_thread(parse_dataset, z.content, q)
        async with conn.cursor() as c:
            await c.executemany(
                """INSERT INTO bt_signals (accession, code, quarter, ticker, issuer_cik, owner_cik, owner_name,
                     relationship, officer_title, shares, value, price, shares_after, trade_date, filed_date, plan_10b5_1)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", rows)
        await conn.execute("INSERT INTO bt_quarters (quarter, signals) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                           (q, len(rows)))
        await conn.commit()
        imported.append(q)
        _log.info("backtest : trimestre %s importé (%d signaux)", q, len(rows))
        break  # un trimestre par cycle : le cycle reste court
    # Trimestres sortis de la fenêtre : supprimés.
    keep = [q for q, _ in wanted]
    if keep:
        await conn.execute("DELETE FROM bt_signals WHERE quarter <> ALL(%s)", (keep,))
        await conn.execute("DELETE FROM bt_quarters WHERE quarter <> ALL(%s)", (keep,))
        await conn.commit()
    return {"imported": imported, "available": len(wanted), "have": len(have | set(imported))}


# ---------------------------------------------------------------- 2. cours historiques


def parse_history(data: dict) -> list[tuple[date, float]]:
    try:
        rows = data["data"]["tradesTable"]["rows"] or []
    except (KeyError, TypeError):
        return []
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%m/%d/%Y").date()
            c = float(str(r["close"]).replace("$", "").replace(",", ""))
        except (KeyError, ValueError, TypeError):
            continue
        if c > 0:
            out.append((d, c))
    out.sort()
    return out


class HistoryClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                                         timeout=httpx.Timeout(20.0, connect=8.0), transport=transport)
        self._last = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, ticker: str, start: date, end: date) -> list[tuple[date, float]]:
        for asset in ("stocks", "etf"):
            wait = self._last + 0.4 - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = await self._client.get(HISTORY_URL.format(ticker=ticker, asset=asset,
                                                                 start=start.isoformat(), end=end.isoformat()))
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                rows = parse_history(resp.json())
                if rows:
                    return rows
        return []


async def store_history(conn, ticker: str, rows: list[tuple[date, float]]) -> None:
    await conn.execute("DELETE FROM price_history WHERE ticker=%s", (ticker,))
    if rows:
        async with conn.cursor() as c:
            await c.executemany("INSERT INTO price_history (ticker, day, close, idx) VALUES (%s,%s,%s,%s)",
                                [(ticker, d, cl, i) for i, (d, cl) in enumerate(rows)])
    await conn.execute(
        "INSERT INTO price_history_state (ticker, fetched_at, rows, ok) VALUES (%s, now(), %s, %s)"
        " ON CONFLICT (ticker) DO UPDATE SET fetched_at=now(), rows=EXCLUDED.rows, ok=EXCLUDED.ok",
        (ticker, len(rows), bool(rows)))
    await conn.commit()


async def ensure_history(conn, client: HistoryClient, ticker: str, max_age_days: int = 1) -> bool:
    """Cours historiques d'un ticker (fiche action) : rechargés s'ils ont plus de `max_age_days` jours."""
    cur = await conn.execute("SELECT fetched_at > now() - make_interval(days => %s) AS fresh FROM price_history_state"
                             " WHERE ticker=%s", (max_age_days, ticker))
    r = await cur.fetchone()
    await conn.commit()
    if r and r["fresh"]:
        return False
    end = date.today()
    await store_history(conn, ticker, await client.fetch(ticker, end - timedelta(days=365 * HISTORY_YEARS), end))
    return True


async def refresh_histories(conn, client: HistoryClient, limit: int = HISTORY_BATCH,
                            stop: asyncio.Event | None = None) -> dict:
    cur = await conn.execute(
        """SELECT t.ticker FROM (
             SELECT %s AS ticker, 1000000 AS n
             UNION ALL SELECT 'IWM', 1000000
             UNION ALL SELECT ticker, count(*) FROM bt_signals WHERE code='P' GROUP BY ticker
             UNION ALL SELECT ticker, count(*) FROM congress_trades WHERE ticker IS NOT NULL GROUP BY ticker) t
           LEFT JOIN price_history_state s USING (ticker)
           WHERE s.ticker IS NULL
              OR (s.fetched_at < now() - make_interval(days => %s)
                  -- rechargement utile seulement si un rendement à 6 mois reste à mesurer
                  AND (t.ticker IN (%s, 'IWM') OR EXISTS (SELECT 1 FROM bt_signals b WHERE b.ticker = t.ticker
                                                AND b.filed_date > current_date - 200)
                       OR EXISTS (SELECT 1 FROM congress_trades c WHERE c.ticker = t.ticker
                                  AND c.report_date > current_date - 200)))
           ORDER BY t.n DESC LIMIT %s""",
        (BENCHMARK, HISTORY_REFRESH_DAYS, BENCHMARK, limit))
    tickers = [r["ticker"] for r in await cur.fetchall()]
    await conn.commit()
    end = date.today()
    ok = 0
    for t in tickers:
        if stop is not None and stop.is_set():
            break
        rows = await client.fetch(t, end - timedelta(days=365 * HISTORY_YEARS), end)
        await store_history(conn, t, rows)
        ok += 1 if rows else 0
    cur = await conn.execute(
        """SELECT count(*) AS n FROM (SELECT DISTINCT ticker FROM bt_signals WHERE code='P') t
           LEFT JOIN price_history_state s USING (ticker) WHERE s.ticker IS NULL""")
    remaining = (await cur.fetchone())["n"]
    await conn.commit()
    return {"fetched": len(tickers), "with_data": ok, "remaining": remaining}


# ---------------------------------------------------------------- 3. calcul et rapport

OUTCOMES_SQL = """
SELECT s.*, e.close AS entry, e.day AS entry_day, cs.shares * e.close AS market_cap, bm.bench,
       x1.close AS c1, x2.close AS c2, x3.close AS c3,
       se.close AS spy_entry, s1.close AS spy1, s2.close AS spy2, s3.close AS spy3,
       (SELECT count(DISTINCT coalesce(b.owner_cik::text, b.owner_name)) FROM bt_signals b
         WHERE b.code='P' AND b.ticker=s.ticker AND b.trade_date BETWEEN s.trade_date - 30 AND s.trade_date) AS cluster,
       EXISTS (SELECT 1 FROM buybacks bb WHERE bb.cik = s.issuer_cik
                 AND bb.filed BETWEEN s.filed_date - 90 AND s.filed_date) AS buyback,
       EXISTS (SELECT 1 FROM earnings er WHERE er.ticker = s.ticker
                 AND er.day BETWEEN s.filed_date AND s.filed_date + 30) AS earnings_soon,
       EXISTS (SELECT 1 FROM earnings_days_done ed WHERE ed.day BETWEEN s.filed_date AND s.filed_date + 30)
         AS earnings_known
FROM bt_signals s
JOIN LATERAL (SELECT idx, close, day FROM price_history p WHERE p.ticker=s.ticker AND p.day > s.filed_date
              ORDER BY p.day LIMIT 1) e ON true
LEFT JOIN company_shares cs ON cs.cik = s.issuer_cik
CROSS JOIN LATERAL (SELECT CASE WHEN cs.shares * e.close >= %(small_cap)s THEN %(bench)s ELSE %(small)s END
                    AS bench) bm
JOIN LATERAL (SELECT idx, close FROM price_history p WHERE p.ticker=bm.bench AND p.day >= e.day
              ORDER BY p.day LIMIT 1) se ON true
LEFT JOIN price_history x1 ON x1.ticker=s.ticker AND x1.idx=e.idx+%(h1)s
LEFT JOIN price_history x2 ON x2.ticker=s.ticker AND x2.idx=e.idx+%(h2)s
LEFT JOIN price_history x3 ON x3.ticker=s.ticker AND x3.idx=e.idx+%(h3)s
LEFT JOIN price_history s1 ON s1.ticker=bm.bench AND s1.idx=se.idx+%(h1)s
LEFT JOIN price_history s2 ON s2.ticker=bm.bench AND s2.idx=se.idx+%(h2)s
LEFT JOIN price_history s3 ON s3.ticker=bm.bench AND s3.idx=se.idx+%(h3)s
WHERE s.filed_date IS NOT NULL AND s.trade_date IS NOT NULL
"""


def score_row(r: dict) -> tuple[int, list[str]]:
    rel = (r["relationship"] or "").lower()
    delay = (r["filed_date"] - r["trade_date"]).days if r["filed_date"] and r["trade_date"] else None
    return scoring.insider_score(
        code=r["code"], officer_title=r["officer_title"], is_officer="officer" in rel, is_director="director" in rel,
        is_ten_percent="tenpercent" in rel, value=float(r["value"] or 0), delay_days=delay,
        cluster=int(r["cluster"] or 0) if r["code"] == "P" else 0, plan_10b5_1=r["plan_10b5_1"], price_change=None)


def position_change(code: str, shares: float | None, after: float | None) -> float | None:
    """Achat : hausse de la ligne en % (100 = la position double). Vente : part de la ligne vendue en %."""
    if not shares or after is None:
        return None
    if code == "P":
        before = after - shares
        return shares / before * 100 if before > 0 else None  # première ligne : non calculable
    total = after + shares
    return shares / total * 100 if total > 0 else None


def _bucket(v, edges: list[tuple[float, str]], default: str) -> str:
    for limit, label in edges:
        if v is not None and v < limit:
            return label
    return default


def dimensions(r: dict, score: int) -> dict[str, str]:
    title = r["officer_title"] or ""
    rel = (r["relationship"] or "").lower()
    value = float(r["value"] or 0)
    delay = (r["filed_date"] - r["trade_date"]).days
    pos = position_change(r["code"], float(r["shares"] or 0), float(r["shares_after"]) if r["shares_after"] is not None else None)
    entry = float(r["entry"])
    return {
        "Score": _bucket(score, [(40, "0-39"), (55, "40-54"), (65, "55-64"), (75, "65-74"), (85, "75-84")], "85-100"),
        "Rôle": ("Dirigeant de premier plan" if scoring._TOP_TITLE.search(title) else
                 "Autre dirigeant ou administrateur" if ("officer" in rel or "director" in rel) else
                 "Actionnaire > 10 %" if "tenpercent" in rel else "Autre"),
        "Montant": _bucket(value, [(50_000, "< 50 K$"), (250_000, "50-250 K$"), (1_000_000, "250 K$-1 M$")], "≥ 1 M$"),
        "Délai de déclaration": _bucket(delay, [(3, "≤ 2 j"), (6, "3-5 j")], "> 5 j"),
        "Cluster (30 j)": ("3 initiés ou plus" if (r["cluster"] or 0) >= 3 else "2 initiés" if (r["cluster"] or 0) == 2
                           else "seul"),
        "Plan 10b5-1": "oui" if r["plan_10b5_1"] else "non",
        "Taille vs position": ("première ligne / inconnue" if pos is None else
                               _bucket(pos, [(10, "+ < 10 %"), (50, "+10-50 %"), (100, "+50-100 %")], "+ ≥ 100 %")
                               if r["code"] == "P" else
                               _bucket(pos, [(10, "< 10 % vendus"), (50, "10-50 % vendus"), (100, "50-99 % vendus")],
                                       "tout vendu")),
        "Prix d'entrée": "< 5 $" if entry < 5 else "≥ 5 $",
        "Capitalisation": cap_bucket(float(r["market_cap"]) if r.get("market_cap") else None),
        "Rachat d'actions annoncé (90 j)": "oui" if r.get("buyback") else "non",
        "Résultats dans les 30 j": ("inconnu" if not r.get("earnings_known") else
                                    "oui" if r.get("earnings_soon") else "non"),
        "Track record de l'initié": r.get("track_label") or "aucun historique",
    }


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    clipped = [min(max(v, CLIP[0]), CLIP[1]) for v in values]
    return {"n": len(values), "mean": round(statistics.fmean(clipped) * 100, 2),
            "median": round(statistics.median(values) * 100, 2),
            "win": round(sum(1 for v in values if v > 0) / len(values) * 100, 1)}


def excess_returns(r: dict) -> dict[int, float]:
    """Écart de performance avec la référence (SPY ou IWM selon la taille) à chaque horizon disponible."""
    entry, ref0 = float(r["entry"]), float(r["spy_entry"])
    out = {}
    for h, c, s in zip(HORIZONS, ("c1", "c2", "c3"), ("spy1", "spy2", "spy3")):
        if r[c] is not None and r[s] is not None:
            out[h] = (float(r[c]) / entry - 1) - (float(r[s]) / ref0 - 1)
    return out


TRACK_MIN = 2  # au moins 2 achats passés mesurés pour parler de « track record »


def annotate_track(rows: list[dict]) -> None:
    """Ajoute à chaque achat le track record de l'initié AVANT ce signal (pas de fuite d'information future)."""
    history: dict[str, list[tuple]] = {}
    for r in sorted((r for r in rows if r["code"] == "P"), key=lambda r: r["filed_date"]):
        key = str(r["owner_cik"] or r["owner_name"])
        # seuls les achats dont le rendement à 3 mois était connu à la date du signal comptent
        past = [x for (d_known, x) in history.get(key, []) if d_known <= r["filed_date"]]
        if len(past) >= TRACK_MIN:
            wins = sum(1 for x in past if x > 0) / len(past)
            r["track_label"] = "favorable (≥ 60 % gagnants)" if wins >= 0.6 else (
                "défavorable (≤ 40 % gagnants)" if wins <= 0.4 else "neutre")
            r["track_wins"] = wins
        ex = excess_returns(r).get(63)
        if ex is not None:
            history.setdefault(key, []).append((r["filed_date"] + timedelta(days=95), ex))


def build_report(rows: list[dict]) -> dict:
    """rows : résultats d'OUTCOMES_SQL. Performances en écart avec la référence de marché adaptée à la taille."""
    groups: dict[tuple[str, str, str], dict[int, list[float]]] = {}
    filed = []
    annotate_track(rows)
    for r in rows:
        score, _ = score_row(r)
        filed.append(r["filed_date"])
        excess = excess_returns(r)
        if not excess:
            continue
        side = "Achats (P)" if r["code"] == "P" else "Ventes (S)"
        for dim, label in [("Tous", "tous")] + list(dimensions(r, score).items()):
            g = groups.setdefault((side, dim, label), {h: [] for h in HORIZONS})
            for h, v in excess.items():
                g[h].append(v)
    out = []
    for (side, dim, label), per_h in groups.items():
        out.append({"side": side, "dimension": dim, "label": label,
                    "stats": {str(h): _stats(v) for h, v in per_h.items()}})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "horizons": list(HORIZONS),
        "benchmark": f"{BENCHMARK} (≥ 2 Md$) / {SMALL_BENCHMARK} (< 2 Md$ ou inconnue)",
        "signals": len(filed),
        "period": [min(filed).isoformat(), max(filed).isoformat()] if filed else None,
        "groups": out,
    }


async def outcome_rows(conn) -> list[dict]:
    params = {"bench": BENCHMARK, "small": SMALL_BENCHMARK, "small_cap": SMALL_CAP,
              "h1": HORIZONS[0], "h2": HORIZONS[1], "h3": HORIZONS[2]}
    rows = []
    async with conn.cursor(name="bt_outcomes") as cur:
        await cur.execute(OUTCOMES_SQL, params)
        while batch := await cur.fetchmany(5000):
            rows.extend(batch)
    await conn.commit()
    return rows


async def compute(conn) -> dict:
    """Rapport + track records des initiés + entraînement du score v2 (un seul passage sur les données)."""
    from . import model as score_model  # import local : model importe backtest

    rows = await outcome_rows(conn)
    report = await asyncio.to_thread(build_report, rows)  # annote aussi le track record (annotate_track)
    await conn.execute("INSERT INTO bt_reports (report) VALUES (%s::jsonb)", (json_dumps(report),))
    await conn.execute("DELETE FROM bt_reports WHERE id NOT IN (SELECT id FROM bt_reports ORDER BY id DESC LIMIT 5)")
    tracks = insider_tracks(rows)
    await conn.execute("DELETE FROM track_records WHERE kind='insider'")
    async with conn.cursor() as c:
        await c.executemany(
            "INSERT INTO track_records (kind, person_key, name, n, wins, mean_63, median_63, last_trade)"
            " VALUES ('insider',%s,%s,%s,%s,%s,%s,%s)", tracks)
    await conn.commit()
    fitted = await asyncio.to_thread(score_model.fit, rows)
    if fitted:
        await conn.execute("UPDATE score_models SET active=false WHERE side='P'")
        await conn.execute(
            "INSERT INTO score_models (side, model, metrics, active) VALUES ('P', %s::jsonb, %s::jsonb, %s)",
            (json_dumps(fitted["model"]), json_dumps(fitted["metrics"]), fitted["metrics"]["validated"]))
        await conn.execute("DELETE FROM score_models WHERE id NOT IN (SELECT id FROM score_models ORDER BY id DESC LIMIT 10)")
        await conn.commit()
    congress = await congress_tracks(conn)
    return {"signals": report["signals"], "groups": len(report["groups"]), "insider_tracks": len(tracks),
            "congress_tracks": congress,
            "model": (fitted or {}).get("metrics", {}).get("auc"), "validated": (fitted or {}).get("metrics", {}).get("validated")}


def insider_tracks(rows: list[dict]) -> list[tuple]:
    per: dict[str, dict] = {}
    for r in rows:
        if r["code"] != "P":
            continue
        ex = excess_returns(r).get(63)
        if ex is None:
            continue
        key = str(r["owner_cik"] or r["owner_name"])
        p = per.setdefault(key, {"name": r["owner_name"], "x": [], "last": r["trade_date"]})
        p["x"].append(ex)
        p["last"] = max(p["last"], r["trade_date"])
    out = []
    for key, p in per.items():
        xs = sorted(p["x"])
        clipped = [min(max(v, CLIP[0]), CLIP[1]) for v in xs]
        out.append((key, p["name"], len(xs), sum(1 for v in xs if v > 0), round(statistics.fmean(clipped) * 100, 2),
                    round(statistics.median(xs) * 100, 2), p["last"]))
    return out


CONGRESS_SQL = """
SELECT c.politician, c.trade_date, e.close AS entry, x.close AS c63, se.close AS b0, sx.close AS b63
FROM congress_trades c
JOIN LATERAL (SELECT idx, close, day FROM price_history p WHERE p.ticker=c.ticker AND p.day > coalesce(c.report_date, c.trade_date)
              ORDER BY p.day LIMIT 1) e ON true
JOIN LATERAL (SELECT idx, close FROM price_history p WHERE p.ticker=%(bench)s AND p.day >= e.day ORDER BY p.day LIMIT 1) se ON true
JOIN price_history x ON x.ticker=c.ticker AND x.idx=e.idx+63
JOIN price_history sx ON sx.ticker=%(bench)s AND sx.idx=se.idx+63
WHERE c.side='buy' AND c.ticker IS NOT NULL
"""


async def congress_tracks(conn) -> int:
    """Track record des élus : achats déclarés comparés au S&P 500 à 3 mois (achat à la déclaration)."""
    cur = await conn.execute(CONGRESS_SQL, {"bench": BENCHMARK})
    per: dict[str, dict] = {}
    for r in await cur.fetchall():
        ex = (float(r["c63"]) / float(r["entry"]) - 1) - (float(r["b63"]) / float(r["b0"]) - 1)
        p = per.setdefault(r["politician"], {"x": [], "last": r["trade_date"]})
        p["x"].append(ex)
        if r["trade_date"] and (p["last"] is None or r["trade_date"] > p["last"]):
            p["last"] = r["trade_date"]
    await conn.execute("DELETE FROM track_records WHERE kind='congress'")
    rows = []
    for name, p in per.items():
        xs = sorted(p["x"])
        clipped = [min(max(v, CLIP[0]), CLIP[1]) for v in xs]
        rows.append((name, name, len(xs), sum(1 for v in xs if v > 0), round(statistics.fmean(clipped) * 100, 2),
                     round(statistics.median(xs) * 100, 2), p["last"]))
    async with conn.cursor() as c:
        await c.executemany(
            "INSERT INTO track_records (kind, person_key, name, n, wins, mean_63, median_63, last_trade)"
            " VALUES ('congress',%s,%s,%s,%s,%s,%s,%s)", rows)
    await conn.commit()
    return len(rows)


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, default=str, ensure_ascii=False)
