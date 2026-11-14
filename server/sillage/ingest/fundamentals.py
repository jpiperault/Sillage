"""Actions en circulation de toutes les sociétés (SEC XBRL « frames », une requête par trimestre).

https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/CY<AAAA>Q<n>I.json
Capitalisation = actions × cours (cours Sillage ou prix au trade).
"""
from __future__ import annotations

from datetime import date

from .. import log

_log = log.get("fundamentals")
FRAME_URL = "https://data.sec.gov/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/shares/CY{y}Q{q}I.json"


def recent_frames(today: date, n: int = 3) -> list[str]:
    """Trimestres terminés, du plus récent au plus ancien : ['CY2026Q3I', …]."""
    y, q = today.year, (today.month - 1) // 3  # trimestre précédent le trimestre en cours
    out = []
    for _ in range(n):
        if q == 0:
            y, q = y - 1, 4
        out.append((y, q))
        q -= 1
    return [f"CY{y}Q{q}I" for y, q in out]


def parse_frame(data: dict) -> list[tuple[int, float, date | None]]:
    rows = []
    for d in data.get("data", []):
        try:
            rows.append((int(d["cik"]), float(d["val"]), date.fromisoformat(d["end"]) if d.get("end") else None))
        except (KeyError, TypeError, ValueError):
            continue
    return rows


async def refresh(client, conn, today: date | None = None) -> dict:
    """Charge les 3 derniers trimestres, du plus ancien au plus récent (le plus récent l'emporte)."""
    loaded = {}
    for frame in reversed(recent_frames(today or date.today())):
        y, q = int(frame[2:6]), int(frame[7])
        resp = await client.get(FRAME_URL.format(y=y, q=q))
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        rows = [r for r in parse_frame(resp.json()) if r[1] > 0]
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO company_shares (cik, shares, as_of, updated_at) VALUES (%s,%s,%s,now())"
                " ON CONFLICT (cik) DO UPDATE SET shares=EXCLUDED.shares, as_of=EXCLUDED.as_of, updated_at=now()"
                " WHERE company_shares.as_of IS NULL OR EXCLUDED.as_of >= company_shares.as_of", rows)
        await conn.commit()
        loaded[frame] = len(rows)
    return {"frames": loaded}


def cap_bucket(market_cap: float | None) -> str:
    if not market_cap:
        return "inconnue"
    if market_cap < 300e6:
        return "< 300 M$"
    if market_cap < 2e9:
        return "300 M$-2 Md$"
    if market_cap < 10e9:
        return "2-10 Md$"
    return "≥ 10 Md$"
