"""Transactions des parlementaires américains via Quiver Quantitative (optionnel, QUIVER_API_KEY)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

import httpx

from .. import log

_log = log.get("congress")

QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"
SOURCE = "quiver"


def _first(row: dict, *keys: str):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def _date(v) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


_AMOUNT_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


def parse_range(text) -> tuple[float | None, float | None]:
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return float(text), float(text)
    nums = [float(n.replace(",", "")) for n in _AMOUNT_RE.findall(str(text))]
    if not nums:
        return None, None
    return nums[0], (nums[1] if len(nums) > 1 else nums[0])


def normalize(row: dict) -> dict | None:
    """Lecture tolérante d'une ligne Quiver ; None si elle n'est ni un achat ni une vente."""
    name = _first(row, "Representative", "Name", "Politician")
    tx = str(_first(row, "Transaction", "Type") or "").lower()
    if "purchase" in tx or tx == "buy":
        side = "buy"
    elif "sale" in tx or tx == "sell":
        side = "sell"
    else:
        return None
    if not name:
        return None
    ticker = _first(row, "Ticker")
    ticker = str(ticker).strip().upper() if ticker else None
    rng = _first(row, "Range", "Amount")
    amin, amax = parse_range(rng)
    trade_date = _date(_first(row, "TransactionDate", "Traded"))
    report_date = _date(_first(row, "ReportDate", "Filed", "Disclosed"))
    party = _first(row, "Party")
    chamber = _first(row, "House", "Chamber")
    key_src = "|".join(str(x) for x in (name, ticker, side, rng, trade_date, report_date))
    return {
        "source_key": hashlib.sha1(key_src.encode()).hexdigest(),
        "politician": str(name).strip(),
        "party": str(party).strip() if party else None,
        "chamber": str(chamber).strip() if chamber else None,
        "ticker": ticker,
        "side": side,
        "amount_range": str(rng) if rng is not None else None,
        "amount_min": amin,
        "amount_max": amax,
        "trade_date": trade_date,
        "report_date": report_date,
        "raw": row,
    }


async def fetch_and_store(api_key: str, conn, transport: httpx.AsyncBaseTransport | None = None) -> int:
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        resp = await client.get(QUIVER_URL, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, list):
        raise ValueError("réponse Quiver inattendue (liste attendue)")
    new = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        n = normalize(row)
        if n is None:
            continue
        cur = await conn.execute(
            """INSERT INTO congress_trades (source, source_key, politician, party, chamber, ticker, side,
                 amount_range, amount_min, amount_max, trade_date, report_date, raw)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source, source_key) DO NOTHING""",
            (SOURCE, n["source_key"], n["politician"], n["party"], n["chamber"], n["ticker"], n["side"],
             n["amount_range"], n["amount_min"], n["amount_max"], n["trade_date"], n["report_date"],
             json.dumps(n["raw"], default=str)),
        )
        new += cur.rowcount
    await conn.commit()
    return new
