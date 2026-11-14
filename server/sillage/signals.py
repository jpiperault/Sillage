"""Construction des signaux scorés (initiés US, élus, France AMF, Allemagne BaFin).

Score v1 : points fixes (scoring.py). Score v2 : probabilité de battre sa référence de marché à 3 mois, calculée par
le modèle entraîné sur le backtest (model.py), affichée en rang centile ; utilisé pour les achats d'initiés US
quand le modèle est validé (réglage « Score affiché » : auto / v1 / v2).
"""
from __future__ import annotations

import time

from . import backtest, model as score_model, scoring
from .ingest import fundamentals

ROLE_LABEL = {"P": "buy", "S": "sell", "M": "option"}
ISIN_RE = "^[A-Z]{2}[A-Z0-9]{9}[0-9]$"


def _f(x):
    return float(x) if x is not None else None


# ---------------------------------------------------------------- modèle actif (cache 10 min)

_model_cache: dict = {"at": 0.0, "row": None}


async def active_model(conn) -> dict | None:
    if time.monotonic() - _model_cache["at"] > 600:
        cur = await conn.execute("SELECT id, model, metrics, active, created_at FROM score_models WHERE side='P'"
                                 " ORDER BY id DESC LIMIT 1")
        _model_cache["row"] = await cur.fetchone()
        _model_cache["at"] = time.monotonic()
    return _model_cache["row"]


def reset_model_cache() -> None:
    _model_cache["at"] = 0.0


def use_v2(row: dict | None, version: str) -> bool:
    if row is None or version == "v1":
        return False
    return version == "v2" or bool(row["active"])


# ---------------------------------------------------------------- enrichissements groupés

async def _insider_clusters(conn, tickers: list[str]) -> dict[str, int]:
    if not tickers:
        return {}
    cur = await conn.execute(
        "SELECT ticker, count(DISTINCT coalesce(owner_cik::text, owner_name)) AS n FROM insider_trades"
        " WHERE code='P' AND amended_by IS NULL AND ticker = ANY(%s)"
        " AND coalesce(trade_date, filed_date) >= current_date - 30 GROUP BY ticker",
        (tickers,),
    )
    return {r["ticker"]: r["n"] for r in await cur.fetchall()}


async def _congress_clusters(conn, tickers: list[str]) -> dict[str, int]:
    if not tickers:
        return {}
    cur = await conn.execute(
        "SELECT ticker, count(DISTINCT politician) AS n FROM congress_trades WHERE side='buy'"
        " AND ticker = ANY(%s) AND coalesce(report_date, trade_date) >= current_date - 30 GROUP BY ticker",
        (tickers,),
    )
    return {r["ticker"]: r["n"] for r in await cur.fetchall()}


