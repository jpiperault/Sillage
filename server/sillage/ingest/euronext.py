"""Cours de clôture des actions européennes cotées sur Euronext (CSV public, par ISIN) et taux BCE."""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta

import httpx

from .. import log
from .prices import BROWSER_UA

_log = log.get("euronext")
HISTORY_URL = ("https://live.euronext.com/en/ajax/AwlHistoricalPrice/getFullDownloadAjax/{isin}-{mic}"
               "?format=csv&decimal_separator=.&date_form=d/m/Y&op=&adjusted=&base100=&startdate={start}&enddate={end}")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
# Lieu indiqué dans la déclaration AMF → marché Euronext (MIC)
VENUE_MIC = [("growth", "ALXP"), ("access", "XMLI"), ("alternext", "ALXP"), ("paris", "XPAR"),
             ("amsterdam", "XAMS"), ("brussels", "XBRU"), ("bruxelles", "XBRU"), ("lisbon", "XLIS"),
             ("milan", "MTAA"), ("dublin", "XMSM"), ("oslo", "XOSL")]
MICS_TRIED = ("XPAR", "ALXP", "XMLI")


def mic_for(venue: str | None) -> list[str]:
    v = (venue or "").lower()
    for key, mic in VENUE_MIC:
        if key in v:
            return [mic] + [m for m in MICS_TRIED if m != mic]
    return list(MICS_TRIED)


def parse_csv(text: str) -> list[tuple[date, float]]:
    out = []
    for line in text.splitlines():
        parts = line.strip().split(";")
        if len(parts) < 6 or not re.match(r"\d{2}/\d{2}/\d{4}$", parts[0]):
            continue
        try:
            d = datetime.strptime(parts[0], "%d/%m/%Y").date()
            close = float(parts[5])
        except ValueError:
            continue
        if close > 0:
            out.append((d, close))
    return sorted(out)


def parse_ecb(xml: str) -> tuple[date | None, dict[str, float]]:
    m = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", xml)
    rates = {c: float(r) for c, r in re.findall(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([\d.]+)['\"]", xml)}
    return (date.fromisoformat(m.group(1)) if m else None), rates


class EuronextClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(headers={"User-Agent": BROWSER_UA}, timeout=20, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def history(self, isin: str, venue: str | None, days: int = 730) -> tuple[str | None, list]:
        end = date.today()
        start = end - timedelta(days=days)
        for mic in mic_for(venue):
            try:
                resp = await self._client.get(HISTORY_URL.format(isin=isin, mic=mic, start=start, end=end))
            except httpx.HTTPError:
                continue
            if resp.status_code == 200:
                rows = parse_csv(resp.text)
                if rows:
                    return mic, rows
            await asyncio.sleep(0.2)
        return None, []

    async def ecb(self) -> tuple[date | None, dict[str, float]]:
        resp = await self._client.get(ECB_URL)
        resp.raise_for_status()
        return parse_ecb(resp.text)


async def refresh_fx(conn, client: EuronextClient) -> dict:
    day, rates = await client.ecb()
    for cur, rate in rates.items():
        await conn.execute(
            "INSERT INTO fx_rates (currency, per_eur, day) VALUES (%s,%s,%s) ON CONFLICT (currency)"
            " DO UPDATE SET per_eur=EXCLUDED.per_eur, day=EXCLUDED.day, updated_at=now()", (cur, rate, day))
    await conn.commit()
    return {"day": day, "usd": rates.get("USD")}


async def eur_usd(conn) -> float | None:
    cur = await conn.execute("SELECT per_eur FROM fx_rates WHERE currency='USD'")
    r = await cur.fetchone()
    return float(r["per_eur"]) if r else None


async def refresh_prices(conn, client: EuronextClient, store_history, limit: int = 60) -> dict:
    """Cours des ISIN suivis (déclarations AMF récentes, positions et watchlist) : historique + dernier cours."""
    cur = await conn.execute(
        """SELECT t.isin, max(t.venue) AS venue FROM (
             SELECT isin, venue FROM amf_trades WHERE published >= current_date - 120 AND isin IS NOT NULL
             UNION ALL SELECT isin, venue FROM bafin_trades WHERE published >= current_date - 120 AND isin LIKE 'FR%%'
             UNION ALL SELECT ticker, NULL FROM paper_positions WHERE ticker ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'
             UNION ALL SELECT ticker, NULL FROM watchlist WHERE ticker ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$') t
           LEFT JOIN price_history_state s ON s.ticker = t.isin
           WHERE s.ticker IS NULL OR s.fetched_at < now() - interval '20 hours'
           GROUP BY t.isin ORDER BY t.isin LIMIT %s""", (limit,))
    todo = await cur.fetchall()
    await conn.commit()
    ok = 0
    for r in todo:
        mic, rows = await client.history(r["isin"], r["venue"])
        await store_history(conn, r["isin"], rows)
        if rows:
            ok += 1
            d, close = rows[-1]
            await conn.execute(
                "INSERT INTO prices (ticker, price, currency, fetched_at) VALUES (%s,%s,'EUR',now())"
                " ON CONFLICT (ticker) DO UPDATE SET price=EXCLUDED.price, currency='EUR', fetched_at=now()",
                (r["isin"], close))
            await conn.commit()
    return {"isins": len(todo), "priced": ok}
