"""Contrats publics fédéraux (USAspending) : montant engagé sur 12 mois au profit d'une société."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import httpx

from .. import log

_log = log.get("usaspending")
URL = "https://api.usaspending.gov/api/v2/search/spending_by_category/recipient/"
SUFFIXES = r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LLC|PLC|HOLDINGS?|GROUP|SA|NV|THE)\b\.?"


def search_name(company: str | None) -> str | None:
    """« Lockheed Martin Corp » → « LOCKHEED MARTIN » (recherche par nom de bénéficiaire)."""
    if not company:
        return None
    n = re.sub(r"[,.&/]", " ", company.upper())
    n = re.sub(SUFFIXES, " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n if len(n) >= 3 else None


def parse(data: dict, wanted: str) -> tuple[float, list[dict]]:
    """Ne garde que les bénéficiaires dont le nom commence par le nom cherché (évite les homonymes partiels)."""
    total, rec = 0.0, []
    for r in data.get("results", []):
        name = (r.get("name") or "").upper()
        if not name.startswith(wanted):
            continue
        amount = float(r.get("amount") or 0)
        total += amount
        rec.append({"name": r.get("name"), "amount": amount})
    rec.sort(key=lambda x: x["amount"], reverse=True)
    return total, rec[:5]


async def fetch(company: str | None, transport: httpx.AsyncBaseTransport | None = None) -> tuple[str | None, float | None, list]:
    name = search_name(company)
    if not name:
        return None, None, []
    end = date.today()
    body = {"filters": {"recipient_search_text": [name], "award_type_codes": ["A", "B", "C", "D"],
                        "time_period": [{"start_date": (end - timedelta(days=365)).isoformat(),
                                         "end_date": end.isoformat()}]},
            "category": "recipient", "limit": 20, "page": 1}
    async with httpx.AsyncClient(timeout=30, transport=transport) as client:
        resp = await client.post(URL, json=body)
        resp.raise_for_status()
        total, rec = parse(resp.json(), name)
    return name, total, rec


async def cached(conn, ticker: str, company: str | None, max_age_days: int = 7) -> dict | None:
    cur = await conn.execute("SELECT *, fetched_at > now() - make_interval(days => %s) AS fresh FROM gov_contracts"
                             " WHERE ticker=%s", (max_age_days, ticker))
    row = await cur.fetchone()
    await conn.commit()
    if row and row["fresh"]:
        return row
    try:
        name, total, rec = await fetch(company)
    except httpx.HTTPError as exc:
        _log.warning("USAspending indisponible pour %s : %s", ticker, exc)
        return row
    await conn.execute(
        "INSERT INTO gov_contracts (ticker, search_name, amount_12m, recipients, fetched_at) VALUES (%s,%s,%s,%s,now())"
        " ON CONFLICT (ticker) DO UPDATE SET search_name=EXCLUDED.search_name, amount_12m=EXCLUDED.amount_12m,"
        " recipients=EXCLUDED.recipients, fetched_at=now()", (ticker, name, total, json.dumps(rec)))
    await conn.commit()
    return {"ticker": ticker, "search_name": name, "amount_12m": total, "recipients": rec}
