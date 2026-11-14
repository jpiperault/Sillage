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
