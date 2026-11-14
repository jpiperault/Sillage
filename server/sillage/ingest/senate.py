"""Sénat américain : déclarations de transactions (eFD, https://efdsearch.senate.gov).

ATTENTION : non testé en conditions réelles, le site renvoie 403 au serveur de développement. Désactivé par défaut ;
l'administrateur peut tester l'accès depuis Compte → Réglages avant de l'activer.

Déroulé (site Django) :
1. GET /search/home/ → cookie csrftoken + champ csrfmiddlewaretoken ;
2. POST /search/home/ (prohibition_agreement=1) : acceptation des conditions d'usage ;
3. POST /search/report/data/ (report_types=[11] = Periodic Transaction Report) → JSON {data: [[prénom, nom,
   nom complet, lien HTML, date], …]} ;
4. GET /search/view/ptr/<id>/ → tableau HTML des transactions (les dépôts papier /paper/ sont ignorés).
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
from datetime import date, datetime, timedelta

import httpx

from .. import log
from .congress import parse_range
from .prices import BROWSER_UA

_log = log.get("senate")
BASE = "https://efdsearch.senate.gov"
SOURCE = "senate"


class SenateBlocked(Exception):
    pass


def _csrf(text: str) -> str | None:
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', text)
    return m.group(1) if m else None


def parse_report_list(data: dict) -> list[dict]:
    out = []
    for row in data.get("data", []):
        if len(row) < 5:
            continue
        m = re.search(r'href="(/search/view/(ptr|paper)/([0-9a-f-]+)/)"', row[3])
        if not m:
            continue
        try:
            filed = datetime.strptime(row[4].strip(), "%m/%d/%Y").date()
        except ValueError:
            filed = None
        name = re.sub(r"\s+", " ", f"{row[0]} {row[1]}").strip().title()
        out.append({"report_id": m.group(3), "url": BASE + m.group(1), "paper": m.group(2) == "paper",
                    "senator": name, "filed": filed})
    return out


def _cells(row_html: str) -> list[str]:
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S | re.I)
    return [re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in cells]


def parse_ptr(page: str) -> list[dict]:
    """Colonnes : #, date, propriétaire, ticker, actif, type d'actif, type, montant, commentaire."""
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S | re.I)
    if not body:
        return []
    out = []
    for i, row in enumerate(re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S | re.I), start=1):
        c = _cells(row)
        if len(c) < 8:
            continue
        ticker = c[3].strip().upper()
        kind = c[6].lower()
        side = "buy" if "purchase" in kind else "sell" if "sale" in kind else None
        if side is None or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker) or "stock" not in c[5].lower():
            continue
        try:
            td = datetime.strptime(c[1], "%m/%d/%Y").date()
        except ValueError:
            td = None
        amin, amax = parse_range(c[7])
        out.append({"line": i, "trade_date": td, "owner": c[2], "ticker": ticker, "asset": c[4], "side": side,
                    "amount_range": c[7], "amount_min": amin, "amount_max": amax})
    return out


class SenateClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(base_url=BASE, headers={"User-Agent": BROWSER_UA}, timeout=30,
                                         follow_redirects=True, transport=transport)
        self._ready = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _agree(self) -> None:
        r = await self._client.get("/search/home/")
        if r.status_code == 403:
            raise SenateBlocked("le site du Sénat refuse l'accès depuis ce serveur (403)")
        r.raise_for_status()
        token = _csrf(r.text)
        if not token:
            raise ValueError("jeton CSRF introuvable sur la page d'accueil du Sénat")
        r = await self._client.post("/search/home/", data={"prohibition_agreement": "1",
                                                           "csrfmiddlewaretoken": token},
                                    headers={"Referer": BASE + "/search/home/"})
        r.raise_for_status()
        self._ready = True

    async def reports(self, start: date, end: date) -> list[dict]:
        if not self._ready:
            await self._agree()
        token = self._client.cookies.get("csrftoken") or ""
        out, offset = [], 0
        while True:
            r = await self._client.post("/search/report/data/", data={
                "start": str(offset), "length": "100", "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
                "submitted_end_date": end.strftime("%m/%d/%Y 23:59:59"), "candidate_state": "", "senator_state": "",
                "office_id": "", "first_name": "", "last_name": "", "csrfmiddlewaretoken": token},
                headers={"X-CSRFToken": token, "Referer": BASE + "/search/"})
            if r.status_code == 403:
                raise SenateBlocked("accès refusé (403)")
            r.raise_for_status()
            page = parse_report_list(r.json())
            out += page
            if len(page) < 100:
                return out
            offset += 100

    async def ptr(self, url: str) -> str:
        if not self._ready:
            await self._agree()
        r = await self._client.get(url.replace(BASE, ""))
        r.raise_for_status()
        return r.text


async def probe(transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Test d'accès (bouton admin) : accepte les conditions et liste les déclarations des 7 derniers jours."""
    client = SenateClient(transport)
    try:
        rows = await client.reports(date.today() - timedelta(days=7), date.today())
        return {"ok": True, "message": f"Accès au Sénat possible : {len(rows)} déclaration(s) sur 7 jours."}
    except SenateBlocked as exc:
        return {"ok": False, "message": str(exc)}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        await client.close()


async def refresh(conn, days: int = 30, limit: int = 40) -> dict:
    client = SenateClient()
    try:
        reports = await client.reports(date.today() - timedelta(days=days), date.today())
        for r in reports:
            await conn.execute(
                "INSERT INTO senate_filings (report_id, senator, filed, url, status) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT DO NOTHING", (r["report_id"], r["senator"], r["filed"], r["url"],
                                           "skipped" if r["paper"] else "pending"))
        await conn.commit()
        cur = await conn.execute("SELECT * FROM senate_filings WHERE status='pending' ORDER BY filed DESC LIMIT %s",
                                 (limit,))
        todo = await cur.fetchall()
        await conn.commit()
        trades = 0
        for f in todo:
            try:
                rows = parse_ptr(await client.ptr(f["url"]))
                for t in rows:
                    key = hashlib.sha1(f"{f['report_id']}-{t['line']}".encode()).hexdigest()
                    c = await conn.execute(
                        """INSERT INTO congress_trades (source, source_key, politician, party, chamber, ticker, side,
                             amount_range, amount_min, amount_max, trade_date, report_date, raw, url)
                           VALUES (%s,%s,%s,NULL,'Senate',%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (SOURCE, key, f["senator"], t["ticker"], t["side"], t["amount_range"], t["amount_min"],
                         t["amount_max"], t["trade_date"], f["filed"], json.dumps(t, default=str), f["url"]))
                    trades += c.rowcount
                await conn.execute("UPDATE senate_filings SET status='done', attempts=attempts+1 WHERE report_id=%s",
                                   (f["report_id"],))
            except (httpx.HTTPError, ValueError) as exc:
                await conn.rollback()
                await conn.execute(
                    "UPDATE senate_filings SET attempts=attempts+1, last_error=%s,"
                    " status = CASE WHEN attempts+1 >= 3 THEN 'error' ELSE 'pending' END WHERE report_id=%s",
                    (str(exc)[:300], f["report_id"]))
            await conn.commit()
        return {"reports": len(reports), "processed": len(todo), "trades": trades}
    finally:
        await client.close()
