"""Annonces de rachat d'actions : recherche plein texte de la SEC dans les 8-K.

https://efts.sec.gov/LATEST/search-index?q="..."&forms=8-K&dateRange=custom&startdt=…&enddt=…&from=N
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import quote

from .. import log

_log = log.get("buybacks")
SEARCH_URL = ("https://efts.sec.gov/LATEST/search-index?q={q}&forms=8-K&dateRange=custom"
              "&startdt={start}&enddt={end}&from={offset}")
QUERIES = ('"share repurchase program"', '"stock repurchase program"', '"buyback program"')
MAX_PAGES = 10


def parse_hits(data: dict) -> list[dict]:
    out = []
    for h in data.get("hits", {}).get("hits", []):
        s = h.get("_source", {})
        acc = s.get("adsh") or (h.get("_id") or "").split(":", 1)[0]
        ciks = s.get("ciks") or []
        if not acc or not ciks or not s.get("file_date"):
            continue
        name = (s.get("display_names") or [""])[0].split("  (")[0].strip() or None
        out.append({"accession": acc, "cik": int(ciks[0]), "company": name,
                    "filed": date.fromisoformat(s["file_date"])})
    return out


async def refresh(client, conn, start: date, end: date) -> int:
    new = 0
    for q in QUERIES:
        for page in range(MAX_PAGES):
            resp = await client.get(SEARCH_URL.format(q=quote(q), start=start.isoformat(), end=end.isoformat(),
                                                      offset=page * 100))
            resp.raise_for_status()
            hits = parse_hits(resp.json())
            for h in hits:
                cur = await conn.execute(
                    "INSERT INTO buybacks (accession, cik, company, filed) VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT DO NOTHING", (h["accession"], h["cik"], h["company"], h["filed"]))
                new += cur.rowcount
            await conn.commit()
            if len(hits) < 100:
                break
    return new


async def refresh_window(client, conn, history_days: int) -> dict:
    """Derniers jours + remontée progressive dans l'historique (90 jours par passage) jusqu'à history_days."""
    today = date.today()
    new = await refresh(client, conn, today - timedelta(days=7), today)
    cur = await conn.execute("SELECT min(filed) AS m FROM buybacks")
    oldest = (await cur.fetchone())["m"] or today
    await conn.commit()
    limit = today - timedelta(days=history_days)
    if oldest > limit:
        start = max(limit, oldest - timedelta(days=90))
        new += await refresh(client, conn, start, oldest)
    return {"new": new}
