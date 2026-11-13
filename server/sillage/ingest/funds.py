"""13F-HR : portefeuilles trimestriels d'une liste de grands fonds suivis.

- data.sec.gov/submissions/CIK<cik>.json : liste des dépôts du fonds (13F-HR, trimestre = reportDate) ;
- index.json du dépôt : le fichier XML « information table » (autre que primary_doc.xml) ;
- CUSIP → ticker : API OpenFIGI (gratuite, sans clé : 25 requêtes/min, 10 CUSIP par requête).
Les montants sont en dollars (depuis 2023 ; les dépôts plus anciens étaient en milliers).
"""
from __future__ import annotations

import asyncio
import re
from datetime import date

import httpx
from defusedxml import ElementTree as ET

from .. import log
from .sec import SecClient, _strip_ns

_log = log.get("funds")

# Fonds suivis par défaut (CIK vérifiés le 2026-11-13 : 13F-HR récents).
DEFAULT_FUNDS: dict[int, str] = {
    1067983: "Berkshire Hathaway (Warren Buffett)",
    1336528: "Pershing Square (Bill Ackman)",
    1061768: "Baupost Group (Seth Klarman)",
    1656456: "Appaloosa (David Tepper)",
    1040273: "Third Point (Dan Loeb)",
    921669: "Icahn (Carl Icahn)",
    1536411: "Duquesne Family Office (Stanley Druckenmiller)",
    1709323: "Himalaya Capital (Li Lu)",
    1167483: "Tiger Global",
    1061165: "Lone Pine Capital",
    1103804: "Viking Global",
    1135730: "Coatue Management",
    1112520: "Akre Capital",
    1056831: "Fairholme (Bruce Berkowitz)",
    1418814: "ValueAct",
    949509: "Oaktree Capital (Howard Marks)",
    1350694: "Bridgewater Associates",
    1029160: "Soros Fund Management",
}

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
INDEX_JSON_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/index.json"
FILE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{name}"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
QUARTERS_KEPT = 2  # le trimestre courant et le précédent suffisent pour calculer les mouvements
FIGI_BATCH = 10
FIGI_INTERVAL = 2.6  # 25 requêtes/min sans clé
FIGI_MAX_REQUESTS = 60


def parse_funds_setting(raw: str | None) -> dict[int, str]:
    """FUNDS_13F : vide → liste par défaut ; « off » → aucun ; sinon CIK séparés par des virgules."""
    if raw is None or not raw.strip():
        return dict(DEFAULT_FUNDS)
    if raw.strip().lower() in ("off", "none", "0", "false", "non"):
        return {}
    out: dict[int, str] = {}
    for part in re.split(r"[,;\s]+", raw.strip()):
        if part.isdigit():
            cik = int(part)
            out[cik] = DEFAULT_FUNDS.get(cik, f"CIK {cik}")
    return out


