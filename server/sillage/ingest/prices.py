"""Cours via l'API publique (non officielle) de Yahoo Finance, avec cache en base."""
from __future__ import annotations

import asyncio
import re
import time

import httpx

from .. import log

_log = log.get("prices")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1d"
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{ticker}/info?assetclass={asset}"
CACHE_MINUTES = 15
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


def clean_ticker(ticker: str | None) -> str | None:
    if not ticker:
        return None
    t = ticker.strip().upper()
    return t if _TICKER_RE.match(t) else None


def parse_chart(data: dict) -> tuple[float, str | None] | None:
    try:
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        return float(price), meta.get("currency")
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def parse_nasdaq(data: dict) -> tuple[float, str | None] | None:
    try:
        raw = data["data"]["primaryData"]["lastSalePrice"]
        price = float(str(raw).replace("$", "").replace(",", "").strip())
        return (price, "USD") if price > 0 else None
    except (KeyError, TypeError, ValueError):
        return None


class PriceClient:
    """Yahoo d'abord ; en cas d'échec, l'API publique (non officielle) de Nasdaq.

    Yahoo renvoie parfois 429 pour toute une adresse IP : il est alors mis de côté 10 minutes.
    """

    YAHOO_COOLDOWN_S = 600

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=httpx.Timeout(8.0, connect=5.0),
            transport=transport,
        )
        self._yahoo_off_until = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def _yahoo(self, ticker: str) -> tuple[float, str | None] | None:
        if time.monotonic() < self._yahoo_off_until:
            return None
        resp = await self._client.get(CHART_URL.format(ticker=ticker.replace(".", "-")))
        if resp.status_code == 429:
            self._yahoo_off_until = time.monotonic() + self.YAHOO_COOLDOWN_S
            _log.warning("Yahoo renvoie 429 : source de secours Nasdaq pendant 10 min")
            return None
        if resp.status_code != 200:
            return None
        return parse_chart(resp.json())

    async def _nasdaq(self, ticker: str) -> tuple[float, str | None] | None:
        for asset in ("stocks", "etf"):
            resp = await self._client.get(NASDAQ_URL.format(ticker=ticker, asset=asset))
            if resp.status_code == 200:
                got = parse_nasdaq(resp.json())
                if got:
                    return got
        return None

    async def fetch(self, ticker: str) -> tuple[float, str | None] | None:
        for source in (self._yahoo, self._nasdaq):
            try:
                got = await source(ticker)
                if got:
                    return got
            except (httpx.HTTPError, ValueError):
                continue
        return None


async def cached(conn, ticker: str) -> dict | None:
    cur = await conn.execute(
        "SELECT price, currency, fetched_at, fetched_at > now() - make_interval(mins => %s) AS fresh"
        " FROM prices WHERE ticker = %s",
        (CACHE_MINUTES, ticker),
    )
    return await cur.fetchone()


async def get_price(pool, client: PriceClient, ticker: str, force: bool = False) -> dict | None:
    """Renvoie {price, currency, fetched_at, stale}. Dernier cours connu si Yahoo échoue."""
    t = clean_ticker(ticker)
    if t is None:
        return None
    async with pool.connection() as conn:
        row = await cached(conn, t)
    if row and row["fresh"] and not force:
        return {"price": float(row["price"]), "currency": row["currency"], "fetched_at": row["fetched_at"], "stale": False}
    got = await client.fetch(t)
    if got is None:
        if row:
            return {"price": float(row["price"]), "currency": row["currency"], "fetched_at": row["fetched_at"], "stale": True}
        return None
    price, currency = got
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO prices(ticker, price, currency, fetched_at) VALUES (%s,%s,%s,now())"
            " ON CONFLICT (ticker) DO UPDATE SET price=EXCLUDED.price, currency=EXCLUDED.currency, fetched_at=now()"
            " RETURNING fetched_at",
            (t, price, currency),
        )
        r = await cur.fetchone()
        await conn.commit()
    return {"price": price, "currency": currency, "fetched_at": r["fetched_at"], "stale": False}


async def tickers_to_refresh(conn) -> list[str]:
    cur = await conn.execute(
        """SELECT ticker FROM (
             SELECT ticker FROM paper_positions
             UNION SELECT ticker FROM watchlist
             UNION SELECT ticker FROM (
               SELECT ticker FROM insider_trades
               WHERE code = 'P' AND ticker IS NOT NULL AND filed_date >= current_date - 14
               GROUP BY ticker ORDER BY sum(value) DESC NULLS LAST LIMIT 40) top
           ) t
           WHERE ticker IS NOT NULL
             AND ticker NOT IN (SELECT ticker FROM prices WHERE fetched_at > now() - make_interval(mins => %s))""",
        (CACHE_MINUTES,),
    )
    return [r["ticker"] for r in await cur.fetchall()]


async def refresh_all(pool, client: PriceClient, stop: asyncio.Event | None = None) -> dict:
    async with pool.connection() as conn:
        tickers = await tickers_to_refresh(conn)
    ok = fail = 0
    for t in tickers:
        if stop is not None and stop.is_set():
            break
        res = await get_price(pool, client, t, force=True)
        if res and not res["stale"]:
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.3)
    return {"refreshed": ok, "failed": fail}
