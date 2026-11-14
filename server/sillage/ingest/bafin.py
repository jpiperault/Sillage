"""Allemagne : transactions des dirigeants (BaFin, base « Directors' Dealings », export CSV public)."""
from __future__ import annotations

import csv
import hashlib
import io
from datetime import date, datetime

import httpx

from .. import log
from .prices import BROWSER_UA

_log = log.get("bafin")
URL = ("https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do?meldepflichtigerName=&zeitraum={period}"
       "&d-4000784-e=1&emittentButton=Suche+Emittent&6578706f7274=1&zeitraumVon=&emittentName=&zeitraumBis="
       "&option=dealings")
SIDE = {"kauf": "buy", "verkauf": "sell"}


def de_number(text: str | None) -> tuple[float | None, str | None]:
    """« 20.945,00 EUR » → (20945.0, 'EUR')."""
    if not text:
        return None, None
    parts = text.strip().split()
    cur = parts[-1] if len(parts) > 1 and parts[-1].isalpha() else None
    num = parts[0].replace(".", "").replace(",", ".")
    try:
        return float(num), cur
    except ValueError:
        return None, cur


def de_date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip()[:10], "%d.%m.%Y").date()
    except ValueError:
        return None


def parse_csv(text: str) -> list[dict]:
    out = []
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    for r in reader:
        side = SIDE.get((r.get("Art des Geschäfts") or "").strip().lower())
        if side is None or (r.get("Art des Instruments") or "").strip().lower() not in ("aktie", "share", "shares"):
            continue
        price, currency = de_number(r.get("Durchschnittspreis"))
        value, vcur = de_number(r.get("Aggregiertes Volumen"))
        if not price or not value:
            continue
        key = "|".join((r.get(k) or "") for k in ("Bafin-ID", "ISIN", "Meldepflichtiger", "Art des Geschäfts",
                                                    "Datum des Geschäfts", "Aggregiertes Volumen", "Durchschnittspreis"))
        out.append({
            "source_key": hashlib.sha1(key.encode()).hexdigest(),
            "company": (r.get("Emittent") or "").strip() or None,
            "isin": (r.get("ISIN") or "").strip() or None,
            "person": (r.get("Meldepflichtiger") or "").strip() or "Inconnu",
            "role": (r.get("Position / Status") or "").strip() or None,
            "side": side, "price": price, "currency": currency or vcur, "value": value,
            "volume": round(value / price, 2) if price else None,
            "trade_date": de_date(r.get("Datum des Geschäfts")),
            "published": de_date(r.get("Mitteilungsdatum")),
            "venue": (r.get("Ort des Geschäfts") or "").strip() or None,
        })
    return out


async def refresh(conn, period: int = 2, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """period : 1 = jour, 2 = semaine, 3 = mois (paramètre « zeitraum » de la BaFin)."""
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": BROWSER_UA}, transport=transport) as client:
        resp = await client.get(URL.format(period=period))
        resp.raise_for_status()
        rows = parse_csv(resp.content.decode("utf-8", errors="replace"))
    new = 0
    for r in rows:
        cur = await conn.execute(
            """INSERT INTO bafin_trades (source_key, company, isin, person, role, side, price, currency, value, volume,
                 trade_date, published, venue) VALUES (%(source_key)s,%(company)s,%(isin)s,%(person)s,%(role)s,%(side)s,
                 %(price)s,%(currency)s,%(value)s,%(volume)s,%(trade_date)s,%(published)s,%(venue)s)
               ON CONFLICT (source_key) DO NOTHING""", r)
        new += cur.rowcount
    await conn.commit()
    return {"rows": len(rows), "new": new}
