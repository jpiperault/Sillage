"""Dates de publication des résultats (calendrier Nasdaq, API non officielle) : 30 jours à venir et historique."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import httpx

from .. import log
from .prices import BROWSER_UA

_log = log.get("earnings")
URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
TIMING = {"time-pre-market": "avant ouverture", "time-after-hours": "après clôture"}


def parse(data: dict) -> list[tuple[str, str | None]]:
    rows = ((data or {}).get("data") or {}).get("rows") or []
    out = []
    for r in rows:
        sym = (r.get("symbol") or "").strip().upper()
        if sym:
            out.append((sym, TIMING.get(r.get("time"))))
    return out


async def refresh(conn, ahead_days: int = 30, history_days: int = 0, per_run: int = 40,
                  transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Jours à venir rechargés chaque jour ; jours passés chargés une seule fois (per_run par passage)."""
    today = date.today()
    future = [today + timedelta(days=i) for i in range(ahead_days + 1)]
    past = [today - timedelta(days=i) for i in range(1, history_days + 1)]
    cur = await conn.execute("SELECT day, fetched_at::date AS f FROM earnings_days_done")
    done = {r["day"]: r["f"] for r in await cur.fetchall()}
    await conn.commit()
    todo = [d for d in future if done.get(d) != today] + [d for d in past if d not in done]
    todo = [d for d in todo if d.weekday() < 5][:per_run]
    n = 0
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
                                 transport=transport) as client:
        for d in todo:
            resp = await client.get(URL.format(d=d.isoformat()))
            if resp.status_code != 200:
                continue
            rows = parse(resp.json())
            await conn.execute("DELETE FROM earnings WHERE day=%s", (d,))
            async with conn.cursor() as c:
                await c.executemany("INSERT INTO earnings (ticker, day, timing) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                                    [(t, d, tm) for t, tm in rows])
            await conn.execute("INSERT INTO earnings_days_done (day) VALUES (%s) ON CONFLICT (day) DO UPDATE"
                               " SET fetched_at=now()", (d,))
            await conn.commit()
            n += 1
            await asyncio.sleep(0.3)
    return {"days": n, "remaining": max(0, len([d for d in past if d not in done and d.weekday() < 5]) - n)}
