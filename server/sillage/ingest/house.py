"""Transactions des représentants américains : déclarations officielles du Greffe de la Chambre.

Source gratuite : https://disclosures-clerk.house.gov
- index annuel `<année>FD.zip` (XML) : un élément <Member> par dépôt ; FilingType « P » = Periodic
  Transaction Report (PTR) ;
- PTR déposé en ligne (DocID à 8 chiffres) : PDF avec du texte, lu ici ;
- PTR papier (DocID plus court) : PDF scanné, sans texte → ignoré (statut « skipped »).
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

import httpx
from defusedxml import ElementTree as ET

from .. import log
from .congress import parse_range

_log = log.get("house")

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
SOURCE = "house"
BATCH_SIZE = 150
MAX_ATTEMPTS = 3
MIN_INTERVAL = 0.4  # site public : on reste discret (≤ 2,5 req/s)
KEEP_ASSET_TYPES = ("ST",)  # actions cotées ; options, obligations, fonds… ignorés en v1


# ---------------------------------------------------------------- index annuel


@dataclass
class IndexEntry:
    doc_id: str
    year: int
    name: str
    state_dst: str | None
    filing_date: date | None
    electronic: bool


def _us_date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return datetime.strptime(v.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def parse_index_zip(content: bytes) -> list[IndexEntry]:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        xml_name = next(n for n in z.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(z.read(xml_name))
    out: list[IndexEntry] = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        doc_id = (m.findtext("DocID") or "").strip()
        if not doc_id.isdigit():
            continue
        parts = [(m.findtext(k) or "").strip() for k in ("Prefix", "First", "Last", "Suffix")]
        name = " ".join(p for p in parts[1:3] if p)
        if parts[3]:
            name += f" {parts[3]}"
        try:
            year = int((m.findtext("Year") or "").strip())
        except ValueError:
            continue
        out.append(IndexEntry(
            doc_id=doc_id, year=year, name=name or "Inconnu",
            state_dst=(m.findtext("StateDst") or "").strip() or None,
            filing_date=_us_date(m.findtext("FilingDate")),
            electronic=len(doc_id) >= 8,
        ))
    return out


# ---------------------------------------------------------------- lecture d'un PTR


def clean_text(raw: str) -> str:
    """Le PDF encode ses intertitres avec des caractères nuls (« F\\x00\\x00 S\\x00 ») : on les retire,
    puis on met tout sur une ligne pour que les champs coupés en fin de ligne se recollent."""
    text = raw.replace("\x00", "")
    return re.sub(r"\s+", " ", text).strip()


_TX_RE = re.compile(
    r"\((?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\)\s*\[(?P<atype>[A-Z]{2})\]\s*"
    r"(?P<type>P|S\s*\(partial\)|S|E)\s+"
    r"(?P<d1>\d{2}/\d{2}/\d{4})\s*(?P<d2>\d{2}/\d{2}/\d{4})\s*"
    r"(?P<amount>\$[\d,]+\s*-\s*\$[\d,]+|Over\s+\$[\d,]+|\$[\d,]+)"
)
_NAME_RE = re.compile(r"Name:\s*(?:Hon\.\s*)?(?P<name>.+?)\s+Status:")
_HONORIFIC_RE = re.compile(r"\b(?:Hon|Dr|Mr|Mrs|Ms)\b\.?\s*")


def clean_name(name: str | None) -> str | None:
    """« Hon. Richard Dean Dr McCormick » → « Richard Dean McCormick » (titres retirés où qu'ils soient)."""
    if not name:
        return None
    name = re.sub(r"\s+", " ", _HONORIFIC_RE.sub(" ", name)).strip(" ,")
    return re.sub(r"\b(\w+) \1\b", r"\1", name) or None  # « Scott Scott Franklin »
_STATE_RE = re.compile(r"State/District:\s*(?P<sd>[A-Z]{2}\d{0,2})")


def parse_ptr_text(raw: str) -> dict:
    """Renvoie {name, state_dst, transactions:[…]} ; ne garde que les actions cotées achetées ou vendues."""
    text = clean_text(raw)
    nm = _NAME_RE.search(text)
    sd = _STATE_RE.search(text)
    rows = []
    for i, m in enumerate(_TX_RE.finditer(text), start=1):
        if m.group("atype") not in KEEP_ASSET_TYPES:
            continue
        kind = m.group("type").replace(" ", "")
        if kind == "E":  # échange : ni achat ni vente
            continue
        amin, amax = parse_range(m.group("amount"))
        rows.append({
            "line": i,
            "ticker": m.group("ticker"),
            "side": "buy" if kind == "P" else "sell",
            "partial": kind == "S(partial)",
            "trade_date": _us_date(m.group("d1")),
            "notification_date": _us_date(m.group("d2")),
            "amount_range": re.sub(r"\s+", " ", m.group("amount")),
            "amount_min": amin,
            "amount_max": amax,
        })
    return {
        "name": clean_name(nm.group("name")) if nm else None,
        "state_dst": sd.group("sd") if sd else None,
        "transactions": rows,
    }


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader  # import local : coûteux, seulement pendant la collecte

    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# ---------------------------------------------------------------- collecte


class HouseClient:
    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent}, timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True, transport=transport,
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


