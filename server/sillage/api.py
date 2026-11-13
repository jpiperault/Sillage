"""API JSON /api/v1."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import paper, scoring, security
from .ingest import finra, funds as funds13f, prices

router = APIRouter(prefix="/api/v1")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


# ---------------------------------------------------------------- utilitaires


def _state(request: Request):
    return request.app.state


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "inconnu")


def _rate_limit(request: Request) -> None:
    if not _state(request).limiter.allow(_client_ip(request)):
        raise HTTPException(429, "Trop de tentatives. Réessayez dans quelques minutes.")


def _set_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.headers.get("x-forwarded-proto", "").lower() == "https" or request.url.scheme == "https"
    response.set_cookie(
        security.COOKIE_NAME, token, max_age=security.TOKEN_DAYS * 86400, httponly=True,
        samesite="strict", secure=secure, path="/",
    )


async def current_user(request: Request) -> dict:
    token = request.cookies.get(security.COOKIE_NAME)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Connexion requise.")
    parsed = security.read_token(_state(request).settings.jwt_secret, token)
    if parsed is None:
        raise HTTPException(401, "Session expirée. Reconnectez-vous.")
    user_id, tv = parsed
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute("SELECT id, email, is_admin, token_version FROM users WHERE id=%s", (user_id,))
        user = await cur.fetchone()
    if not user or user["token_version"] != tv:
        raise HTTPException(401, "Session expirée. Reconnectez-vous.")
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Réservé à l'administrateur.")
    return user


def _ticker(t: str) -> str:
    c = prices.clean_ticker(t)
    if c is None:
        raise HTTPException(400, "Ticker invalide.")
    return c


# ---------------------------------------------------------------- comptes


class Credentials(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


def _public_user(u: dict) -> dict:
    return {"id": u["id"], "email": u["email"], "is_admin": u["is_admin"]}


@router.post("/auth/register")
async def register(body: Credentials, request: Request, response: Response):
    _rate_limit(request)
    st = _state(request)
    if not st.settings.allow_signup:
        raise HTTPException(403, "Les inscriptions sont fermées.")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Adresse e-mail invalide.")
    if email == st.settings.admin_email:
        raise HTTPException(403, "Cette adresse est réservée.")
    if len(body.password) < security.MIN_PASSWORD:
        raise HTTPException(400, f"Le mot de passe doit faire au moins {security.MIN_PASSWORD} caractères.")
    pw_hash = security.hash_password(body.password)
    async with st.pool.connection() as conn:
        try:
            cur = await conn.execute(
                "INSERT INTO users(email, password_hash, last_login_at) VALUES (%s,%s,now())"
                " RETURNING id, email, is_admin, token_version",
                (email, pw_hash),
            )
            user = await cur.fetchone()
            await paper.ensure_account(conn, user["id"], st.settings.paper_start_cash)
            await conn.commit()
        except psycopg.errors.UniqueViolation:
            await conn.rollback()
            raise HTTPException(409, "Un compte existe déjà avec cette adresse.")
    _set_cookie(request, response, security.make_token(st.settings.jwt_secret, user["id"], user["token_version"]))
    return {"user": _public_user(user)}


@router.post("/auth/login")
async def login(body: Credentials, request: Request, response: Response):
    _rate_limit(request)
    st = _state(request)
    email = body.email.strip().lower()
    async with st.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, is_admin, token_version, password_hash FROM users WHERE email=%s", (email,)
        )
        user = await cur.fetchone()
        ok = security.check_password(body.password, user["password_hash"] if user else security.DUMMY_HASH)
        if not user or not ok:
            raise HTTPException(401, "E-mail ou mot de passe incorrect.")
        await conn.execute("UPDATE users SET last_login_at=now() WHERE id=%s", (user["id"],))
        await conn.commit()
    _set_cookie(request, response, security.make_token(st.settings.jwt_secret, user["id"], user["token_version"]))
    return {"user": _public_user(user)}


@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(security.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(request: Request, user: dict = Depends(current_user)):
    st = _state(request).settings
    return {"user": _public_user(user), "congress_enabled": st.congress_enabled,
            "amf_enabled": st.amf_enabled, "funds_13f_enabled": bool(st.funds_13f),
            "congress_sources": st.congress_sources,
            "paper_start_cash": st.paper_start_cash}


@router.post("/auth/password")
async def change_password(body: PasswordChange, request: Request, response: Response,
                          user: dict = Depends(current_user)):
    _rate_limit(request)
    st = _state(request)
    if len(body.new_password) < security.MIN_PASSWORD:
        raise HTTPException(400, f"Le nouveau mot de passe doit faire au moins {security.MIN_PASSWORD} caractères.")
    async with st.pool.connection() as conn:
        cur = await conn.execute("SELECT password_hash FROM users WHERE id=%s", (user["id"],))
        row = await cur.fetchone()
        if not security.check_password(body.current_password, row["password_hash"]):
            raise HTTPException(400, "Mot de passe actuel incorrect.")
        cur = await conn.execute(
            "UPDATE users SET password_hash=%s, token_version=token_version+1 WHERE id=%s RETURNING token_version",
            (security.hash_password(body.new_password), user["id"]),
        )
        tv = (await cur.fetchone())["token_version"]
        await conn.commit()
    _set_cookie(request, response, security.make_token(st.settings.jwt_secret, user["id"], tv))
    return {"ok": True, "message": "Mot de passe modifié. Les autres sessions sont déconnectées."}


# ---------------------------------------------------------------- signaux


ROLE_LABEL = {"P": "buy", "S": "sell", "M": "option"}


def _f(x):
    return float(x) if x is not None else None


async def _insider_clusters(conn, tickers: list[str]) -> dict[str, int]:
    if not tickers:
        return {}
    cur = await conn.execute(
        "SELECT ticker, count(DISTINCT coalesce(owner_cik::text, owner_name)) AS n FROM insider_trades"
        " WHERE code='P' AND ticker = ANY(%s) AND coalesce(trade_date, filed_date) >= current_date - 30"
        " GROUP BY ticker",
        (tickers,),
    )
    return {r["ticker"]: r["n"] for r in await cur.fetchall()}


async def _congress_clusters(conn, tickers: list[str]) -> dict[str, int]:
    if not tickers:
        return {}
    cur = await conn.execute(
        "SELECT ticker, count(DISTINCT politician) AS n FROM congress_trades"
        " WHERE ticker = ANY(%s) AND coalesce(report_date, trade_date) >= current_date - 30 GROUP BY ticker",
        (tickers,),
    )
    return {r["ticker"]: r["n"] for r in await cur.fetchall()}


async def _price_map(conn, tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    cur = await conn.execute("SELECT ticker, price FROM prices WHERE ticker = ANY(%s)", (tickers,))
    return {r["ticker"]: float(r["price"]) for r in await cur.fetchall()}


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


def _insider_signal(r: dict, cluster: int, current: float | None) -> dict:
    delay = scoring.days_between(r["trade_date"], r["filed_date"])
    price = _f(r["price"])
    change = (current / price - 1) if current and price else None
    score, reasons = scoring.insider_score(
        code=r["code"], officer_title=r["officer_title"], is_officer=r["is_officer"], is_director=r["is_director"],
        is_ten_percent=r["is_ten_percent"], value=_f(r["value"]), delay_days=delay,
        cluster=cluster if r["code"] == "P" else 0, plan_10b5_1=r["plan_10b5_1"], price_change=change,
    )
    return {
        "id": f"i{r['id']}", "source": "insider", "ticker": r["ticker"], "company": r["issuer_name"],
        "person": r["owner_name"], "role": _insider_role(r), "side": ROLE_LABEL.get(r["code"], "other"),
        "code": r["code"], "shares": _f(r["shares"]), "price": price, "value": _f(r["value"]),
        "amount_range": None, "trade_date": r["trade_date"], "filed_date": r["filed_date"], "delay_days": delay,
        "plan_10b5_1": r["plan_10b5_1"], "cluster": cluster, "score": score, "reasons": reasons,
        "current_price": current, "change_pct": round(change * 100, 2) if change is not None else None,
        "url": r["url"], "accession": r["accession"],
    }


def _congress_signal(r: dict, cluster: int, current: float | None) -> dict:
    delay = scoring.days_between(r["trade_date"], r["report_date"])
    score, reasons = scoring.congress_score(side=r["side"], amount_min=_f(r["amount_min"]), delay_days=delay,
                                            cluster=cluster)
    chamber = {"House": "Représentant", "Senate": "Sénateur"}.get(r["chamber"] or "", r["chamber"])
    role = " · ".join(x for x in (chamber, r["party"], r.get("state_dst")) if x) or "Élu"
    return {
        "id": f"c{r['id']}", "source": "congress", "ticker": r["ticker"], "company": None,
        "person": r["politician"], "role": role, "side": r["side"], "code": None, "shares": None, "price": None,
        "value": _f(r["amount_min"]), "amount_range": r["amount_range"], "trade_date": r["trade_date"],
        "filed_date": r["report_date"], "delay_days": delay, "plan_10b5_1": False, "cluster": cluster,
        "score": score, "reasons": reasons, "current_price": current, "change_pct": None, "url": r.get("url"),
        "congress_source": r.get("source"),
    }


INSIDER_COLS = ("id, accession, ticker, issuer_name, owner_name, is_director, is_officer, is_ten_percent,"
                " officer_title, code, shares, price, value, trade_date, filed_date, plan_10b5_1, url")
CONGRESS_COLS = ("id, source, politician, party, chamber, ticker, side, amount_range, amount_min, trade_date,"
                 " report_date, url, raw->>'state_dst' AS state_dst")


async def _build_signals(conn, *, source: str, side: str, fast: bool, days: int, ticker: str | None,
                         limit: int) -> list[dict]:
    out: list[dict] = []
    if source in ("all", "insider"):
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
        cur = await conn.execute(
            f"SELECT {INSIDER_COLS} FROM insider_trades WHERE {' AND '.join(where)}"
            " ORDER BY filed_date DESC, value DESC NULLS LAST LIMIT 2000",
            args,
        )
        rows = await cur.fetchall()
        tickers = sorted({r["ticker"] for r in rows if r["ticker"]})
        clusters = await _insider_clusters(conn, tickers)
        pmap = await _price_map(conn, tickers)
        out += [_insider_signal(r, clusters.get(r["ticker"], 0), pmap.get(r["ticker"])) for r in rows]
    if source in ("all", "congress") and side in ("all", "buy", "sell"):
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
        cur = await conn.execute(
            f"SELECT {CONGRESS_COLS} FROM congress_trades WHERE {' AND '.join(where)}"
            " ORDER BY report_date DESC NULLS LAST LIMIT 1000",
            args,
        )
        rows = await cur.fetchall()
        tickers = sorted({r["ticker"] for r in rows if r["ticker"]})
        clusters = await _congress_clusters(conn, tickers)
        pmap = await _price_map(conn, tickers)
        out += [_congress_signal(r, clusters.get(r["ticker"], 0), pmap.get(r["ticker"])) for r in rows]
    if source in ("all", "amf") and side in ("all", "buy", "sell") and not ticker:
        where = ["published >= current_date - %s::int"]
        args = [days]
        if side != "all":
            where.append("side = %s")
            args.append(side)
        if fast:
            where.append("trade_date IS NOT NULL AND published - trade_date <= 2")
        cur = await conn.execute(
            f"SELECT {AMF_COLS} FROM amf_trades WHERE {' AND '.join(where)}"
            " ORDER BY published DESC, value DESC NULLS LAST LIMIT 1000", args)
        rows = await cur.fetchall()
        clusters = await _amf_clusters(conn, sorted({r["isin"] for r in rows if r["isin"]}))
        out += [_amf_signal(r, clusters.get(r["isin"], 0)) for r in rows]
    await conn.commit()
    return out


AMF_COLS = ("id, numero, company, person, role, side, isin, price, currency, volume, value, trade_date, published,"
            " venue, nature, url")


async def _amf_clusters(conn, isins: list[str]) -> dict[str, int]:
    if not isins:
        return {}
    cur = await conn.execute(
        "SELECT isin, count(DISTINCT person) AS n FROM amf_trades WHERE side='buy' AND isin = ANY(%s)"
        " AND coalesce(trade_date, published) >= current_date - 30 GROUP BY isin", (isins,))
    return {r["isin"]: r["n"] for r in await cur.fetchall()}


def _amf_signal(r: dict, cluster: int) -> dict:
    delay = scoring.days_between(r["trade_date"], r["published"])
    code = "P" if r["side"] == "buy" else "S"
    score, reasons = scoring.insider_score(
        code=code, officer_title=r["role"], is_officer=bool(r["role"]), is_director=False, is_ten_percent=False,
        value=_f(r["value"]), delay_days=delay, cluster=cluster if code == "P" else 0, plan_10b5_1=False,
        price_change=None,
    )
    reasons[0] = ("Acquisition" if code == "P" else "Cession") + reasons[0][reasons[0].index(" ("):]
    reasons = [r.replace(" M$", " M€").replace(" K$", " K€").replace(" $ ", " € ") for r in reasons]
    return {
        "id": f"a{r['id']}", "source": "amf", "ticker": None, "company": r["company"], "person": r["person"],
        "role": r["role"] or "Dirigeant", "side": r["side"], "code": code, "shares": _f(r["volume"]),
        "price": _f(r["price"]), "value": _f(r["value"]), "currency": "EUR" if (r["currency"] or "").lower().startswith("eur") else r["currency"],
        "amount_range": None, "trade_date": r["trade_date"], "filed_date": r["published"], "delay_days": delay,
        "plan_10b5_1": False, "cluster": cluster, "score": score, "reasons": reasons, "current_price": None,
        "change_pct": None, "url": r["url"], "isin": r["isin"], "venue": r["venue"],
    }


@router.get("/signals")
async def signals(
    request: Request,
    source: Literal["all", "insider", "congress", "amf"] = "all",
    side: Literal["all", "buy", "sell", "option"] = "all",
    fast: bool = False,
    days: int = Query(7, ge=1, le=365),
    sort: Literal["score", "date"] = "score",
    ticker: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(current_user),
):
    t = _ticker(ticker) if ticker else None
    async with _state(request).pool.connection() as conn:
        items = await _build_signals(conn, source=source, side=side, fast=fast, days=days, ticker=t, limit=limit)
    if sort == "score":
        items.sort(key=lambda s: (s["score"], s["filed_date"] or date.min), reverse=True)
    else:
        items.sort(key=lambda s: (s["filed_date"] or date.min, s["score"]), reverse=True)
    return {"items": items[:limit], "total": len(items)}


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: str, request: Request, user: dict = Depends(current_user)):
    m = re.fullmatch(r"([ica])(\d{1,18})", signal_id)
    if not m:
        raise HTTPException(404, "Signal introuvable.")
    st = _state(request)
    kind, sid = m.group(1), int(m.group(2))
    async with st.pool.connection() as conn:
        if kind == "a":
            cur = await conn.execute(f"SELECT {AMF_COLS} FROM amf_trades WHERE id=%s", (sid,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "Signal introuvable.")
            sig = _amf_signal(row, (await _amf_clusters(conn, [row["isin"]])).get(row["isin"], 0) if row["isin"] else 0)
            await conn.commit()
            return {**sig, "in_watchlist": False, "quote": None, "position": None, "short": None, "notices": []}
        if kind == "i":
            cur = await conn.execute(f"SELECT {INSIDER_COLS} FROM insider_trades WHERE id=%s", (sid,))
        else:
            cur = await conn.execute(f"SELECT {CONGRESS_COLS} FROM congress_trades WHERE id=%s", (sid,))
        row = await cur.fetchone()
        await conn.commit()
    if not row:
        raise HTTPException(404, "Signal introuvable.")
    quote = await prices.get_price(st.pool, st.prices, row["ticker"]) if row["ticker"] else None
    current = quote["price"] if quote else None
    async with st.pool.connection() as conn:
        if kind == "i":
            cl = (await _insider_clusters(conn, [row["ticker"]])).get(row["ticker"], 0) if row["ticker"] else 0
            sig = _insider_signal(row, cl, current)
        else:
            cl = (await _congress_clusters(conn, [row["ticker"]])).get(row["ticker"], 0) if row["ticker"] else 0
            sig = _congress_signal(row, cl, current)
        cur = await conn.execute(
            "SELECT 1 FROM watchlist WHERE user_id=%s AND ticker=%s", (user["id"], row["ticker"])
        )
        sig["in_watchlist"] = (await cur.fetchone()) is not None
        cur = await conn.execute(
            "SELECT quantity, avg_price FROM paper_positions WHERE user_id=%s AND ticker=%s", (user["id"], row["ticker"])
        )
        pos = await cur.fetchone()
        sig["short"] = await finra.stats_for(conn, row["ticker"]) if row["ticker"] else None
        sig["notices"] = []
        if row["ticker"]:
            cur = await conn.execute(
                "SELECT accession, person, relationship, shares, market_value, shares_outstanding, approx_sale_date,"
                " filed_date, plan_10b5_1, url FROM sale_notices WHERE ticker=%s AND filed_date >= current_date - 60"
                " ORDER BY filed_date DESC LIMIT 5", (row["ticker"],))
            sig["notices"] = await cur.fetchall()
        await conn.commit()
    if sig["short"]:
        s = sig["short"]
        sig["reasons"].append(f"Info : ventes à découvert {s['ratio_pct']} % du volume le {s['day']:%d/%m}"
                              f" (moyenne 20 j : {s['avg20_pct']} %)".replace(".", ","))
    if sig["notices"]:
        sig["reasons"].append(f"Info : {len(sig['notices'])} vente(s) annoncée(s) (Form 144) sur 60 j")
    sig["quote"] = quote
    sig["position"] = {"quantity": float(pos["quantity"]), "avg_price": float(pos["avg_price"])} if pos else None
    return sig


@router.get("/notices")
async def notices(request: Request, days: int = Query(14, ge=1, le=180), min_value: float = Query(0, ge=0),
                  ticker: str | None = None, user: dict = Depends(current_user)):
    where = ["filed_date >= current_date - %s::int", "coalesce(market_value, 0) >= %s"]
    args: list = [days, min_value]
    if ticker:
        where.append("ticker = %s")
        args.append(_ticker(ticker))
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            "SELECT accession, issuer_name, ticker, person, relationship, shares, market_value, shares_outstanding,"
            " approx_sale_date, filed_date, plan_10b5_1, url,"
            " CASE WHEN shares_outstanding > 0 THEN shares / shares_outstanding * 100 END AS pct_outstanding"
            f" FROM sale_notices WHERE {' AND '.join(where)}"
            " ORDER BY filed_date DESC, market_value DESC NULLS LAST LIMIT 500", args)
        items = await cur.fetchall()
        await conn.commit()
    return {"items": items}


@router.get("/funds13f")
async def funds_13f_list(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            """SELECT DISTINCT ON (cik) cik, fund_name, period, filed_date, total_value, positions
               FROM fund_filings ORDER BY cik, period DESC""")
        items = await cur.fetchall()
        await conn.commit()
    tracked = _state(request).settings.funds_13f
    known = {i["cik"] for i in items}
    items += [{"cik": c, "fund_name": n, "period": None, "filed_date": None, "total_value": None, "positions": None}
              for c, n in tracked.items() if c not in known]
    items.sort(key=lambda i: (i["total_value"] or 0), reverse=True)
    return {"items": items}


async def _fund_moves(conn, cik: int) -> tuple[list[dict], list[dict]]:
    cur = await conn.execute(
        "SELECT accession, fund_name, period, filed_date, total_value FROM fund_filings WHERE cik=%s"
        " ORDER BY period DESC LIMIT 2", (cik,))
    filings = await cur.fetchall()
    if not filings:
        return [], []
    cur_acc = filings[0]["accession"]
    prev_acc = filings[1]["accession"] if len(filings) > 1 else None
    cur = await conn.execute(
        """WITH c AS (SELECT * FROM fund_holdings WHERE accession=%s),
                p AS (SELECT * FROM fund_holdings WHERE accession=%s)
           SELECT coalesce(c.cusip, p.cusip) AS cusip, coalesce(c.put_call, p.put_call) AS put_call,
                  coalesce(c.issuer, p.issuer) AS issuer, c.shares AS shares, p.shares AS prev_shares,
                  c.value AS value, p.value AS prev_value, t.ticker
           FROM c FULL JOIN p ON c.cusip = p.cusip AND c.put_call = p.put_call
           LEFT JOIN cusip_tickers t ON t.cusip = coalesce(c.cusip, p.cusip)""",
        (cur_acc, prev_acc))
    total = float(filings[0]["total_value"] or 0)
    moves = []
    for r in await cur.fetchall():
        shares, prev = _f(r["shares"]), _f(r["prev_shares"])
        status = funds13f.classify(shares, prev) if prev_acc else "held"
        moves.append({
            **r, "shares": shares, "prev_shares": prev, "value": _f(r["value"]), "prev_value": _f(r["prev_value"]),
            "status": status, "weight_pct": round(_f(r["value"]) / total * 100, 2) if total and r["value"] else None,
            "change_pct": round((shares / prev - 1) * 100, 1) if shares and prev else None,
        })
    order = {"new": 0, "added": 1, "reduced": 2, "sold": 3, "held": 4}
    moves.sort(key=lambda m: (order[m["status"]], -(m["value"] or m["prev_value"] or 0)))
    return filings, moves


@router.get("/funds13f/consensus")
async def funds_13f_consensus(request: Request, user: dict = Depends(current_user)):
    """Titres achetés ou renforcés par le plus de fonds suivis lors de leur dernier trimestre."""
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute("SELECT DISTINCT cik FROM fund_filings")
        ciks = [r["cik"] for r in await cur.fetchall()]
        agg: dict[str, dict] = {}
        for cik in ciks:
            filings, moves = await _fund_moves(conn, cik)
            if len(filings) < 2:
                continue
            for m in moves:
                if m["put_call"] or m["status"] not in ("new", "added", "sold", "reduced"):
                    continue
                a = agg.setdefault(m["cusip"], {"cusip": m["cusip"], "ticker": m["ticker"], "issuer": m["issuer"],
                                                "buyers": [], "sellers": [], "value_bought": 0.0})
                if m["status"] in ("new", "added"):
                    a["buyers"].append({"fund": filings[0]["fund_name"], "status": m["status"]})
                    a["value_bought"] += (m["value"] or 0) - (m["prev_value"] or 0)
                else:
                    a["sellers"].append({"fund": filings[0]["fund_name"], "status": m["status"]})
        await conn.commit()
    items = [a for a in agg.values() if len(a["buyers"]) >= 2]
    items.sort(key=lambda a: (len(a["buyers"]) - len(a["sellers"]), a["value_bought"]), reverse=True)
    return {"items": items[:40]}


@router.get("/funds13f/{cik}")
async def funds_13f_detail(cik: int, request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        filings, moves = await _fund_moves(conn, cik)
        await conn.commit()
    if not filings:
        raise HTTPException(404, "Aucun 13F collecté pour ce fonds.")
    return {"filings": filings, "moves": moves}


@router.get("/funds")
async def funds(request: Request, form: Literal["13D", "13G", "all"] = "all",
                days: int = Query(30, ge=1, le=365), user: dict = Depends(current_user)):
    where = ["filed_date >= current_date - %s::int"]
    args: list = [days]
    if form != "all":
        where.append("form_type LIKE %s")
        args.append(form + "%")
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            "SELECT accession, form_type, subject_cik, subject_name, ticker, filer_cik, filer_name, filed_date, url"
            f" FROM ownership_filings WHERE {' AND '.join(where)} ORDER BY filed_date DESC, accession DESC LIMIT 500",
            args,
        )
        items = await cur.fetchall()
        await conn.commit()
    return {"items": items}


@router.get("/convergence")
async def convergence(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            """WITH i AS (SELECT ticker, count(DISTINCT coalesce(owner_cik::text, owner_name)) n,
                                 max(issuer_name) AS name
                          FROM insider_trades WHERE code='P' AND ticker IS NOT NULL
                            AND filed_date >= current_date - 30 GROUP BY ticker),
                    c AS (SELECT ticker, count(DISTINCT politician) n FROM congress_trades
                          WHERE side='buy' AND ticker IS NOT NULL
                            AND coalesce(report_date, trade_date) >= current_date - 30 GROUP BY ticker),
                    o AS (SELECT ticker, count(*) n, max(subject_name) AS name FROM ownership_filings
                          WHERE ticker IS NOT NULL AND form_type LIKE '13D%'
                            AND filed_date >= current_date - 30 GROUP BY ticker)
               SELECT t.ticker, coalesce(i.name, o.name) AS name,
                      coalesce(i.n,0) AS insiders, coalesce(c.n,0) AS congress, coalesce(o.n,0) AS ownership
               FROM (SELECT ticker FROM i UNION SELECT ticker FROM c UNION SELECT ticker FROM o) t
               LEFT JOIN i USING (ticker) LEFT JOIN c USING (ticker) LEFT JOIN o USING (ticker)
               WHERE coalesce(i.n,0) + coalesce(c.n,0) + coalesce(o.n,0) >= 2"""
        )
        rows = await cur.fetchall()
        await conn.commit()
    items = []
    for r in rows:
        score = scoring.convergence_score(insiders=r["insiders"], congress=r["congress"], ownership=r["ownership"])
        items.append({**r, "score": score})
    items.sort(key=lambda x: (x["score"], x["insiders"]), reverse=True)
    return {"items": items[:30]}


@router.get("/congress/ranking")
async def congress_ranking(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT politician, max(party) AS party, max(chamber) AS chamber, max(raw->>'state_dst') AS state_dst, count(*) AS trades,
                      count(*) FILTER (WHERE side='buy') AS buys, count(*) FILTER (WHERE side='sell') AS sells,
                      coalesce(sum(amount_min),0) AS volume_min, max(report_date) AS last_report,
                      avg(report_date - trade_date) FILTER (WHERE report_date IS NOT NULL AND trade_date IS NOT NULL)
                        AS avg_delay
               FROM congress_trades WHERE coalesce(report_date, trade_date) >= current_date - 365
               GROUP BY politician ORDER BY count(*) DESC LIMIT 50"""
        )
        items = await cur.fetchall()
        await conn.commit()
    return {"enabled": st.settings.congress_enabled, "items": items}


