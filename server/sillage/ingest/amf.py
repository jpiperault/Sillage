"""Déclarations des dirigeants de sociétés françaises (AMF, base BDIF, art. 19 MAR).

API publique (non documentée) de https://bdif.amf-france.org :
- liste : /back/api/v1/informations?typesInformation=DD&size=…&from=… (plus récentes d'abord) ;
- document : /back/api/v1/documents/<path> (PDF généré, texte lisible, en français puis en anglais).
On ne garde que les acquisitions et cessions d'actions ; donations, attributions gratuites, nantissements… ignorés.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime

import httpx

from .. import log
from .house import pdf_text

_log = log.get("amf")

LIST_URL = "https://bdif.amf-france.org/back/api/v1/informations?typesInformation=DD&size={size}&from={start}"
DOC_URL = "https://bdif.amf-france.org/back/api/v1/documents/{path}"
PAGE_SIZE = 100
MAX_PAGES = 3
BATCH_SIZE = 150
MAX_ATTEMPTS = 3
MIN_INTERVAL = 0.4
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

_MONTHS = {m: i for i, m in enumerate(
    ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
     "novembre", "décembre"], start=1)}


def fr_date(text: str | None) -> date | None:
    if not text:
        return None
    m = re.search(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", text.lower())
    if not m or m.group(2) not in _MONTHS:
        return None
    try:
        return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None


def fr_number(text: str | None) -> float | None:
    if not text:
        return None
    t = text.replace(" ", "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _field(block: str, label: str) -> str | None:
    m = re.search(label + r"\s*:\s*(.+)", block)
    return m.group(1).strip() if m else None


def parse_amf_text(raw: str) -> dict:
    """Lit la partie française d'une déclaration ; renvoie {person, role, issuer, transactions:[…]}."""
    # La déclaration est répétée en anglais : on coupe avant.
    text = re.split(r"\n\s*Individual notification relating", raw, maxsplit=1)[0]
    text = re.split(r"\n\d{4}DD\d+\s*\n[A-Z]{2}[A-Z0-9]{9}\d\s*-\s*DD\d+\s*\n\d{1,2} [A-Z][a-z]+ \d{4}", text)[0]

    who = re.search(r"PERSONNE ETROITEMENT LIEE\s*:\s*\n(.+?)\nNOTIFICATION", text, re.S)
    who_txt = re.sub(r"\s+", " ", who.group(1)).strip() if who else ""
    person, _, role = who_txt.partition(",")
    # « HOLDING SAS personne morale liée à Jean Dupont » → « Jean Dupont (via HOLDING SAS) »
    pm = re.match(r"(.+?)\s+personne morale li[ée]e à\s+(.+)", person, re.I)
    if pm:
        person = f"{pm.group(2).strip()} (via {pm.group(1).strip()})"
    issuer = _field(text, r"COORDONNEES DE L’EMETTEUR\s*\nNOM")
    if issuer is None:
        m = re.search(r"COORDONNEES DE L.EMETTEUR\s*\nNOM\s*:\s*(.+)", text)
        issuer = m.group(1).strip() if m else None

    out = []
    head_isin = re.search(r"^\s*\d{4}DD\d+\s*\n([A-Z]{2}[A-Z0-9]{9}\d)\b", raw)
    blocks = re.split(r"\nDETAIL DE LA TRANSACTION(?:\s+\d+)?\s*\n", text)[1:]
    for i, b in enumerate(blocks, start=1):
        nature = (_field(b, "NATURE DE LA TRANSACTION") or "").strip()
        instrument = (_field(b, "DESCRIPTION DE L’INSTRUMENT FINANCIER") or
                      _field(b, "DESCRIPTION DE L.INSTRUMENT FINANCIER") or "")
        isin = _field(b, r"CODE D.IDENTIFICATION DE L.INSTRUMENT FINANCIER")
        agg = b.split("INFORMATIONS AGREGEES", 1)[-1]
        price_m = re.search(r"PRIX\s*:\s*([\d\s.,]+)\s*([A-Za-z]+)", agg)
        vol_m = re.search(r"VOLUME\s*:\s*([\d\s.,]+)", agg)
        n = nature.lower()
        side = "buy" if n.startswith("acquisition") else "sell" if n.startswith("cession") else None
        price = fr_number(price_m.group(1)) if price_m else None
        volume = fr_number(vol_m.group(1)) if vol_m else None
        out.append({
            "line": i,
            "nature": nature,
            "side": side,
            "instrument": instrument.strip(),
            "isin": isin.split()[0] if isin else (head_isin.group(1) if head_isin else None),
            "trade_date": fr_date(_field(b, "DATE DE LA TRANSACTION")),
            "venue": _field(b, "LIEU DE LA TRANSACTION"),
            "price": price,
            "currency": price_m.group(2) if price_m else None,
            "volume": volume,
            "value": round(price * volume, 2) if price and volume else None,
            "options": bool(re.search(r"ACTIONS GRATUITES OU DE PERFORMANCES\s*:\s*OUI", b)),
        })
    return {
        "person": person.strip() or None,
        "role": role.strip() or None,
        "issuer": issuer,
        "received": fr_date(_field(text, "DATE DE RECEPTION DE LA NOTIFICATION")),
        "transactions": out,
    }


def keep(tx: dict) -> bool:
    nature = tx["nature"].lower()
    return (tx["side"] is not None and tx["instrument"].lower().startswith("action")
            and not tx["options"] and (tx["price"] or 0) > 0
            and not any(w in nature for w in ("gratuit", "performance", "donation")))


# ---------------------------------------------------------------- collecte


class AmfClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json, application/pdf"},
            timeout=httpx.Timeout(30.0, connect=10.0), transport=transport,
        )
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, url: str) -> httpx.Response:
        async with self._lock:
            wait = self._last + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        return await self._client.get(url)


def _parse_dt(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v[:19]).date()
    except ValueError:
        return None


async def poll_list(client: AmfClient, conn) -> int:
    new = 0
    for page in range(MAX_PAGES):
        resp = await client.get(LIST_URL.format(size=PAGE_SIZE, start=page * PAGE_SIZE))
        resp.raise_for_status()
        items = resp.json().get("result") or []
        page_new = 0
        for it in items:
            docs = [d for d in (it.get("documents") or []) if d.get("accessible") and d.get("path")]
            if not docs or not it.get("numero"):
                continue
            company = next((s.get("raisonSociale") for s in it.get("societes") or []
                            if s.get("role") == "SocieteConcernee"), None)
            cur = await conn.execute(
                """INSERT INTO amf_filings (numero, path, company, published)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (numero) DO NOTHING""",
                (it["numero"], docs[0]["path"], company, _parse_dt(it.get("datePublication"))),
            )
            page_new += cur.rowcount
        await conn.commit()
        new += page_new
        if page_new == 0 or len(items) < PAGE_SIZE:
            break
    return new


HISTORY_LIMIT = 10_000  # l'API BDIF ne pagine pas au-delà


async def backfill_history(client: AmfClient, conn, pages: int = 5) -> dict:
    """Remonte l'historique des déclarations, quelques pages par passage, jusqu'à la limite de l'API."""
    await conn.execute("INSERT INTO amf_history_state (id) VALUES (1) ON CONFLICT DO NOTHING")
    cur = await conn.execute("SELECT next_offset, done FROM amf_history_state WHERE id=1")
    st = await cur.fetchone()
    await conn.commit()
    if st["done"]:
        return {"done": True}
    offset, new = st["next_offset"], 0
    for _ in range(pages):
        if offset >= HISTORY_LIMIT:
            break
        resp = await client.get(LIST_URL.format(size=PAGE_SIZE, start=offset))
        resp.raise_for_status()
        items = resp.json().get("result") or []
        for it in items:
            docs = [d for d in (it.get("documents") or []) if d.get("accessible") and d.get("path")]
            if not docs or not it.get("numero"):
                continue
            company = next((s.get("raisonSociale") for s in it.get("societes") or []
                            if s.get("role") == "SocieteConcernee"), None)
            c = await conn.execute(
                "INSERT INTO amf_filings (numero, path, company, published) VALUES (%s,%s,%s,%s)"
                " ON CONFLICT (numero) DO NOTHING",
                (it["numero"], docs[0]["path"], company, _parse_dt(it.get("datePublication"))))
            new += c.rowcount
        offset += PAGE_SIZE
        done = len(items) < PAGE_SIZE or offset >= HISTORY_LIMIT
        await conn.execute("UPDATE amf_history_state SET next_offset=%s, done=%s WHERE id=1", (offset, done))
        await conn.commit()
        if done:
            break
    return {"offset": offset, "new": new}


async def process_queue(client: AmfClient, conn, limit: int = BATCH_SIZE,
                        stop: asyncio.Event | None = None, recent: bool | None = None) -> dict:
    """recent=True : déclarations de moins de 7 jours (boucle rapide) ; False : plus anciennes (boucle lente)."""
    cond = "" if recent is None else (" AND published >= current_date - 7" if recent
                                      else " AND (published < current_date - 7 OR published IS NULL)")
    cur = await conn.execute(
        "SELECT numero, path, company, published FROM amf_filings WHERE status='pending'" + cond +
        " ORDER BY published DESC NULLS LAST LIMIT %s", (limit,))
    rows = await cur.fetchall()
    await conn.commit()
    stats = {"filings": 0, "trades": 0, "errors": 0}
    for f in rows:
        if stop is not None and stop.is_set():
            break
        url = DOC_URL.format(path=f["path"])
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            parsed = parse_amf_text(await asyncio.to_thread(pdf_text, resp.content))
            n = 0
            for t in parsed["transactions"]:
                if not keep(t):
                    continue
                c = await conn.execute(
                    """INSERT INTO amf_trades (numero, line_no, company, person, role, side, isin, price, currency,
                         volume, value, trade_date, published, venue, nature, url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (numero, line_no) DO NOTHING""",
                    (f["numero"], t["line"], parsed["issuer"] or f["company"], parsed["person"] or "Inconnu",
                     parsed["role"], t["side"], t["isin"], t["price"], t["currency"], t["volume"], t["value"],
                     t["trade_date"], f["published"], t["venue"], t["nature"], url),
                )
                n += c.rowcount
            await conn.execute(
                "UPDATE amf_filings SET status='done', attempts=attempts+1, trades=%s, last_error=NULL,"
                " updated_at=now() WHERE numero=%s", (n, f["numero"]))
            await conn.commit()
            stats["filings"] += 1
            stats["trades"] += n
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await conn.rollback()
            stats["errors"] += 1
            msg = f"{type(exc).__name__}: {exc}"[:500]
            await conn.execute(
                "UPDATE amf_filings SET attempts=attempts+1, last_error=%s, updated_at=now(),"
                " status = CASE WHEN attempts+1 >= %s THEN 'error' ELSE 'pending' END WHERE numero=%s",
                (msg, MAX_ATTEMPTS, f["numero"]))
            await conn.commit()
            _log.warning("déclaration AMF %s en erreur : %s", f["numero"], msg)
    return stats


__all__ = ["parse_amf_text", "keep", "poll_list", "process_queue", "AmfClient", "json"]
