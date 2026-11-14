"""Tests de l'API sur une vraie base PostgreSQL.

Lancés seulement si TEST_DATABASE_URL est défini et pointe vers une base dont le nom finit par « _test »
(elle est entièrement vidée au début). Exemple local :
  TEST_DATABASE_URL=postgresql://sillage:sillage-local@postgres:5432/sillage_test
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import psycopg
import pytest

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.rsplit("/", 1)[-1].endswith("_test"),
                                reason="TEST_DATABASE_URL (base *_test) non défini")

ADMIN = ("admin@example.test", "admin-password-123")


class FakePrices:
    """Remplace Yahoo/Nasdaq : cours fixes."""
    quotes = {"AAPL": 100.0, "MSFT": 400.0, "ACME": 10.0}

    async def fetch(self, ticker):
        p = self.quotes.get(ticker)
        return (p, "USD") if p else None

    async def close(self):
        pass


class FakeHistory:
    async def fetch(self, ticker, start, end):
        return [(end - timedelta(days=i), 100.0 + i) for i in range(30, 0, -1)]

    async def close(self):
        pass


def _reset_db():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from sillage.config import load_settings
    from sillage.main import create_app

    _reset_db()
    settings = load_settings({"DATABASE_URL": URL, "JWT_SECRET": "t" * 40, "ADMIN_EMAIL": ADMIN[0],
                              "ADMIN_PASSWORD": ADMIN[1], "INGEST_ENABLED": "false"})
    app = create_app(settings)
    with TestClient(app) as c:
        app.state.prices = FakePrices()
        app.state.history = FakeHistory()
        c.app_ref = app
        yield c


def sql(query, params=()):
    with psycopg.connect(URL, autocommit=True) as c:
        cur = c.execute(query, params)
        return cur.fetchall() if cur.description else None


def login(client, email, password):
    client.cookies.clear()
    client.app_ref.state.limiter._hits.clear()  # les tests se connectent souvent depuis la même « IP »
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------- comptes

def test_health_and_auth(client):
    assert client.get("/health").text == "ok"
    assert client.get("/api/v1/signals").status_code == 401
    r = client.post("/api/v1/auth/register", json={"email": ADMIN[0], "password": "x" * 12})
    assert r.status_code == 403  # adresse admin réservée
    r = client.post("/api/v1/auth/register", json={"email": "u1@example.test", "password": "court"})
    assert r.status_code == 400
    r = client.post("/api/v1/auth/register", json={"email": "u1@example.test", "password": "motdepasse-u1"})
    assert r.status_code == 200 and "sillage_session" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    old_token = client.cookies.get("sillage_session")
    assert client.get("/api/v1/auth/me").json()["user"]["email"] == "u1@example.test"
    r = client.post("/api/v1/auth/password", json={"current_password": "motdepasse-u1",
                                                   "new_password": "nouveau-mdp-u1"})
    assert r.status_code == 200
    client.cookies.clear()
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert r.status_code == 401  # les autres sessions sont déconnectées
    login(client, "u1@example.test", "nouveau-mdp-u1")
    assert client.get("/api/v1/admin/settings").status_code == 403


def test_rate_limit(client):
    client.cookies.clear()
    codes = [client.post("/api/v1/auth/login", json={"email": "x@example.test", "password": "y" * 10},
                         headers={"X-Real-IP": "203.0.113.9"}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


# ---------------------------------------------------------------- fictif

def test_paper_manual_and_isin(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    r = client.post("/api/v1/paper/orders", json={"ticker": "AAPL", "side": "buy", "quantity": 10})
    assert r.status_code == 200 and r.json()["price"] == 100.0
    p = client.get("/api/v1/paper").json()
    assert p["cash"] == 25000 - 1000 and p["positions"][0]["stop_price"] == pytest.approx(92.0)
    # action française : cours en euros converti au taux BCE
    sql("INSERT INTO prices (ticker, price, currency) VALUES ('FR0000052292', 50, 'EUR')")
    sql("INSERT INTO fx_rates (currency, per_eur, day) VALUES ('USD', 1.1, current_date)")
    r = client.post("/api/v1/paper/orders", json={"ticker": "FR0000052292", "side": "buy", "quantity": 2})
    assert r.status_code == 200 and r.json()["price"] == pytest.approx(55.0) and r.json()["currency_note"]
    r = client.post("/api/v1/paper/orders", json={"ticker": "AAPL", "side": "sell"})
    assert r.json()["pnl"] == 0
    p = client.get("/api/v1/paper").json()
    assert p["cash"] == pytest.approx(25000 - 110) and p["closed"] == 1
    assert client.get("/api/v1/paper?book=rule").json()["cash"] == 25000  # portefeuille séparé
    assert client.post("/api/v1/paper/orders", json={"ticker": "ZZZZ", "side": "buy", "quantity": 1}).status_code == 400
    csv = client.get("/api/v1/export/paper.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "Ticker" in csv.text and "AAPL" in csv.text
    assert client.post("/api/v1/paper/reset").json()["ok"]
    assert client.get("/api/v1/paper").json()["positions"] == []


# ---------------------------------------------------------------- signaux

def _insert_trade(acc, line, code="P", owner_cik=1, trade=None, filed=None, shares=100, price=10.0, after=1100,
                  ticker="ACME", amendment=False):
    trade = trade or date.today() - timedelta(days=2)
    filed = filed or date.today() - timedelta(days=1)
    sql("""INSERT INTO insider_trades (accession, line_no, issuer_cik, issuer_name, ticker, owner_name, owner_cik,
             is_officer, officer_title, code, shares, price, value, shares_after, trade_date, filed_date, is_amendment)
           VALUES (%s,%s,123,'ACME CORP',%s,'Doe John',%s,true,'CEO',%s,%s,%s,%s,%s,%s,%s,%s)""",
        (acc, line, ticker, owner_cik, code, shares, price, shares * price, after, trade, filed, amendment))


def test_signals_grouping_and_amendments(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    _insert_trade("ACC-1", 1, shares=100, price=10, after=1100)
    _insert_trade("ACC-1", 2, shares=300, price=12, after=1400)
    items = client.get("/api/v1/signals?source=insider&days=7").json()["items"]
    assert len(items) == 1
    s = items[0]
    assert s["lines"] == 2 and s["shares"] == 400 and s["price"] == pytest.approx(11.5)
    assert s["position_pct"] == pytest.approx(40.0)  # 1000 → 1400
    detail = client.get(f"/api/v1/signals/{s['id']}").json()
    assert detail["ticker"] == "ACME" and detail["current_price"] == 10.0 and detail["position"] is None
    # correction 4/A : la déclaration d'origine sort du flux
    from sillage.ingest import sec
    form = sec.Form4(issuer_cik=123, issuer_name="ACME CORP", ticker="ACME", owner_name="Doe John", owner_cik=1,
                     is_director=False, is_officer=True, is_ten_percent=False, officer_title="CEO",
                     plan_10b5_1=False, trades=[{"trade_date": date.today() - timedelta(days=2)}])

    async def amend():
        async with await psycopg.AsyncConnection.connect(URL) as c:
            n = await sec.apply_amendment(c, "ACC-2", form, date.today())
            await c.commit()
            return n
    assert asyncio.run(amend()) == 2
    _insert_trade("ACC-2", 1, shares=50, price=10, after=1050, amendment=True)
    items = client.get("/api/v1/signals?source=insider&days=7").json()["items"]
    assert [i["accession"] for i in items] == ["ACC-2"] and items[0]["shares"] == 50
    csv = client.get("/api/v1/export/signals.csv?days=7").text
    assert csv.splitlines()[0].startswith("﻿Déclaré le") and "ACME" in csv


def test_summary_ticker_and_watchlist(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    assert client.put("/api/v1/watchlist/ACME").json()["ok"]
    summ = client.get("/api/v1/summary").json()
    assert summ["counts"]["signals_24h"] >= 1 and summ["top_buys"][0]["ticker"] == "ACME"
    assert summ["watch_signals"] and "manual" in summ["books"]
    page = client.get("/api/v1/ticker/ACME").json()
    assert page["ticker"] == "ACME" and len(page["history"]) == 30 and page["signals"] and page["in_watchlist"]
    assert client.get("/api/v1/ticker/A;B").status_code == 400


# ---------------------------------------------------------------- réglages et règle automatique

def test_admin_settings(client):
    login(client, *ADMIN)
    r = client.put("/api/v1/admin/settings", json={"values": {"ingest_interval_min": "0"}})
    assert r.status_code == 400
    r = client.put("/api/v1/admin/settings", json={"values": {"score_version": "v1", "bafin_enabled": "non"}})
    assert r.status_code == 200
    items = {i["key"]: i for i in r.json()["items"]}
    assert items["score_version"]["value"] == "v1" and items["bafin_enabled"]["value"] is False
    assert client.app_ref.state.settings.score_version == "v1"
    client.put("/api/v1/admin/settings", json={"values": {"score_version": None, "bafin_enabled": None}})
    assert client.app_ref.state.settings.score_version == "auto"


def test_rule_engine(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    r = client.put("/api/v1/paper/rule", json={"enabled": True, "min_score": 0, "alloc_pct": 5, "hold_days": 90,
                                               "max_positions": 10, "sources": ["insider"]})
    assert r.status_code == 200 and r.json()["enabled"]
    sql("UPDATE paper_rules SET last_check = now() - interval '1 day'")
    _insert_trade("ACC-3", 1, owner_cik=2, ticker="MSFT", shares=10, price=400, after=20)
    from sillage import rules, signals

    async def run():
        app = client.app_ref
        async with app.state.pool.connection() as conn:
            recent = await signals.build(conn, days=14)
            return await rules.run(app.state.pool, app.state.prices, conn, recent, 25000)
    stats = client.portal.call(run)
    assert stats["bought"] >= 1
    book = client.get("/api/v1/paper?book=rule").json()
    held = {p["ticker"] for p in book["positions"]}
    assert "MSFT" in held and book["orders"][0]["reason"] == "rule"
    # 5 % de 25 000 $ = 1 250 $ : 3 actions MSFT à 400 $
    assert next(p for p in book["positions"] if p["ticker"] == "MSFT")["quantity"] == 3


# ---------------------------------------------------------------- v2.1

def test_positions_partial_trailing_target(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    client.post("/api/v1/paper/reset")
    assert client.post("/api/v1/paper/orders", json={"ticker": "AAPL", "side": "buy", "quantity": 10}).status_code == 200
    r = client.post("/api/v1/paper/orders", json={"ticker": "AAPL", "side": "sell", "quantity": 4})
    assert r.status_code == 200 and r.json()["remaining"] == 6
    assert client.post("/api/v1/paper/orders", json={"ticker": "AAPL", "side": "sell", "quantity": 99}).status_code == 400
    r = client.put("/api/v1/paper/positions/AAPL", json={"trailing_pct": 10, "target_price": 90, "note": "x"})
    assert r.status_code == 400  # objectif sous le prix moyen
    r = client.put("/api/v1/paper/positions/AAPL", json={"stop_pct": 5, "trailing_pct": 10, "target_price": 130,
                                                          "note": "achat du CEO"})
    assert r.status_code == 200 and r.json()["stop_price"] == pytest.approx(95.0)  # max(100×0,95 ; 100×0,90)
    from sillage import paper

    def run_stops():
        async def go():
            async with client.app_ref.state.pool.connection() as conn:
                return await paper.apply_stops(conn)
        return client.portal.call(go)
    sql("INSERT INTO prices (ticker, price, currency) VALUES ('AAPL', 120, 'USD') ON CONFLICT (ticker)"
        " DO UPDATE SET price=120, fetched_at=now()")
    assert run_stops() == 0  # le stop suiveur monte : 120 × 0,9 = 108
    pos = client.get("/api/v1/paper").json()["positions"][0]
    assert pos["stop_price"] == pytest.approx(108.0) and pos["note"] == "achat du CEO"
    sql("UPDATE prices SET price=107 WHERE ticker='AAPL'")
    assert run_stops() == 1
    orders = client.get("/api/v1/paper").json()["orders"]
    assert orders[0]["reason"] == "trailing" and orders[0]["pnl"] == pytest.approx(6 * 7)
    sql("UPDATE prices SET price=100 WHERE ticker='AAPL'")


def test_price_alerts(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    r = client.post("/api/v1/price-alerts", json={"ticker": "MSFT", "direction": "below", "price": 390})
    assert r.status_code == 200
    sql("INSERT INTO prices (ticker, price, currency) VALUES ('MSFT', 385, 'USD') ON CONFLICT (ticker)"
        " DO UPDATE SET price=385")
    from sillage import price_alerts

    async def go():
        async with client.app_ref.state.pool.connection() as conn:
            return await price_alerts.check(conn, None, "")
    assert client.portal.call(go)["triggered"] == 1
    items = client.get("/api/v1/price-alerts").json()["items"]
    assert items[0]["triggered_at"] and float(items[0]["triggered_price"]) == 385
    assert client.get("/api/v1/summary").json()["price_alerts"]
    assert "MSFT" in {w["ticker"] for w in client.get("/api/v1/watchlist").json()["items"]}


def test_admin_users_and_health(client):
    login(client, *ADMIN)
    users = client.get("/api/v1/admin/users").json()["items"]
    u1 = next(u for u in users if u["email"] == "u1@example.test")
    assert client.post(f"/api/v1/admin/users/{u1['id']}", json={"action": "disable"}).json()["ok"]
    client.cookies.clear()
    r = client.post("/api/v1/auth/login", json={"email": "u1@example.test", "password": "nouveau-mdp-u1"})
    assert r.status_code == 403
    login(client, *ADMIN)
    client.post(f"/api/v1/admin/users/{u1['id']}", json={"action": "enable"})
    r = client.post(f"/api/v1/admin/users/{u1['id']}", json={"action": "reset_password"}).json()
    temp = r["temporary_password"]
    login(client, "u1@example.test", temp)
    client.post("/api/v1/auth/password", json={"current_password": temp, "new_password": "nouveau-mdp-u1"})
    assert client.get("/api/v1/admin/health").status_code == 403
    login(client, *ADMIN)
    me = next(u for u in client.get("/api/v1/admin/users").json()["items"] if u["email"] == ADMIN[0])
    assert client.post(f"/api/v1/admin/users/{me['id']}", json={"action": "disable"}).status_code == 400
    h = client.get("/api/v1/admin/health").json()
    assert h["queues"]["db_bytes"] > 0 and h["tables"] and "jobs" in h and h["memory_mb"]


def test_market_network_convergence(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    m = client.get("/api/v1/market/insiders?weeks=8").json()
    assert m["series"] and m["series"][-1]["buyers"] >= 1 and m["sectors"]
    net = client.get("/api/v1/insider/1").json()
    assert net["name"] == "Doe John" and net["companies"][0]["ticker"] == "ACME"
    assert any(p["owner_cik"] == 2 and p["shared"] == 1 for p in net["peers"])  # même émetteur (CIK 123) en test
    assert client.get("/api/v1/insider/999999").status_code == 404
    # 13F : un fonds qui renforce ACME + un initié acheteur → convergence
    sql("INSERT INTO fund_filings (accession, cik, fund_name, period, filed_date, total_value) VALUES"
        " ('F1', 9, 'Fonds', current_date - 200, current_date - 150, 1000), ('F2', 9, 'Fonds', current_date - 110,"
        " current_date - 60, 1000)")
    sql("INSERT INTO fund_holdings (accession, cusip, put_call, issuer, shares, value) VALUES"
        " ('F1', 'C1', '', 'ACME', 100, 10), ('F2', 'C1', '', 'ACME', 200, 20)")
    sql("INSERT INTO cusip_tickers (cusip, ticker) VALUES ('C1', 'ACME')")
    conv = {c["ticker"]: c for c in client.get("/api/v1/convergence").json()["items"]}
    assert conv["ACME"]["funds"] == 1 and conv["ACME"]["insiders"] >= 1


def test_rule_simulation(client):
    login(client, "u1@example.test", "nouveau-mdp-u1")
    base = date.today() - timedelta(days=300)
    days = [base + timedelta(days=i) for i in range(260) if (base + timedelta(days=i)).weekday() < 5]
    for t, f in (("SPY", lambda i: 100 + i * 0.1), ("IWM", lambda i: 100), ("GAIN", lambda i: 10 + i * 0.05),
                 ("LOSS", lambda i: 10 - i * 0.02)):
        sql("DELETE FROM price_history WHERE ticker=%s", (t,))
        with psycopg.connect(URL, autocommit=True) as c:
            c.cursor().executemany("INSERT INTO price_history (ticker, day, close, idx) VALUES (%s,%s,%s,%s)",
                                   [(t, d, f(i), i) for i, d in enumerate(days)])
    for i, (t, owner) in enumerate((("GAIN", 11), ("LOSS", 12))):
        sql("""INSERT INTO bt_signals (accession, code, quarter, ticker, issuer_cik, owner_cik, owner_name, relationship,
                 officer_title, shares, value, price, shares_after, trade_date, filed_date, plan_10b5_1)
               VALUES (%s,'P','2026q1',%s,%s,%s,'X','Officer','CEO',1000,10000,10,2000,%s,%s,false)""",
            (f"BT{i}", t, 500 + i, owner, days[10], days[11]))
    r = client.post("/api/v1/paper/rule/simulate", json={"score": "v1", "min_score": 0, "alloc_pct": 10,
                                                         "hold_days": 60, "max_positions": 5}).json()
    assert r["trades"] == 2 and r["exits"]["horizon"] >= 1 and r["bench_pct"] > 0
    best = r["best"][0]
    assert best["ticker"] == "GAIN" and best["ret"] > 0 and r["curve"]
    r2 = client.post("/api/v1/paper/rule/simulate", json={"score": "v1", "min_score": 101, "alloc_pct": 10,
                                                          "hold_days": 60, "max_positions": 5})
    assert r2.status_code == 422