@router.get("/quote/{ticker}")
async def quote(ticker: str, request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    q = await prices.get_price(st.pool, st.prices, _ticker(ticker))
    if q is None:
        raise HTTPException(404, "Cours indisponible pour ce ticker.")
    return {"ticker": _ticker(ticker), **q}


@router.get("/status")
async def status(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        cur = await conn.execute("SELECT name, last_run_at, last_ok_at, last_error_at, last_error, info"
                                 " FROM job_state ORDER BY name")
        jobs = await cur.fetchall()
        cur = await conn.execute(
            """SELECT (SELECT count(*) FROM insider_trades) AS insider_trades,
                      (SELECT count(*) FROM sec_filings WHERE status='pending') AS filings_pending,
                      (SELECT count(*) FROM sec_filings WHERE status='done') AS filings_done,
                      (SELECT count(*) FROM sec_filings WHERE status='error') AS filings_error,
                      (SELECT count(*) FROM ownership_filings) AS ownership_filings,
                      (SELECT count(*) FROM congress_trades) AS congress_trades,
                      (SELECT count(*) FROM house_filings WHERE status='pending') AS house_pending,
                      (SELECT count(*) FROM sale_notices) AS sale_notices,
                      (SELECT count(*) FROM amf_trades) AS amf_trades,
                      (SELECT count(*) FROM fund_filings) AS fund_filings,
                      (SELECT count(DISTINCT day) FROM short_volume) AS short_days,
                      (SELECT count(*) FROM cik_tickers) AS tickers,
                      (SELECT count(*) FROM prices) AS prices,
                      (SELECT max(filed_date) FROM insider_trades) AS last_filed"""
        )
        counts = await cur.fetchone()
        await conn.commit()
    ing = st.ingestor
    return {
        "jobs": [{**j, "info": {k: v for k, v in (j["info"] or {}).items() if k != "days"}} for j in jobs],
        "counts": counts,
        "ingest_enabled": st.settings.ingest_enabled,
        "ingest_running": bool(ing and ing.running),
        "interval_min": st.settings.ingest_interval_min,
        "congress_enabled": st.settings.congress_enabled,
        "congress_sources": st.settings.congress_sources,
    }


# ---------------------------------------------------------------- entraînement


class PaperOrder(BaseModel):
    ticker: str = Field(max_length=12)
    side: Literal["buy", "sell"]
    quantity: float | None = Field(default=None, gt=0, le=10_000_000)
    signal_ref: str | None = Field(default=None, max_length=32)


@router.get("/paper")
async def paper_get(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        return await paper.summary(conn, user["id"], st.settings.paper_start_cash)


@router.post("/paper/orders")
async def paper_order(body: PaperOrder, request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    t = _ticker(body.ticker)
    q = await prices.get_price(st.pool, st.prices, t)
    if q is None:
        raise HTTPException(400, f"Cours indisponible pour {t} : ordre fictif impossible.")
    async with st.pool.connection() as conn:
        try:
            if body.side == "buy":
                if not body.quantity:
                    raise paper.PaperError("Indiquez une quantité.")
                res = await paper.buy(conn, user["id"], t, body.quantity, q["price"], st.settings.paper_start_cash,
                                      body.signal_ref)
            else:
                res = await paper.sell_all(conn, user["id"], t, q["price"])
            await conn.commit()
        except paper.PaperError as exc:
            await conn.rollback()
            raise HTTPException(400, str(exc))
    res["stale_price"] = q["stale"]
    return res


@router.post("/paper/reset")
async def paper_reset(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        await paper.reset(conn, user["id"], st.settings.paper_start_cash)
        await conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- watchlist


@router.get("/watchlist")
async def watchlist_get(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(
            """SELECT w.ticker, w.added_at, p.price, p.fetched_at,
                      (SELECT max(filed_date) FROM insider_trades it WHERE it.ticker = w.ticker) AS last_insider,
                      (SELECT max(issuer_name) FROM insider_trades it WHERE it.ticker = w.ticker) AS company
               FROM watchlist w LEFT JOIN prices p ON p.ticker = w.ticker
               WHERE w.user_id=%s ORDER BY w.added_at DESC""",
            (user["id"],),
        )
        items = await cur.fetchall()
        await conn.commit()
    return {"items": items}


@router.put("/watchlist/{ticker}")
async def watchlist_add(ticker: str, request: Request, user: dict = Depends(current_user)):
    t = _ticker(ticker)
    async with _state(request).pool.connection() as conn:
        await conn.execute("INSERT INTO watchlist(user_id, ticker) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                           (user["id"], t))
        await conn.commit()
    return {"ok": True, "ticker": t}


@router.delete("/watchlist/{ticker}")
async def watchlist_remove(ticker: str, request: Request, user: dict = Depends(current_user)):
    t = _ticker(ticker)
    async with _state(request).pool.connection() as conn:
        await conn.execute("DELETE FROM watchlist WHERE user_id=%s AND ticker=%s", (user["id"], t))
        await conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------- admin


@router.post("/admin/ingest")
async def admin_ingest(request: Request, user: dict = Depends(admin_user)):
    ing = _state(request).ingestor
    if ing is None:
        raise HTTPException(409, "La collecte est désactivée (INGEST_ENABLED=false).")
    if not ing.trigger():
        return {"ok": False, "message": "Une collecte est déjà en cours."}
    return {"ok": True, "message": "Collecte relancée."}
