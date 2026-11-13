"""FINRA : volume vendu à découvert par action et par jour (Reg SHO, consolidé CNMS).

Fichier texte public publié chaque soir : https://cdn.finra.org/equity/regsho/daily/CNMSshvol<AAAAMMJJ>.txt
Colonnes : Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market.
Attention : c'est le volume des ventes à découvert du jour, pas la position vendeuse (short interest).
"""
from __future__ import annotations

from datetime import date, datetime

import httpx

from .. import log
from .sec import business_days_back

_log = log.get("finra")

URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"
DAYS_KEPT = 45
DAYS_CHECKED = 5
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def parse_file(text: str) -> list[tuple[date, str, float, float]]:
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.strip().split("|")
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        try:
            day = datetime.strptime(parts[0], "%Y%m%d").date()
            short, total = float(parts[2]), float(parts[4])
        except ValueError:
            continue
        if total <= 0:
            continue
        rows.append((day, parts[1].strip().upper(), short, total))
    return rows


async def refresh(conn, today: date | None = None, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    today = today or date.today()
    days = [today] + business_days_back(today, DAYS_CHECKED)
    cur = await conn.execute("SELECT DISTINCT day FROM short_volume WHERE day >= %s", (min(days),))
    have = {r["day"] for r in await cur.fetchall()}
    await conn.commit()
    loaded = 0
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": BROWSER_UA}, transport=transport) as client:
        for d in sorted(set(days) - have):
            if d.weekday() >= 5:
                continue
            resp = await client.get(URL.format(ymd=d.strftime("%Y%m%d")))
            if resp.status_code in (403, 404):  # pas encore publié, ou jour férié
                continue
            resp.raise_for_status()
            rows = parse_file(resp.text)
            async with conn.cursor() as c:
                await c.executemany(
                    "INSERT INTO short_volume (day, ticker, short_volume, total_volume) VALUES (%s,%s,%s,%s)"
                    " ON CONFLICT (ticker, day) DO UPDATE SET short_volume=EXCLUDED.short_volume,"
                    " total_volume=EXCLUDED.total_volume",
                    rows,
                )
            await conn.commit()
            loaded += 1
    await conn.execute("DELETE FROM short_volume WHERE day < %s::date - %s::int", (today, DAYS_KEPT))
    await conn.commit()
    return {"days_loaded": loaded}


async def stats_for(conn, ticker: str) -> dict | None:
    """Part des ventes à découvert : dernier jour connu et moyenne sur 20 séances."""
    cur = await conn.execute(
        """SELECT day, short_volume / total_volume AS ratio FROM short_volume
           WHERE ticker=%s ORDER BY day DESC LIMIT 20""", (ticker,))
    rows = await cur.fetchall()
    if not rows:
        return None
    avg = sum(float(r["ratio"]) for r in rows) / len(rows)
    return {"day": rows[0]["day"], "ratio_pct": round(float(rows[0]["ratio"]) * 100, 1),
            "avg20_pct": round(avg * 100, 1), "days": len(rows)}
