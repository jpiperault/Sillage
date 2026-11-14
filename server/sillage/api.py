"""API JSON /api/v1."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal

import httpx
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from . import alerts, backtest, model as score_model, paper, rules, scoring, security, settings_store
from . import signals as signals_mod
from .ingest import euronext, finra, funds as funds13f, prices, senate, usaspending

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
            "bafin_enabled": st.bafin_enabled, "senate_enabled": st.senate_enabled,
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


async def _build_signals(conn, *, source: str, side: str, fast: bool, days: int, ticker: str | None,
                         limit: int, score_version: str = "auto") -> list[dict]:
    return await signals_mod.build(conn, source=source, side=side, fast=fast, days=days, ticker=ticker,
                                   limit=limit, score_version=score_version)


def _is_isin(t: str | None) -> bool:
    return bool(t and len(t) == 12 and t[:2].isalpha() and t[-1].isdigit())


@router.get("/signals")
async def signals(
    request: Request,
    source: Literal["all", "insider", "congress", "amf", "bafin", "europe"] = "all",
    side: Literal["all", "buy", "sell", "option"] = "all",
    fast: bool = False,
    days: int = Query(7, ge=1, le=365),
    sort: Literal["score", "date"] = "score",
    ticker: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(current_user),
):
    t = _ticker(ticker) if ticker else None
    st = _state(request)
    async with st.pool.connection() as conn:
        items = await _build_signals(conn, source=source, side=side, fast=fast, days=days, ticker=t, limit=2000,
                                     score_version=st.settings.score_version)
    if sort == "score":
        items.sort(key=lambda s: (s["score"], s["filed_date"] or date.min), reverse=True)
    else:
        items.sort(key=lambda s: (s["filed_date"] or date.min, s["score"]), reverse=True)
    return {"items": items[:limit], "total": len(items)}


async def _one_signal(conn, kind: str, sid: int, score_version: str) -> dict | None:
    if kind == "i":
        cur = await conn.execute(signals_mod.INSIDER_GROUP_SQL.format(
            where="(accession, code) = (SELECT accession, code FROM insider_trades WHERE id=%s)"), (sid,))
        row = await cur.fetchone()
        if not row:
            return None
        cl = (await signals_mod._insider_clusters(conn, [row["ticker"]])).get(row["ticker"], 0) if row["ticker"] else 0
        pm = await signals_mod._price_map(conn, [row["ticker"]] if row["ticker"] else [])
        ctx = await signals_mod._context(conn, [row])
        mrow = await signals_mod.active_model(conn)
        return signals_mod.insider_signal(row, cl, pm.get(row["ticker"]), ctx,
                                          mrow if signals_mod.use_v2(mrow, score_version) else None)
    if kind == "c":
        cur = await conn.execute(f"SELECT {signals_mod.CONGRESS_COLS} FROM congress_trades WHERE id=%s", (sid,))
        row = await cur.fetchone()
        if not row:
            return None
        cl = (await signals_mod._congress_clusters(conn, [row["ticker"]])).get(row["ticker"], 0) if row["ticker"] else 0
        tr = await signals_mod.congress_tracks(conn, [row["politician"]])
        return signals_mod.congress_signal(row, cl, None, tr.get(row["politician"]))
    table, cols, source = (("amf_trades", signals_mod.AMF_COLS, "amf") if kind == "a"
                           else ("bafin_trades", signals_mod.BAFIN_COLS, "bafin"))
    cur = await conn.execute(f"SELECT {cols} FROM {table} WHERE id=%s", (sid,))
    row = await cur.fetchone()
    if not row:
        return None
    pm = await signals_mod._price_map(conn, [row["isin"]] if row["isin"] else [])
    return signals_mod.europe_signal(row, 0, source, pm.get(row["isin"]))


@router.get("/signals/{signal_id}")
async def signal_detail(signal_id: str, request: Request, user: dict = Depends(current_user)):
    m = re.fullmatch(r"([icab])(\d{1,18})", signal_id)
    if not m:
        raise HTTPException(404, "Signal introuvable.")
    st = _state(request)
    kind, sid = m.group(1), int(m.group(2))
    async with st.pool.connection() as conn:
        sig = await _one_signal(conn, kind, sid, st.settings.score_version)
        await conn.commit()
    if not sig:
        raise HTTPException(404, "Signal introuvable.")
    sym = sig.get("ticker") or sig.get("symbol")
    quote = None
    if sig.get("ticker"):
        quote = await prices.get_price(st.pool, st.prices, sig["ticker"])
        if quote and sig["source"] == "insider" and sig.get("price"):
            sig["current_price"] = quote["price"]
            sig["change_pct"] = round((quote["price"] / sig["price"] - 1) * 100, 2)
    elif sym:
        async with st.pool.connection() as conn:
            cur = await conn.execute("SELECT price, currency, fetched_at FROM prices WHERE ticker=%s", (sym,))
            q = await cur.fetchone()
            usd = None
            if q:
                fx = await euronext.eur_usd(conn)
                usd = float(q["price"]) * fx if fx and q["currency"] == "EUR" else None
            await conn.commit()
        if q:
            quote = {"price": float(q["price"]), "currency": q["currency"], "fetched_at": q["fetched_at"],
                     "stale": False, "price_usd": usd}
    async with st.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM watchlist WHERE user_id=%s AND ticker=%s", (user["id"], sym))
        sig["in_watchlist"] = (await cur.fetchone()) is not None
        cur = await conn.execute("SELECT quantity, avg_price FROM paper_positions WHERE user_id=%s AND book='manual'"
                                 " AND ticker=%s", (user["id"], sym))
        pos = await cur.fetchone()
        sig["short"] = await finra.stats_for(conn, sig["ticker"]) if sig.get("ticker") else None
        sig["notices"] = []
        sig["contracts"] = None
        if sig.get("ticker"):
            cur = await conn.execute(
                "SELECT accession, person, relationship, shares, market_value, shares_outstanding, approx_sale_date,"
                " filed_date, plan_10b5_1, url FROM sale_notices WHERE ticker=%s AND filed_date >= current_date - 60"
                " ORDER BY filed_date DESC LIMIT 5", (sig["ticker"],))
            sig["notices"] = await cur.fetchall()
            if sig["source"] == "congress":
                company = await _company_name(conn, sig["ticker"])
                sig["contracts"] = await usaspending.cached(conn, sig["ticker"], company)
        await conn.commit()
    if sig["short"]:
        s = sig["short"]
        sig["reasons"].append(f"Info : ventes à découvert {s['ratio_pct']} % du volume le {s['day']:%d/%m}"
                              f" (moyenne 20 j : {s['avg20_pct']} %)".replace(".", ","))
    if sig["notices"]:
        sig["reasons"].append(f"Info : {len(sig['notices'])} vente(s) annoncée(s) (Form 144) sur 60 j")
    c = sig.get("contracts")
    if c and c.get("amount_12m"):
        sig["reasons"].append(f"Info : contrats fédéraux sur 12 mois : {_fmt_big(float(c['amount_12m']))}"
                              f" (USAspending, « {c['search_name']} »)")
    sig["quote"] = quote
    sig["position"] = {"quantity": float(pos["quantity"]), "avg_price": float(pos["avg_price"])} if pos else None
    return sig


def _fmt_big(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f} Md$".replace(".", ",")
    if v >= 1e6:
        return f"{v / 1e6:.1f} M$".replace(".", ",")
    return f"{v / 1e3:.0f} K$"


async def _company_name(conn, ticker: str) -> str | None:
    cur = await conn.execute(
        "SELECT coalesce((SELECT title FROM cik_tickers WHERE ticker=%s LIMIT 1),"
        " (SELECT max(issuer_name) FROM insider_trades WHERE ticker=%s)) AS n", (ticker, ticker))
    r = await cur.fetchone()
    return r["n"] if r else None


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
                          FROM insider_trades WHERE code='P' AND ticker IS NOT NULL AND amended_by IS NULL
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
                      (SELECT count(*) FROM bafin_trades) AS bafin_trades,
                      (SELECT count(*) FROM buybacks) AS buybacks,
                      (SELECT count(*) FROM earnings WHERE day >= current_date) AS earnings_upcoming,
                      (SELECT count(*) FROM company_shares) AS company_shares,
                      (SELECT count(*) FROM track_records) AS track_records,
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


Book = Literal["manual", "rule"]


class PaperOrder(BaseModel):
    ticker: str = Field(max_length=12)
    side: Literal["buy", "sell"]
    quantity: float | None = Field(default=None, gt=0, le=10_000_000)
    signal_ref: str | None = Field(default=None, max_length=32)
    book: Book = "manual"


@router.get("/paper")
async def paper_get(request: Request, book: Book = "manual", user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        res = await paper.summary(conn, user["id"], st.settings.paper_start_cash, book)
        res["curve"] = await paper.equity_curve(conn, user["id"], book)
        return res


@router.post("/paper/orders")
async def paper_order(body: PaperOrder, request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    t = _ticker(body.ticker)
    async with st.pool.connection() as conn:
        px, currency = await rules.quote_usd(st.pool, st.prices, conn, t)
        if px is None:
            raise HTTPException(400, f"Cours indisponible pour {t} : ordre fictif impossible.")
        try:
            if body.side == "buy":
                if not body.quantity:
                    raise paper.PaperError("Indiquez une quantité.")
                res = await paper.buy(conn, user["id"], t, body.quantity, px, st.settings.paper_start_cash,
                                      body.signal_ref, book=body.book, currency=currency)
            else:
                res = await paper.sell_all(conn, user["id"], t, px, book=body.book)
            await conn.commit()
        except paper.PaperError as exc:
            await conn.rollback()
            raise HTTPException(400, str(exc))
    res["currency_note"] = "cours en euros converti en dollars au taux BCE" if currency == "EUR" else None
    res["stale_price"] = False
    return res


@router.post("/paper/reset")
async def paper_reset(request: Request, book: Book = "manual", user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        await paper.reset(conn, user["id"], st.settings.paper_start_cash, book)
        await conn.commit()
    return {"ok": True}


class RuleConfig(BaseModel):
    enabled: bool
    min_score: int = Field(ge=0, le=100)
    alloc_pct: float = Field(gt=0, le=20)
    hold_days: int = Field(ge=5, le=400)
    max_positions: int = Field(ge=1, le=100)
    sources: list[Literal["insider", "congress", "amf", "bafin"]] = ["insider"]


async def _rule_row(conn, user_id: int) -> dict:
    await conn.execute("INSERT INTO paper_rules (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    cur = await conn.execute("SELECT * FROM paper_rules WHERE user_id=%s", (user_id,))
    r = await cur.fetchone()
    await conn.commit()
    return {**r, "alloc_pct": float(r["alloc_pct"]), "sources": r["sources"].split(",")}


@router.get("/paper/rule")
async def paper_rule_get(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        return await _rule_row(conn, user["id"])


@router.put("/paper/rule")
async def paper_rule_put(body: RuleConfig, request: Request, user: dict = Depends(current_user)):
    if not body.sources:
        raise HTTPException(400, "Choisissez au moins une source.")
    async with _state(request).pool.connection() as conn:
        before = await _rule_row(conn, user["id"])
        await conn.execute(
            """UPDATE paper_rules SET enabled=%s, min_score=%s, alloc_pct=%s, hold_days=%s, max_positions=%s,
                 sources=%s, last_check = CASE WHEN %s AND NOT enabled THEN now() ELSE last_check END
               WHERE user_id=%s""",
            (body.enabled, body.min_score, body.alloc_pct, body.hold_days, body.max_positions,
             ",".join(body.sources), body.enabled, user["id"]))
        await conn.commit()
        if body.enabled and not before["enabled"]:
            await paper.ensure_account(conn, user["id"], _state(request).settings.paper_start_cash, "rule")
            await conn.commit()
        return await _rule_row(conn, user["id"])


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


# ---------------------------------------------------------------- réglages (admin)


@router.get("/admin/settings")
async def admin_settings_get(request: Request, user: dict = Depends(admin_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        overrides = await settings_store.load(conn)
    return {"items": settings_store.describe(st.base_settings, overrides, st.settings)}


class SettingsUpdate(BaseModel):
    values: dict[str, str | None]  # None = revenir à la variable d'environnement / au défaut


@router.put("/admin/settings")
async def admin_settings_put(body: SettingsUpdate, request: Request, user: dict = Depends(admin_user)):
    st = _state(request)
    errors = {}
    for key, raw in body.values.items():
        if key not in settings_store.EDITABLE:
            errors[key] = "réglage inconnu"
            continue
        if raw is not None:
            try:
                settings_store.convert(key, raw)
            except settings_store.SettingError as exc:
                errors[key] = str(exc)
    if errors:
        label = {k: settings_store.EDITABLE.get(k, (None, k))[1] for k in errors}
        raise HTTPException(400, " ; ".join(f"{label[k]} : {v}" for k, v in errors.items()))
    async with st.pool.connection() as conn:
        for key, raw in body.values.items():
            if raw is None:
                await conn.execute("DELETE FROM app_settings WHERE key=%s", (key,))
            else:
                await conn.execute(
                    "INSERT INTO app_settings (key, value, updated_by) VALUES (%s,%s,%s) ON CONFLICT (key)"
                    " DO UPDATE SET value=EXCLUDED.value, updated_at=now(), updated_by=EXCLUDED.updated_by",
                    (key, raw.strip(), user["id"]))
        await conn.commit()
        overrides = await settings_store.load(conn)
    st.settings = settings_store.apply(st.base_settings, overrides)
    st.bot_username = None
    return {"items": settings_store.describe(st.base_settings, overrides, st.settings),
            "message": "Réglages enregistrés : pris en compte au prochain cycle de collecte."}


# ---------------------------------------------------------------- alertes Telegram


async def _bot_username(st) -> str | None:
    token = st.settings.telegram_bot_token
    if not token:
        return None
    if getattr(st, "bot_username", None):
        return st.bot_username
    tg = alerts.Telegram(token)
    try:
        st.bot_username = await tg.username()
    except (alerts.TelegramError, httpx.HTTPError):
        st.bot_username = None
    finally:
        await tg.close()
    return st.bot_username


async def _alerts_row(conn, user_id: int) -> dict:
    await conn.execute("INSERT INTO user_alerts (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    cur = await conn.execute("SELECT * FROM user_alerts WHERE user_id=%s", (user_id,))
    return await cur.fetchone()


@router.get("/alerts")
async def alerts_get(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        row = await _alerts_row(conn, user["id"])
        await conn.commit()
    bot = await _bot_username(st)
    return {"enabled": bool(st.settings.telegram_bot_token), "bot": bot, "linked": row["chat_id"] is not None,
            "link_url": f"https://t.me/{bot}?start={row['link_code']}" if bot and row["link_code"] else None,
            "min_score": row["min_score"], "watchlist": row["watchlist"], "stops": row["stops"],
            "digest": row["digest"]}


@router.post("/alerts/link")
async def alerts_link(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    bot = await _bot_username(st)
    if not bot:
        raise HTTPException(409, "Alertes indisponibles : l'administrateur doit régler le jeton du bot Telegram.")
    code = alerts.new_link_code()
    async with st.pool.connection() as conn:
        await _alerts_row(conn, user["id"])
        await conn.execute("UPDATE user_alerts SET link_code=%s WHERE user_id=%s", (code, user["id"]))
        await conn.commit()
    return {"link_url": f"https://t.me/{bot}?start={code}"}


@router.delete("/alerts/link")
async def alerts_unlink(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        await conn.execute("UPDATE user_alerts SET chat_id=NULL, link_code=NULL WHERE user_id=%s", (user["id"],))
        await conn.commit()
    return {"ok": True}


class AlertPrefs(BaseModel):
    min_score: int = Field(ge=0, le=100)
    watchlist: bool
    stops: bool
    digest: bool = False


@router.put("/alerts")
async def alerts_put(body: AlertPrefs, request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        await _alerts_row(conn, user["id"])
        await conn.execute("UPDATE user_alerts SET min_score=%s, watchlist=%s, stops=%s, digest=%s WHERE user_id=%s",
                           (body.min_score, body.watchlist, body.stops, body.digest, user["id"]))
        await conn.commit()
    return {"ok": True}


@router.post("/alerts/test")
async def alerts_test(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        row = await _alerts_row(conn, user["id"])
        await conn.commit()
    if not st.settings.telegram_bot_token or row["chat_id"] is None:
        raise HTTPException(409, "Telegram n'est pas encore lié à ce compte.")
    tg = alerts.Telegram(st.settings.telegram_bot_token)
    try:
        await tg.send(row["chat_id"], "🔔 Test Sillage : les alertes arrivent bien ici.")
    except alerts.TelegramError as exc:
        raise HTTPException(502, f"Telegram a refusé le message : {exc}")
    finally:
        await tg.close()
    return {"ok": True}


# ---------------------------------------------------------------- backtest


@router.get("/backtest")
async def backtest_get(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute("SELECT created_at, report FROM bt_reports ORDER BY id DESC LIMIT 1")
        rep = await cur.fetchone()
        cur = await conn.execute(
            """SELECT (SELECT coalesce(string_agg(quarter, ', ' ORDER BY quarter), '') FROM bt_quarters) AS quarters,
                      (SELECT count(*) FROM bt_signals) AS signals,
                      (SELECT count(DISTINCT ticker) FROM bt_signals WHERE code='P') AS tickers,
                      (SELECT count(*) FROM price_history_state) AS histories""")
        progress = await cur.fetchone()
        await conn.commit()
    return {"report": rep["report"] if rep else None, "created_at": rep["created_at"] if rep else None,
            "progress": progress, "enabled": _state(request).settings.backtest_enabled}


# ---------------------------------------------------------------- fiche action


@router.get("/ticker/{ticker}")
async def ticker_page(ticker: str, request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    t = _ticker(ticker)
    isin = _is_isin(t)
    quote = None if isin else await prices.get_price(st.pool, st.prices, t)
    async with st.pool.connection() as conn:
        try:
            if isin:
                cur = await conn.execute("SELECT fetched_at > now() - interval '20 hours' AS fresh"
                                         " FROM price_history_state WHERE ticker=%s", (t,))
                fr = await cur.fetchone()
                await conn.commit()
                if not (fr and fr["fresh"]):
                    cur = await conn.execute("SELECT max(venue) AS v FROM amf_trades WHERE isin=%s", (t,))
                    venue = (await cur.fetchone())["v"]
                    _, rows = await st.euronext.history(t, venue)
                    await backtest.store_history(conn, t, rows)
                    if rows:
                        await conn.execute(
                            "INSERT INTO prices (ticker, price, currency, fetched_at) VALUES (%s,%s,'EUR',now())"
                            " ON CONFLICT (ticker) DO UPDATE SET price=EXCLUDED.price, currency='EUR', fetched_at=now()",
                            (t, rows[-1][1]))
                        await conn.commit()
                cur = await conn.execute("SELECT price, currency, fetched_at FROM prices WHERE ticker=%s", (t,))
                q = await cur.fetchone()
                await conn.commit()
                quote = {"price": float(q["price"]), "currency": q["currency"], "fetched_at": q["fetched_at"],
                         "stale": False} if q else None
            else:
                await backtest.ensure_history(conn, st.history, t)
        except httpx.HTTPError:
            await conn.rollback()
        cur = await conn.execute("SELECT day, close FROM price_history WHERE ticker=%s AND day >= current_date - 365"
                                 " ORDER BY day", (t,))
        history = [{"d": r["day"], "c": float(r["close"])} for r in await cur.fetchall()]
        sigs = await _build_signals(conn, source="all", side="all", fast=False, days=365, ticker=t, limit=500,
                                    score_version=st.settings.score_version)
        sigs.sort(key=lambda s: s["filed_date"] or date.min, reverse=True)
        cur = await conn.execute(
            "SELECT accession, person, relationship, shares, market_value, approx_sale_date, filed_date, plan_10b5_1, url"
            " FROM sale_notices WHERE ticker=%s AND filed_date >= current_date - 180 ORDER BY filed_date DESC", (t,))
        notices = await cur.fetchall()
        cur = await conn.execute(
            "SELECT accession, form_type, filer_name, filed_date, url FROM ownership_filings WHERE ticker=%s"
            " ORDER BY filed_date DESC LIMIT 20", (t,))
        ownership = await cur.fetchall()
        cur = await conn.execute(
            """WITH latest AS (SELECT DISTINCT ON (cik) cik, accession, fund_name, period, total_value
                               FROM fund_filings ORDER BY cik, period DESC),
                    prev AS (SELECT f.cik, f.accession FROM fund_filings f JOIN latest l USING (cik)
                             WHERE f.period < l.period),
                    cus AS (SELECT cusip FROM cusip_tickers WHERE ticker=%s)
               SELECT l.fund_name, l.period, h.shares, h.value, h.value / nullif(l.total_value, 0) * 100 AS weight_pct,
                      (SELECT sum(ph.shares) FROM fund_holdings ph JOIN prev p ON p.accession = ph.accession
                        WHERE p.cik = l.cik AND ph.cusip IN (SELECT cusip FROM cus) AND ph.put_call = '') AS prev_shares
               FROM latest l JOIN fund_holdings h ON h.accession = l.accession
               WHERE h.cusip IN (SELECT cusip FROM cus) AND h.put_call = ''
               ORDER BY h.value DESC""", (t,))
        holders = [{**r, "status": funds13f.classify(_f(r["shares"]), _f(r["prev_shares"]))}
                   for r in await cur.fetchall()]
        cur = await conn.execute("SELECT day, short_volume / total_volume * 100 AS pct FROM short_volume"
                                 " WHERE ticker=%s ORDER BY day", (t,))
        short = [{"d": r["day"], "pct": round(float(r["pct"]), 1)} for r in await cur.fetchall()]
        cur = await conn.execute("SELECT cik, title FROM cik_tickers WHERE ticker=%s LIMIT 1", (t,))
        name = await cur.fetchone()
        cur = await conn.execute("SELECT 1 FROM watchlist WHERE user_id=%s AND ticker=%s", (user["id"], t))
        in_watch = (await cur.fetchone()) is not None
        cur = await conn.execute("SELECT day, timing FROM earnings WHERE ticker=%s AND day >= current_date"
                                 " ORDER BY day LIMIT 1", (t,))
        next_earnings = await cur.fetchone()
        market_cap, buybacks_rows, contracts = None, [], None
        if name:
            cur = await conn.execute("SELECT shares FROM company_shares WHERE cik=%s", (name["cik"],))
            sh = await cur.fetchone()
            if sh and quote:
                market_cap = float(sh["shares"]) * quote["price"]
            cur = await conn.execute("SELECT accession, filed FROM buybacks WHERE cik=%s ORDER BY filed DESC LIMIT 5",
                                     (name["cik"],))
            buybacks_rows = await cur.fetchall()
            contracts = await usaspending.cached(conn, t, name["title"])
        await conn.commit()
    company = name["title"] if name else (sigs[0]["company"] if sigs else None)
    return {"ticker": t, "isin": isin, "name": company, "quote": quote,
            "history": history, "signals": sigs, "notices": notices, "ownership": ownership, "holders": holders,
            "short": short, "in_watchlist": in_watch, "next_earnings": next_earnings, "market_cap": market_cap,
            "buybacks": buybacks_rows, "contracts": contracts}


# ---------------------------------------------------------------- score v2, track records, Sénat


@router.get("/model")
async def model_info(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        cur = await conn.execute("SELECT created_at, metrics, active, model->'weights' AS weights FROM score_models"
                                 " WHERE side='P' ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        await conn.commit()
    if not row:
        return {"model": None, "version": st.settings.score_version}
    weights = sorted(((k, v) for k, v in (row["weights"] or {}).items()), key=lambda kv: kv[1], reverse=True)
    return {"model": {"created_at": row["created_at"], "metrics": row["metrics"], "active": row["active"],
                      "weights": [{"feature": k, "label": score_model.LABELS.get(k) or score_model.describe_feature(k),
                                   "weight": v} for k, v in weights]},
            "version": st.settings.score_version,
            "in_use": signals_mod.use_v2(row, st.settings.score_version)}


@router.get("/tracks")
async def tracks(request: Request, kind: Literal["insider", "congress"] = "insider",
                 min_n: int = Query(3, ge=1, le=50), order: Literal["best", "worst", "most"] = "best",
                 user: dict = Depends(current_user)):
    sort = {"best": "wins::float / n DESC, median_63 DESC", "worst": "wins::float / n ASC, median_63 ASC",
            "most": "n DESC"}[order]
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute(f"SELECT person_key, name, n, wins, mean_63, median_63, last_trade FROM track_records"
                                 f" WHERE kind=%s AND n >= %s ORDER BY {sort} LIMIT 100", (kind, min_n))
        items = await cur.fetchall()
        await conn.commit()
    return {"items": [{**r, "win_rate": round(r["wins"] / r["n"] * 100, 1)} for r in items]}


@router.post("/admin/senate-test")
async def senate_test(request: Request, user: dict = Depends(admin_user)):
    return await senate.probe()


# ---------------------------------------------------------------- résumé (tableau de bord)


@router.get("/summary")
async def summary(request: Request, user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        sigs = await _build_signals(conn, source="all", side="all", fast=False, days=3, ticker=None, limit=3000,
                                    score_version=st.settings.score_version)
        cur = await conn.execute("SELECT ticker FROM watchlist WHERE user_id=%s", (user["id"],))
        watch = {r["ticker"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT DISTINCT ticker FROM paper_positions WHERE user_id=%s", (user["id"],))
        held = {r["ticker"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT accession, issuer_name, ticker, person, relationship, market_value, approx_sale_date, filed_date,"
            " plan_10b5_1 FROM sale_notices WHERE filed_date >= current_date - 2 AND market_value IS NOT NULL"
            " ORDER BY market_value DESC LIMIT 8")
        notices = await cur.fetchall()
        mine = sorted(watch | held)
        cur = await conn.execute(
            "SELECT ticker, day, timing FROM earnings WHERE ticker = ANY(%s) AND day BETWEEN current_date"
            " AND current_date + 14 ORDER BY day", (mine,))
        upcoming = await cur.fetchall()
        cur = await conn.execute(
            """SELECT f.fund_name, f.period, f.filed_date FROM fund_filings f
               WHERE f.filed_date >= current_date - 7 ORDER BY f.filed_date DESC LIMIT 10""")
        new_13f = await cur.fetchall()
        books = {}
        for book in paper.BOOKS:
            cur = await conn.execute("SELECT 1 FROM paper_accounts WHERE user_id=%s AND book=%s", (user["id"], book))
            if await cur.fetchone():
                s = await paper.summary(conn, user["id"], st.settings.paper_start_cash, book)
                books[book] = {k: s[k] for k in ("equity", "total_pct", "unrealized", "realized", "cash")}
                books[book]["positions"] = len(s["positions"])
        await conn.commit()
    today = date.today()
    recent = [s for s in sigs if s.get("filed_date") and (today - s["filed_date"]).days <= 1]
    buys = sorted([s for s in recent if s["side"] == "buy"], key=lambda s: s["score"], reverse=True)
    mine_sig = [s for s in sigs if (s.get("ticker") or s.get("symbol")) in watch | held]
    return {
        "date": today, "counts": {"signals_24h": len(recent), "buys_24h": len(buys)},
        "top_buys": buys[:10], "watch_signals": mine_sig[:10], "notices": notices, "earnings": upcoming,
        "new_13f": new_13f, "books": books,
    }


# ---------------------------------------------------------------- exports CSV


def _csv(rows: list[dict], columns: list[tuple[str, str]], filename: str) -> Response:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([label for _, label in columns])
    for r in rows:
        w.writerow(["" if r.get(k) is None else (str(r.get(k)).replace(".", ",") if isinstance(r.get(k), float)
                                                  else r.get(k)) for k, _ in columns])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/signals.csv")
async def export_signals(request: Request, source: Literal["all", "insider", "congress", "amf", "bafin", "europe"] = "all",
                         side: Literal["all", "buy", "sell", "option"] = "all", days: int = Query(30, ge=1, le=365),
                         user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        items = await _build_signals(conn, source=source, side=side, fast=False, days=days, ticker=None, limit=5000,
                                     score_version=st.settings.score_version)
    items.sort(key=lambda s: (s["filed_date"] or date.min), reverse=True)
    for s in items:
        s["symbol_"] = s.get("ticker") or s.get("symbol")
    cols = [("filed_date", "Déclaré le"), ("trade_date", "Trade"), ("source", "Source"), ("symbol_", "Ticker/ISIN"),
            ("company", "Société"), ("person", "Qui"), ("role", "Rôle"), ("side", "Sens"), ("shares", "Actions"),
            ("price", "Prix"), ("value", "Montant"), ("currency", "Devise"), ("amount_range", "Tranche"),
            ("delay_days", "Délai (j)"), ("score", "Score"), ("score_version", "Version du score"),
            ("probability", "Probabilité (%)"), ("url", "Lien")]
    return _csv(items, cols, f"sillage-signaux-{date.today()}.csv")


@router.get("/export/paper.csv")
async def export_paper(request: Request, book: Book = "manual", user: dict = Depends(current_user)):
    st = _state(request)
    async with st.pool.connection() as conn:
        s = await paper.summary(conn, user["id"], st.settings.paper_start_cash, book)
    cols = [("created_at", "Date"), ("ticker", "Ticker"), ("side", "Sens"), ("quantity", "Quantité"),
            ("price", "Prix ($)"), ("pnl", "Résultat ($)"), ("reason", "Motif"), ("signal_ref", "Signal"),
            ("note", "Note")]
    return _csv(s["orders"], cols, f"sillage-fictif-{book}-{date.today()}.csv")


@router.get("/export/backtest.csv")
async def export_backtest(request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        cur = await conn.execute("SELECT report FROM bt_reports ORDER BY id DESC LIMIT 1")
        r = await cur.fetchone()
        await conn.commit()
    rows = []
    for g in (r["report"]["groups"] if r else []):
        for h, st_ in g["stats"].items():
            if st_:
                rows.append({"side": g["side"], "dimension": g["dimension"], "label": g["label"], "h": int(h),
                             **st_})
    cols = [("side", "Signaux"), ("dimension", "Critère"), ("label", "Tranche"), ("h", "Horizon (séances)"),
            ("n", "Nombre"), ("mean", "Moyenne vs référence (%)"), ("median", "Médiane (%)"), ("win", "Gagnants (%)")]
    return _csv(rows, cols, f"sillage-backtest-{date.today()}.csv")


@router.get("/export/funds13f/{cik}.csv")
async def export_13f(cik: int, request: Request, user: dict = Depends(current_user)):
    async with _state(request).pool.connection() as conn:
        filings, moves = await _fund_moves(conn, cik)
        await conn.commit()
    cols = [("status", "Mouvement"), ("ticker", "Ticker"), ("issuer", "Société"), ("cusip", "CUSIP"),
            ("put_call", "Option"), ("shares", "Actions"), ("prev_shares", "Actions trimestre précédent"),
            ("change_pct", "Variation (%)"), ("value", "Valeur ($)"), ("weight_pct", "Poids (%)")]
    return _csv(moves, cols, f"sillage-13f-{cik}-{date.today()}.csv")