async def _price_map(conn, tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    cur = await conn.execute("SELECT ticker, price FROM prices WHERE ticker = ANY(%s)", (tickers,))
    return {r["ticker"]: float(r["price"]) for r in await cur.fetchall()}


async def _context(conn, rows: list[dict]) -> dict:
    """Capitalisation, rachats, résultats à venir et track record pour un lot de lignes d'initiés."""
    ciks = sorted({r["issuer_cik"] for r in rows if r.get("issuer_cik")})
    owners = sorted({str(r["owner_cik"]) for r in rows if r.get("owner_cik")})
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    ctx = {"shares": {}, "buyback": {}, "earnings": {}, "track": {}}
    if ciks:
        cur = await conn.execute("SELECT cik, shares FROM company_shares WHERE cik = ANY(%s)", (ciks,))
        ctx["shares"] = {r["cik"]: float(r["shares"]) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT cik, max(filed) AS d FROM buybacks WHERE cik = ANY(%s)"
                                 " AND filed >= current_date - 120 GROUP BY cik", (ciks,))
        ctx["buyback"] = {r["cik"]: r["d"] for r in await cur.fetchall()}
    if tickers:
        cur = await conn.execute("SELECT ticker, min(day) AS d FROM earnings WHERE ticker = ANY(%s)"
                                 " AND day >= current_date - 30 GROUP BY ticker", (tickers,))
        ctx["earnings"] = {r["ticker"]: r["d"] for r in await cur.fetchall()}
    if owners:
        cur = await conn.execute("SELECT * FROM track_records WHERE kind='insider' AND person_key = ANY(%s)",
                                 (owners,))
        ctx["track"] = {r["person_key"]: r for r in await cur.fetchall()}
    return ctx


async def congress_tracks(conn, names: list[str]) -> dict[str, dict]:
    if not names:
        return {}
    cur = await conn.execute("SELECT * FROM track_records WHERE kind='congress' AND person_key = ANY(%s)", (names,))
    return {r["person_key"]: r for r in await cur.fetchall()}


def track_text(t: dict | None, what: str = "achats") -> str | None:
    if not t or not t["n"]:
        return None
    return (f"Historique : {t['n']} {what} mesurés, {round(t['wins'] / t['n'] * 100)} % ont battu le marché à 3 mois"
            f" (médiane {float(t['median_63']):+.1f} %)").replace(".", ",")


# ---------------------------------------------------------------- initiés US

def _insider_role(r: dict) -> str:
    parts = []
    if r["officer_title"]:
        parts.append(r["officer_title"])
    elif r["is_officer"]:
        parts.append("Dirigeant")
    if r["is_director"]:
        parts.append("Administrateur")
    if r["is_ten_percent"]:
        parts.append("Actionnaire > 10 %")
    return ", ".join(parts) or "Initié"


def _role_bucket(r: dict) -> str:
    if r["officer_title"] and scoring._TOP_TITLE.search(r["officer_title"]):
        return "Dirigeant de premier plan"
    if r["is_officer"] or r["is_director"]:
        return "Autre dirigeant ou administrateur"
    if r["is_ten_percent"]:
        return "Actionnaire > 10 %"
    return "Autre"


def insider_signal(r: dict, cluster: int, current: float | None, ctx: dict | None = None,
                   mdl: dict | None = None) -> dict:
    ctx = ctx or {"shares": {}, "buyback": {}, "earnings": {}, "track": {}}
    delay = scoring.days_between(r["trade_date"], r["filed_date"])
    price = _f(r["price"])
    change = (current / price - 1) if current and price else None
    score, reasons = scoring.insider_score(
        code=r["code"], officer_title=r["officer_title"], is_officer=r["is_officer"], is_director=r["is_director"],
        is_ten_percent=r["is_ten_percent"], value=_f(r["value"]), delay_days=delay,
        cluster=cluster if r["code"] == "P" else 0, plan_10b5_1=r["plan_10b5_1"], price_change=change,
    )
    score_v1 = score
    after = _f(r.get("shares_after"))
    pos = backtest.position_change(r["code"], _f(r["shares"]), after)
    shares_out = ctx["shares"].get(r.get("issuer_cik"))
    ref_price = current or price
    market_cap = shares_out * ref_price if shares_out and ref_price else None
    buyback = ctx["buyback"].get(r.get("issuer_cik"))
    earn = ctx["earnings"].get(r.get("ticker"))
    track = ctx["track"].get(str(r.get("owner_cik"))) if r.get("owner_cik") else None
    earnings_soon = bool(earn and r["filed_date"] and 0 <= (earn - r["filed_date"]).days <= 30)

    info = []
    if r["code"] == "P":
        if pos is None and after is not None:
            info.append("Info : première ligne dans cette société")
        elif pos is not None:
            info.append(f"Info : ligne augmentée de {pos:.0f} %")
    elif r["code"] == "S" and pos is not None:
        info.append(f"Info : {pos:.0f} % de sa ligne vendue")
    if market_cap and _f(r["value"]):
        info.append(f"Info : capitalisation {fundamentals.cap_bucket(market_cap)}, l'opération représente "
                    f"{_f(r['value']) / market_cap * 100:.3f} % du capital".replace(".", ","))
    if buyback:
        info.append(f"Info : rachat d'actions annoncé le {buyback:%d/%m/%Y}")
    if earn:
        info.append(f"Info : résultats le {earn:%d/%m/%Y}")
    if r["code"] == "P" and track_text(track):
        info.append("Info : " + track_text(track))
    if (r.get("lines") or 1) > 1:
        info.append(f"Info : {r['lines']} transactions regroupées (prix moyen)")

    prob = None
    version = "v1"
    if mdl is not None and r["code"] == "P":
        m = mdl["model"]
        fs = score_model.features(
            role=_role_bucket(r), value=_f(r["value"]) or 0, delay=delay, cluster=cluster, plan=r["plan_10b5_1"],
            position_pct=pos, first_line=pos is None and after is not None, price=price, market_cap=market_cap,
            buyback=bool(buyback), earnings_soon=earnings_soon,
            track_wins=(track["wins"] / track["n"]) if track and track["n"] >= backtest.TRACK_MIN else None)
        prob = score_model.predict(m, fs)
        score = score_model.percentile(m, prob)
        reasons = ([f"Probabilité de battre le marché à 3 mois : {prob * 100:.0f} % (meilleure que {score} %"
                    " des achats passés)"] + score_model.explain(m, fs, prob))
        version = "v2"
    return {
        "id": f"i{r['id']}", "source": "insider", "ticker": r["ticker"], "company": r["issuer_name"],
        "person": r["owner_name"], "role": _insider_role(r), "side": ROLE_LABEL.get(r["code"], "other"),
        "code": r["code"], "shares": _f(r["shares"]), "price": price, "value": _f(r["value"]),
        "amount_range": None, "trade_date": r["trade_date"], "filed_date": r["filed_date"], "delay_days": delay,
        "plan_10b5_1": r["plan_10b5_1"], "cluster": cluster, "score": score, "score_v1": score_v1,
        "score_version": version, "probability": round(prob * 100, 1) if prob is not None else None,
        "reasons": reasons + info, "current_price": current,
        "change_pct": round(change * 100, 2) if change is not None else None,
        "url": r["url"], "accession": r["accession"], "position_pct": round(pos, 1) if pos is not None else None,
        "lines": r.get("lines") or 1, "market_cap": market_cap, "earnings_date": earn,
        "track": dict(track) if track else None, "owner_cik": r.get("owner_cik"),
    }


# ---------------------------------------------------------------- élus

def congress_signal(r: dict, cluster: int, current: float | None, track: dict | None = None) -> dict:
    delay = scoring.days_between(r["trade_date"], r["report_date"])
    score, reasons = scoring.congress_score(side=r["side"], amount_min=_f(r["amount_min"]), delay_days=delay,
                                            cluster=cluster if r["side"] == "buy" else 0)
    if r["side"] == "buy" and track_text(track):
        reasons.append("Info : " + track_text(track))
    chamber = {"House": "Représentant", "Senate": "Sénateur"}.get(r["chamber"] or "", r["chamber"])
    role = " · ".join(x for x in (chamber, r["party"], r.get("state_dst")) if x) or "Élu"
    return {
        "id": f"c{r['id']}", "source": "congress", "ticker": r["ticker"], "company": None,
        "person": r["politician"], "role": role, "side": r["side"], "code": None, "shares": None, "price": None,
        "value": _f(r["amount_min"]), "amount_range": r["amount_range"], "trade_date": r["trade_date"],
        "filed_date": r["report_date"], "delay_days": delay, "plan_10b5_1": False, "cluster": cluster,
        "score": score, "score_v1": score, "score_version": "v1", "reasons": reasons, "current_price": current,
        "change_pct": None, "url": r.get("url"), "congress_source": r.get("source"),
        "track": dict(track) if track else None,
    }


# ---------------------------------------------------------------- Europe (AMF, BaFin)

# Rôles BaFin → (intitulé pour le score, dirigeant, administrateur, libellé affiché)
BAFIN_ROLES = {
    "vorstand": ("Directoire (Vorstand)", True, False, "Membre du directoire"),
    "aufsichtsrat": (None, False, True, "Membre du conseil de surveillance"),
    "sonstige führungsperson": (None, True, False, "Autre dirigeant"),
    "in enger beziehung": (None, False, False, "Proche d'un dirigeant"),
}


def _europe_role(source: str, role: str | None) -> tuple[str | None, bool, bool, str]:
    if source == "bafin":
        title, officer, director, label = BAFIN_ROLES.get((role or "").strip().lower(), (None, False, False, role))
        # « Directoire » compte comme dirigeant de premier plan dans le score v1
        return ("President" if title else None), officer, director, label or "Dirigeant"
    low = (role or "").lower()
    closely = "étroitement liée" in low or "etroitement liee" in low
    return (None if closely else role), (bool(role) and not closely), False, role or "Dirigeant"


def europe_signal(r: dict, cluster: int, source: str, current_eur: float | None) -> dict:
    delay = scoring.days_between(r["trade_date"], r["published"])
    code = "P" if r["side"] == "buy" else "S"
    title, officer, director, role_label = _europe_role(source, r["role"])
    score, reasons = scoring.insider_score(
        code=code, officer_title=title, is_officer=officer, is_director=director, is_ten_percent=False,
        value=_f(r["value"]), delay_days=delay, cluster=cluster if code == "P" else 0, plan_10b5_1=False,
        price_change=None,
    )
    reasons[0] = ("Acquisition" if code == "P" else "Cession") + reasons[0][reasons[0].index(" ("):]
    reasons = [x.replace(" M$", " M€").replace(" K$", " K€").replace(" $ ", " € ")
               .replace("Dirigeant de premier plan : President", "Membre du directoire") for x in reasons]
    price = _f(r["price"])
    change = (current_eur / price - 1) if current_eur and price else None
    cur_code = "EUR" if (r["currency"] or "EUR").upper().startswith("EUR") else r["currency"]
    return {
        "id": f"{'a' if source == 'amf' else 'b'}{r['id']}", "source": source, "ticker": None,
        "symbol": r["isin"], "company": r["company"], "person": r["person"], "role": role_label,
        "side": r["side"], "code": code, "shares": _f(r["volume"]), "price": price, "value": _f(r["value"]),
        "currency": cur_code, "amount_range": None, "trade_date": r["trade_date"], "filed_date": r["published"],
        "delay_days": delay, "plan_10b5_1": False, "cluster": cluster, "score": score, "score_v1": score,
        "score_version": "v1", "reasons": reasons, "current_price": current_eur,
        "change_pct": round(change * 100, 2) if change is not None else None, "url": r.get("url"),
        "isin": r["isin"], "venue": r["venue"], "country": "FR" if source == "amf" else "DE",
    }


INSIDER_GROUP_SQL = """
SELECT min(id) AS id, accession, code, max(ticker) AS ticker, max(issuer_name) AS issuer_name,
       max(issuer_cik) AS issuer_cik, max(owner_cik) AS owner_cik,
       max(owner_name) AS owner_name, bool_or(is_director) AS is_director, bool_or(is_officer) AS is_officer,
       bool_or(is_ten_percent) AS is_ten_percent, max(officer_title) AS officer_title,
       sum(shares) AS shares, sum(value) AS value,
       sum(value) / nullif(sum(shares) FILTER (WHERE price IS NOT NULL), 0) AS price,
       (array_agg(shares_after ORDER BY line_no DESC))[1] AS shares_after,
       max(trade_date) AS trade_date, max(filed_date) AS filed_date, bool_or(plan_10b5_1) AS plan_10b5_1,
       max(url) AS url, count(*) AS lines
FROM insider_trades WHERE amended_by IS NULL AND {where} GROUP BY accession, code"""
CONGRESS_COLS = ("id, source, politician, party, chamber, ticker, side, amount_range, amount_min, trade_date,"
                 " report_date, url, raw->>'state_dst' AS state_dst")
AMF_COLS = ("id, numero, company, person, role, side, isin, price, currency, volume, value, trade_date, published,"
            " venue, nature, url")
BAFIN_COLS = ("id, company, person, role, side, isin, price, currency, volume, value, trade_date, published, venue,"
              " NULL::text AS url")


async def _europe(conn, table: str, cols: str, source: str, *, side: str, fast: bool, days: int,
                  isin: str | None) -> list[dict]:
    where = ["published >= current_date - %s::int"]
    args: list = [days]
    if side != "all":
        where.append("side = %s")
        args.append(side)
    if isin:
        where.append("isin = %s")
        args.append(isin)
    if fast:
        where.append("trade_date IS NOT NULL AND published - trade_date <= 2")
    cur = await conn.execute(f"SELECT {cols} FROM {table} WHERE {' AND '.join(where)}"
                             " ORDER BY published DESC, value DESC NULLS LAST LIMIT 1000", args)
    rows = await cur.fetchall()
    isins = sorted({r["isin"] for r in rows if r["isin"]})
    clusters = {}
    if isins:
        cur = await conn.execute(
            f"SELECT isin, count(DISTINCT person) AS n FROM {table} WHERE side='buy' AND isin = ANY(%s)"
            " AND coalesce(trade_date, published) >= current_date - 30 GROUP BY isin", (isins,))
        clusters = {r["isin"]: r["n"] for r in await cur.fetchall()}
    pmap = await _price_map(conn, isins)
    return [europe_signal(r, clusters.get(r["isin"], 0), source, pmap.get(r["isin"])) for r in rows]


async def build(conn, *, source: str = "all", side: str = "all", fast: bool = False, days: int = 7,
                ticker: str | None = None, limit: int = 2000, score_version: str = "auto") -> list[dict]:
    out: list[dict] = []
    is_isin = bool(ticker and len(ticker) == 12 and ticker[:2].isalpha() and ticker[-1].isdigit())
    mrow = await active_model(conn)
    mdl = mrow if use_v2(mrow, score_version) else None
    if source in ("all", "insider") and not is_isin:
        where = ["filed_date >= current_date - %s::int"]
        args: list = [days]
        codes = {"all": ["P", "S", "M"], "buy": ["P"], "sell": ["S"], "option": ["M"]}[side]
        where.append("code = ANY(%s)")
        args.append(codes)
        if ticker:
            where.append("ticker = %s")
            args.append(ticker)
        if fast:
            where.append("trade_date IS NOT NULL AND filed_date - trade_date <= 2")
        cur = await conn.execute(INSIDER_GROUP_SQL.format(where=" AND ".join(where))
                                 + " ORDER BY max(filed_date) DESC, sum(value) DESC NULLS LAST LIMIT %s",
                                 args + [limit])
        rows = await cur.fetchall()
        tickers = sorted({r["ticker"] for r in rows if r["ticker"]})
        clusters = await _insider_clusters(conn, tickers)
        pmap = await _price_map(conn, tickers)
        ctx = await _context(conn, rows)
        out += [insider_signal(r, clusters.get(r["ticker"], 0), pmap.get(r["ticker"]), ctx, mdl) for r in rows]
    if source in ("all", "congress") and side in ("all", "buy", "sell") and not is_isin:
        where = ["coalesce(report_date, trade_date) >= current_date - %s::int"]
        args = [days]
        if side != "all":
            where.append("side = %s")
            args.append(side)
        if ticker:
            where.append("ticker = %s")
            args.append(ticker)
        if fast:
            where.append("trade_date IS NOT NULL AND report_date - trade_date <= 15")
        cur = await conn.execute(f"SELECT {CONGRESS_COLS} FROM congress_trades WHERE {' AND '.join(where)}"
                                 " ORDER BY report_date DESC NULLS LAST LIMIT 1000", args)
        rows = await cur.fetchall()
        tickers = sorted({r["ticker"] for r in rows if r["ticker"]})
        clusters = await _congress_clusters(conn, tickers)
        pmap = await _price_map(conn, tickers)
        tracks = await congress_tracks(conn, sorted({r["politician"] for r in rows}))
        out += [congress_signal(r, clusters.get(r["ticker"], 0), pmap.get(r["ticker"]), tracks.get(r["politician"]))
                for r in rows]
    if side in ("all", "buy", "sell") and (not ticker or is_isin):
        if source in ("all", "europe", "amf"):
            out += await _europe(conn, "amf_trades", AMF_COLS, "amf", side=side, fast=fast, days=days,
                                 isin=ticker if is_isin else None)
        if source in ("all", "europe", "bafin"):
            out += await _europe(conn, "bafin_trades", BAFIN_COLS, "bafin", side=side, fast=fast, days=days,
                                 isin=ticker if is_isin else None)
    await conn.commit()
    return out


ISIN_PATTERN = ISIN_RE