def parse_info_table(xml_text: str | bytes) -> dict[tuple[str, str], dict]:
    """Agrège les lignes par (CUSIP, put/call) : un même titre apparaît une fois par gestionnaire."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    _strip_ns(root)
    out: dict[tuple[str, str], dict] = {}
    for it in root.iter("infoTable"):
        cusip = (it.findtext("cusip") or "").strip().upper()
        if not cusip:
            continue
        put_call = (it.findtext("putCall") or "").strip().upper()
        amt = it.find("shrsOrPrnAmt")
        shares = float((amt.findtext("sshPrnamt") or "0").replace(",", "")) if amt is not None else 0.0
        value = float((it.findtext("value") or "0").replace(",", ""))
        rec = out.setdefault((cusip, put_call), {
            "cusip": cusip, "put_call": put_call, "issuer": (it.findtext("nameOfIssuer") or "").strip(),
            "title": (it.findtext("titleOfClass") or "").strip(), "shares": 0.0, "value": 0.0,
        })
        rec["shares"] += shares
        rec["value"] += value
    return out


def recent_13f(submissions: dict, n: int = QUARTERS_KEPT) -> list[dict]:
    """Derniers 13F-HR (hors amendements), un par trimestre, du plus récent au plus ancien."""
    r = submissions.get("filings", {}).get("recent", {})
    seen: set[str] = set()
    out = []
    for i, form in enumerate(r.get("form", [])):
        if form != "13F-HR":
            continue
        period = r["reportDate"][i]
        if not period or period in seen:
            continue
        seen.add(period)
        out.append({"accession": r["accessionNumber"][i], "period": date.fromisoformat(period),
                    "filed": date.fromisoformat(r["filingDate"][i])})
        if len(out) >= n:
            break
    return out


async def refresh_fund(client: SecClient, conn, cik: int, label: str) -> int:
    resp = await client.get(SUBMISSIONS_URL.format(cik=cik))
    resp.raise_for_status()
    subs = resp.json()
    added = 0
    for f in recent_13f(subs):
        cur = await conn.execute("SELECT 1 FROM fund_filings WHERE accession=%s", (f["accession"],))
        if await cur.fetchone():
            continue
        nodash = f["accession"].replace("-", "")
        idx = await client.get(INDEX_JSON_URL.format(cik=cik, acc_nodash=nodash))
        idx.raise_for_status()
        names = [i["name"] for i in idx.json()["directory"]["item"]]
        xmls = [n for n in names if n.lower().endswith(".xml") and n.lower() != "primary_doc.xml"]
        if not xmls:
            raise ValueError(f"table d'information introuvable dans {f['accession']}")
        doc = await client.get(FILE_URL.format(cik=cik, acc_nodash=nodash, name=xmls[0]))
        doc.raise_for_status()
        holdings = parse_info_table(doc.content)
        total = sum(h["value"] for h in holdings.values())
        await conn.execute(
            "INSERT INTO fund_filings (accession, cik, fund_name, period, filed_date, total_value, positions)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (f["accession"], cik, label, f["period"], f["filed"], total, len(holdings)),
        )
        async with conn.cursor() as c:
            await c.executemany(
                "INSERT INTO fund_holdings (accession, cusip, put_call, issuer, title, shares, value)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                [(f["accession"], h["cusip"], h["put_call"], h["issuer"], h["title"], h["shares"], h["value"])
                 for h in holdings.values()],
            )
        await conn.commit()
        added += 1
    # On ne garde que les QUARTERS_KEPT derniers trimestres par fonds.
    await conn.execute(
        """DELETE FROM fund_filings WHERE cik=%s AND accession NOT IN (
             SELECT accession FROM fund_filings WHERE cik=%s ORDER BY period DESC LIMIT %s)""",
        (cik, cik, QUARTERS_KEPT),
    )
    await conn.commit()
    return added


async def refresh_all(client: SecClient, conn, funds: dict[int, str], stop: asyncio.Event | None = None) -> dict:
    stats = {"funds": 0, "filings": 0, "errors": 0}
    for cik, label in funds.items():
        if stop is not None and stop.is_set():
            break
        try:
            stats["filings"] += await refresh_fund(client, conn, cik, label)
            stats["funds"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — un fonds en erreur n'arrête pas les autres
            await conn.rollback()
            stats["errors"] += 1
            _log.warning("13F de %s (CIK %s) en erreur : %s: %s", label, cik, type(exc).__name__, exc)
    return stats


# ---------------------------------------------------------------- CUSIP → ticker


def pick_ticker(data: list[dict] | None) -> tuple[str | None, str | None]:
    if not data:
        return None, None
    us = [d for d in data if d.get("exchCode") == "US"] or data
    best = next((d for d in us if (d.get("marketSector") == "Equity")), us[0])
    t = best.get("ticker")
    return (t.replace("/", ".").upper() if t else None), best.get("name")


async def map_cusips(conn, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    cur = await conn.execute(
        "SELECT h.cusip FROM fund_holdings h LEFT JOIN cusip_tickers c USING (cusip)"
        " WHERE c.cusip IS NULL GROUP BY h.cusip ORDER BY max(h.value) DESC LIMIT %s",  # grosses lignes d'abord
        (FIGI_BATCH * FIGI_MAX_REQUESTS,))
    cusips = [r["cusip"] for r in await cur.fetchall()]
    await conn.commit()
    mapped = 0
    async with httpx.AsyncClient(timeout=20, transport=transport) as client:
        for i in range(0, len(cusips), FIGI_BATCH):
            chunk = cusips[i:i + FIGI_BATCH]
            resp = await client.post(OPENFIGI_URL, json=[{"idType": "ID_CUSIP", "idValue": c} for c in chunk])
            if resp.status_code == 429:
                _log.warning("OpenFIGI : limite atteinte, suite au prochain cycle")
                break
            resp.raise_for_status()
            for cusip, res in zip(chunk, resp.json()):
                ticker, name = pick_ticker(res.get("data"))
                mapped += 1 if ticker else 0
                await conn.execute(
                    "INSERT INTO cusip_tickers (cusip, ticker, name) VALUES (%s,%s,%s)"
                    " ON CONFLICT (cusip) DO UPDATE SET ticker=EXCLUDED.ticker, name=EXCLUDED.name, updated_at=now()",
                    (cusip, ticker, name),
                )
            await conn.commit()
            await asyncio.sleep(FIGI_INTERVAL)
    return {"cusips": len(cusips), "mapped": mapped}


# ---------------------------------------------------------------- mouvements d'un trimestre à l'autre


def classify(cur_shares: float | None, prev_shares: float | None, threshold: float = 0.05) -> str:
    if not prev_shares:
        return "new"
    if not cur_shares:
        return "sold"
    change = cur_shares / prev_shares - 1
    if change >= threshold:
        return "added"
    if change <= -threshold:
        return "reduced"
    return "held"