async def poll_index(client: HouseClient, conn, years: list[int]) -> int:
    new = 0
    for year in years:
        resp = await client.get(INDEX_URL.format(year=year))
        if resp.status_code == 404:
            continue
        resp.raise_for_status()
        for e in parse_index_zip(resp.content):
            cur = await conn.execute(
                """INSERT INTO house_filings (doc_id, year, name, state_dst, filing_date, status)
                   VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (doc_id) DO NOTHING""",
                (e.doc_id, e.year, e.name, e.state_dst, e.filing_date, "pending" if e.electronic else "skipped"),
            )
            new += cur.rowcount
        await conn.commit()
    return new


async def store_ptr(conn, filing: dict, parsed: dict, url: str) -> int:
    # Le nom du PDF (« Name: Hon. … ») est plus propre que celui de l'index (« Richard Dean Dr McCormick »).
    name = parsed["name"] or clean_name(filing["name"]) or "Inconnu"
    n = 0
    for t in parsed["transactions"]:
        cur = await conn.execute(
            """INSERT INTO congress_trades (source, source_key, politician, party, chamber, ticker, side,
                 amount_range, amount_min, amount_max, trade_date, report_date, raw, url)
               VALUES (%s,%s,%s,NULL,'House',%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (source, source_key) DO NOTHING""",
            (SOURCE, f"{filing['doc_id']}-{t['line']}", name, t["ticker"], t["side"], t["amount_range"],
             t["amount_min"], t["amount_max"], t["trade_date"], filing["filing_date"] or t["notification_date"],
             json.dumps({**t, "state_dst": filing["state_dst"] or parsed["state_dst"]}, default=str), url),
        )
        n += cur.rowcount
    return n


async def process_queue(client: HouseClient, conn, limit: int = BATCH_SIZE,
                        stop: asyncio.Event | None = None) -> dict:
    cur = await conn.execute(
        "SELECT doc_id, year, name, state_dst, filing_date FROM house_filings WHERE status='pending'"
        " ORDER BY filing_date DESC NULLS LAST, doc_id DESC LIMIT %s",
        (limit,),
    )
    rows = await cur.fetchall()
    await conn.commit()
    stats = {"filings": 0, "trades": 0, "errors": 0, "no_text": 0}
    for f in rows:
        if stop is not None and stop.is_set():
            break
        url = PTR_URL.format(year=f["year"], doc_id=f["doc_id"])
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            text = await asyncio.to_thread(pdf_text, resp.content)
            if not clean_text(text):
                status, n = "skipped", 0
                stats["no_text"] += 1
            else:
                parsed = parse_ptr_text(text)
                n = await store_ptr(conn, f, parsed, url)
                status = "done"
            await conn.execute(
                "UPDATE house_filings SET status=%s, attempts=attempts+1, trades=%s, last_error=NULL,"
                " updated_at=now() WHERE doc_id=%s",
                (status, n, f["doc_id"]),
            )
            await conn.commit()
            stats["filings"] += 1
            stats["trades"] += n
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — un PTR illisible n'arrête pas la file
            await conn.rollback()
            stats["errors"] += 1
            msg = f"{type(exc).__name__}: {exc}"[:500]
            await conn.execute(
                "UPDATE house_filings SET attempts=attempts+1, last_error=%s, updated_at=now(),"
                " status = CASE WHEN attempts+1 >= %s THEN 'error' ELSE 'pending' END WHERE doc_id=%s",
                (msg, MAX_ATTEMPTS, f["doc_id"]),
            )
            await conn.commit()
            _log.warning("PTR %s en erreur : %s", f["doc_id"], msg)
    return stats
