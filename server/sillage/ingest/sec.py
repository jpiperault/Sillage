"""Collecte SEC EDGAR : Form 4 (flux récent + index quotidien), 13D/13G, table CIK → ticker."""
from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from defusedxml import ElementTree as ET

from .. import log

_log = log.get("sec")

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CURRENT_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={type}&company=&dateb="
    "&owner=include&start={start}&count=100&output=atom"
)
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}.txt"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

MAX_PAGES = 5
BATCH_SIZE = 400
MAX_ATTEMPTS = 3
FETCH_CONCURRENCY = 6
KEEP_CODES = ("P", "S", "M")


# ---------------------------------------------------------------- client HTTP


class SecClient:
    """Client EDGAR poli : User-Agent avec contact, ≤ 5 requêtes/s, nouvel essai sur 429/503."""

    def __init__(self, user_agent: str, min_interval: float = 0.2, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    async def get(self, url: str) -> httpx.Response:
        delay = 1.0
        for attempt in range(4):
            async with self._lock:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                resp = await self._client.get(url)
            except httpx.TimeoutException:
                if attempt >= 2:
                    raise
                _log.warning("SEC lente, nouvel essai : %s", url)
                continue
            if resp.status_code in (429, 503) and attempt < 3:
                _log.warning("SEC %d, nouvel essai dans %.0f s : %s", resp.status_code, delay, url)
                await asyncio.sleep(delay)
                delay *= 3
                continue
            return resp
        return resp  # pragma: no cover


# ---------------------------------------------------------------- flux Atom

_TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.+) \((?P<cik>\d{1,10})\) \((?P<role>[^)]+)\)\s*$")
_FILED_RE = re.compile(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})")


@dataclass
class AtomEntry:
    accession: str
    form: str
    name: str
    cik: int
    role: str
    filed: date | None
    link: str


def parse_atom(text: str | bytes) -> list[AtomEntry]:
    root = ET.fromstring(text)
    out: list[AtomEntry] = []
    for entry in root.iter(f"{ATOM_NS}entry"):
        cat = entry.find(f"{ATOM_NS}category")
        term = (cat.get("term") if cat is not None else "") or ""
        id_text = (entry.findtext(f"{ATOM_NS}id") or "").strip()
        if "accession-number=" not in id_text:
            continue
        accession = id_text.split("accession-number=", 1)[1].strip()
        title = (entry.findtext(f"{ATOM_NS}title") or "").strip()
        m = _TITLE_RE.match(title)
        if not m:
            continue
        summary = html.unescape(entry.findtext(f"{ATOM_NS}summary") or "")
        fm = _FILED_RE.search(summary)
        filed = date.fromisoformat(fm.group(1)) if fm else None
        link_el = entry.find(f"{ATOM_NS}link")
        link = link_el.get("href", "") if link_el is not None else ""
        out.append(
            AtomEntry(
                accession=accession,
                form=term.strip(),
                name=m.group("name").strip(),
                cik=int(m.group("cik")),
                role=m.group("role").strip(),
                filed=filed,
                link=link,
            )
        )
    return out


def txt_url_from_index(link: str) -> str:
    """…/edgar/data/<cik>/<acc sans tirets>/<acc>-index.htm → …/<acc>.txt"""
    if link.endswith("-index.htm"):
        return link[: -len("-index.htm")] + ".txt"
    if link.endswith("-index.html"):
        return link[: -len("-index.html")] + ".txt"
    return link


def archive_url(cik: int | str, accession: str) -> str:
    return ARCHIVE_URL.format(cik=int(cik), acc_nodash=accession.replace("-", ""), acc=accession)


def index_url(cik: int | str, accession: str) -> str:
    return FILING_INDEX_URL.format(cik=int(cik), acc_nodash=accession.replace("-", ""), acc=accession)


async def _enqueue(conn, accession: str, url: str, filed: date | None, form: str = "4") -> bool:
    cur = await conn.execute(
        "INSERT INTO sec_filings(accession, form_type, url, filed_date) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (accession) DO NOTHING",
        (accession, form, url, filed),
    )
    return cur.rowcount == 1


async def poll_form4_feed(client: SecClient, conn) -> int:
    return await poll_feed(client, conn, "4")


async def poll_feed(client: SecClient, conn, form: str) -> int:
    """Lit le flux récent d'un formulaire (4 ou 144) et met les nouveaux dépôts en file."""
    total = 0
    for page in range(MAX_PAGES):
        resp = await client.get(CURRENT_URL.format(type=form, start=page * 100))
        resp.raise_for_status()
        entries = parse_atom(resp.content)
        seen: set[str] = set()
        new = 0
        for e in entries:
            # type=4 renvoie aussi d'autres formulaires ; les corrections « 4/A » sont gardées.
            accepted = (form, form + "/A") if form == "4" else (form,)
            if e.form not in accepted or e.accession in seen:
                continue
            seen.add(e.accession)
            if await _enqueue(conn, e.accession, txt_url_from_index(e.link), e.filed, e.form):
                new += 1
        await conn.commit()
        total += new
        if new == 0 or len(entries) < 100:
            break
    return total


# ---------------------------------------------------------------- index quotidien


@dataclass
class IndexRow:
    form: str
    company: str
    cik: int
    filed: date
    path: str

    @property
    def accession(self) -> str:
        return self.path.rsplit("/", 1)[-1].removesuffix(".txt")


def parse_daily_index(text: str) -> list[IndexRow]:
    rows: list[IndexRow] = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("-----"):
                started = True
            continue
        parts = line.split()
        if len(parts) < 4 or not parts[-1].startswith("edgar/"):
            continue
        try:
            filed = datetime.strptime(parts[-2], "%Y%m%d").date()
            cik = int(parts[-3])
        except ValueError:
            continue
        form = line[:12].strip() if len(line) > 12 else parts[0]
        company = " ".join(parts[1:-3]) if form == parts[0] else ""
        rows.append(IndexRow(form=form, company=company, cik=cik, filed=filed, path=parts[-1]))
    return rows


def business_days_back(today: date, n: int) -> list[date]:
    days: list[date] = []
    d = today
    while len(days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    return days


async def backfill(client: SecClient, conn, n_days: int, done_days: set[str], today: date | None = None) -> tuple[int, list[str]]:
    """Met en file les Form 4 des `n_days` derniers jours ouvrés pas encore traités."""
    added = 0
    newly_done: list[str] = []
    for d in business_days_back(today or date.today(), n_days):
        key = d.isoformat()
        if key in done_days:
            continue
        q = (d.month - 1) // 3 + 1
        url = DAILY_INDEX_URL.format(y=d.year, q=q, ymd=d.strftime("%Y%m%d"))
        resp = await client.get(url)
        if resp.status_code in (403, 404):  # jour férié, ou index pas encore publié
            if d < (today or date.today()) - timedelta(days=1):
                newly_done.append(key)
            continue
        resp.raise_for_status()
        for row in parse_daily_index(resp.text):
            if row.form not in ("4", "4/A"):
                continue
            url_txt = "https://www.sec.gov/Archives/" + row.path
            if await _enqueue(conn, row.accession, url_txt, row.filed, row.form):
                added += 1
        await conn.commit()
        newly_done.append(key)
    return added, newly_done


# ---------------------------------------------------------------- Form 4


def extract_xml_block(txt: str, marker: str) -> str | None:
    for m in re.finditer(r"<XML>(.*?)</XML>", txt, re.DOTALL | re.IGNORECASE):
        block = m.group(1).strip()
        if marker in block:
            return block
    return None


def extract_ownership_xml(txt: str) -> str | None:
    return extract_xml_block(txt, "<ownershipDocument")


_FILED_AS_OF = re.compile(r"FILED AS OF DATE:\s*(\d{8})")


def filed_date_from_txt(txt: str) -> date | None:
    m = _FILED_AS_OF.search(txt[:5000])
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def _txt(el, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None:
        return None
    value = found.findtext("value") if found.find("value") is not None else found.text
    value = (value or "").strip()
    return value or None


def _flag(el, path: str) -> bool:
    v = (_txt(el, path) or "").lower()
    return v in ("1", "true", "y", "yes")


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass
class Form4:
    issuer_cik: int | None
    issuer_name: str | None
    ticker: str | None
    owner_name: str
    owner_cik: int | None
    is_director: bool
    is_officer: bool
    is_ten_percent: bool
    officer_title: str | None
    plan_10b5_1: bool
    trades: list[dict] = field(default_factory=list)


def parse_form4(xml_text: str) -> Form4:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    issuer = root.find("issuer")
    ticker = (_txt(issuer, "issuerTradingSymbol") or "").upper().strip()
    if ticker in ("", "NONE", "N/A", "NA"):
        ticker = None

    owners = root.findall("reportingOwner")
    first = owners[0] if owners else None
    oid = first.find("reportingOwnerId") if first is not None else None
    rel = first.find("reportingOwnerRelationship") if first is not None else None
    name = _txt(oid, "rptOwnerName") or "Inconnu"
    if len(owners) > 1:
        name += " et al."
    # Rôle : on cumule les relations de tous les propriétaires déclarants.
    is_dir = any(_flag(o.find("reportingOwnerRelationship"), "isDirector") for o in owners)
    is_off = any(_flag(o.find("reportingOwnerRelationship"), "isOfficer") for o in owners)
    is_ten = any(_flag(o.find("reportingOwnerRelationship"), "isTenPercentOwner") for o in owners)
    title = _txt(rel, "officerTitle")
    if not title:
        for o in owners:
            title = _txt(o.find("reportingOwnerRelationship"), "officerTitle")
            if title:
                break

    footnotes = " ".join((f.text or "") for f in root.iter("footnote"))
    plan = _flag(root, "aff10b5One") or bool(re.search(r"10b5-?1", footnotes, re.IGNORECASE))

    trades: list[dict] = []
    table = root.find("nonDerivativeTable")
    line_no = 0
    if table is not None:
        for tx in table.findall("nonDerivativeTransaction"):
            line_no += 1
            code = (_txt(tx, "transactionCoding/transactionCode") or "").upper()
            if code not in KEEP_CODES:
                continue
            shares = _num(_txt(tx, "transactionAmounts/transactionShares"))
            price = _num(_txt(tx, "transactionAmounts/transactionPricePerShare"))
            ad = _txt(tx, "transactionAmounts/transactionAcquiredDisposedCode")
            trades.append(
                {
                    "line_no": line_no,
                    "code": code,
                    "shares": shares,
                    "price": price,
                    "value": round(shares * price, 2) if shares is not None and price is not None else None,
                    "acquired": None if ad is None else ad.upper() == "A",
                    "shares_after": _num(_txt(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                    "trade_date": _date(_txt(tx, "transactionDate")),
                }
            )

    cik_txt = _txt(issuer, "issuerCik")
    owner_cik = _txt(oid, "rptOwnerCik")
    return Form4(
        issuer_cik=int(cik_txt) if cik_txt and cik_txt.isdigit() else None,
        issuer_name=_txt(issuer, "issuerName"),
        ticker=ticker,
        owner_name=name,
        owner_cik=int(owner_cik) if owner_cik and owner_cik.isdigit() else None,
        is_director=is_dir,
        is_officer=is_off,
        is_ten_percent=is_ten,
        officer_title=title,
        plan_10b5_1=plan,
        trades=trades,
    )


async def _ticker_for_cik(conn, cik: int | None) -> str | None:
    if cik is None:
        return None
    cur = await conn.execute("SELECT ticker FROM cik_tickers WHERE cik = %s", (cik,))
    row = await cur.fetchone()
    return row["ticker"] if row else None


async def apply_amendment(conn, accession: str, form: Form4, filed: date | None) -> int:
    """Une 4/A reprend la déclaration corrigée : les lignes d'origine (même société, même déclarant, mêmes dates)
    sont marquées remplacées et sortent du flux ; elles restent en base pour la trace."""
    dates = sorted({t["trade_date"] for t in form.trades if t["trade_date"]})
    if not dates or form.issuer_cik is None or form.owner_cik is None:
        return 0
    cur = await conn.execute(
        """UPDATE insider_trades SET amended_by=%s
           WHERE issuer_cik=%s AND owner_cik=%s AND trade_date = ANY(%s) AND accession <> %s
             AND amended_by IS NULL AND (%s::date IS NULL OR filed_date <= %s::date)""",
        (accession, form.issuer_cik, form.owner_cik, dates, accession, filed, filed))
    return cur.rowcount


async def store_form4(conn, accession: str, form: Form4, filed: date | None, url: str,
                      amendment: bool = False) -> int:
    ticker = form.ticker or await _ticker_for_cik(conn, form.issuer_cik)
    n = 0
    for t in form.trades:
        await conn.execute(
            """INSERT INTO insider_trades (accession, line_no, issuer_cik, issuer_name, ticker, owner_name, owner_cik,
                 is_director, is_officer, is_ten_percent, officer_title, code, acquired, shares, price, value,
                 shares_after, trade_date, filed_date, plan_10b5_1, url, is_amendment)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (accession, line_no) DO NOTHING""",
            (
                accession, t["line_no"], form.issuer_cik, form.issuer_name, ticker, form.owner_name, form.owner_cik,
                form.is_director, form.is_officer, form.is_ten_percent, form.officer_title, t["code"], t["acquired"],
                t["shares"], t["price"], t["value"], t["shares_after"], t["trade_date"], filed, form.plan_10b5_1, url,
                amendment,
            ),
        )
        n += 1
    return n


async def process_queue(client: SecClient, conn, limit: int = BATCH_SIZE, stop: asyncio.Event | None = None,
                        form: str = "4") -> dict:
    cur = await conn.execute(
        "SELECT accession, url, filed_date, form_type FROM sec_filings WHERE status = 'pending'"
        " AND form_type = ANY(%s) ORDER BY filed_date DESC NULLS LAST, created_at LIMIT %s",
        ([form, form + "/A"] if form == "4" else [form], limit),
    )
    rows = await cur.fetchall()
    await conn.commit()
    stats = {"filings": 0, "trades": 0, "errors": 0}

    async def download(url: str) -> str | Exception:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return exc

    # Jusqu'à FETCH_CONCURRENCY téléchargements en vol (le client garde ≤ 5 req/s, la SEC répond
    # entre 0,3 et 5 s), écriture en base séquentielle avec un commit par dépôt.
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch(row: dict):
        async with sem:
            if stop is not None and stop.is_set():
                return row, asyncio.CancelledError()
            return row, await download(row["url"])

    tasks = [asyncio.create_task(fetch(r)) for r in rows]
    try:
        for fut in asyncio.as_completed(tasks):
            row, got = await fut
            if isinstance(got, asyncio.CancelledError):
                continue
            await _store_batch(conn, [(row, got)], stats)
    finally:
        for t in tasks:
            t.cancel()
    return stats


async def _store_batch(conn, items: list, stats: dict) -> None:
    for row, got in items:
        acc = row["accession"]
        try:
            if isinstance(got, Exception):
                raise got
            txt = got
            filed = row["filed_date"] or filed_date_from_txt(txt)
            link = row["url"].removesuffix(".txt") + "-index.htm"
            if row.get("form_type") == "144":
                xml = extract_xml_block(txt, "edgarSubmission")
                if xml is None:
                    raise ValueError("bloc <edgarSubmission> introuvable")
                stats["trades"] += await store_form144(conn, acc, parse_form144(xml), filed, link)
            else:
                xml = extract_ownership_xml(txt)
                if xml is None:
                    raise ValueError("bloc <ownershipDocument> introuvable")
                form = parse_form4(xml)
                amendment = row.get("form_type") == "4/A"
                if amendment:
                    stats["amended"] = stats.get("amended", 0) + await apply_amendment(conn, acc, form, filed)
                stats["trades"] += await store_form4(conn, acc, form, filed, link, amendment=amendment)
            await conn.execute(
                "UPDATE sec_filings SET status='done', attempts=attempts+1, filed_date=COALESCE(filed_date,%s),"
                " last_error=NULL, updated_at=now() WHERE accession=%s",
                (filed, acc),
            )
            await conn.commit()
            stats["filings"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — une erreur de dépôt n'arrête pas la file
            await conn.rollback()
            stats["errors"] += 1
            msg = f"{type(exc).__name__}: {exc}"[:500]
            await conn.execute(
                "UPDATE sec_filings SET attempts=attempts+1, last_error=%s, updated_at=now(),"
                " status = CASE WHEN attempts+1 >= %s THEN 'error' ELSE 'pending' END WHERE accession=%s",
                (msg, MAX_ATTEMPTS, acc),
            )
            await conn.commit()
            _log.warning("dépôt %s en erreur : %s", acc, msg)


# ---------------------------------------------------------------- Form 144


def _strip_ns(root) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _us_date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return datetime.strptime(v.strip(), "%m/%d/%Y").date()
    except ValueError:
        return _date(v)


def parse_form144(xml_text: str) -> dict:
    """Avis de vente envisagée (Form 144). Une ligne par catégorie de titres à vendre."""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    _strip_ns(root)
    fd = root.find("formData")
    issuer = fd.find("issuerInfo") if fd is not None else None
    rels = [(r.text or "").strip() for r in (issuer.iter("relationshipToIssuer") if issuer is not None else [])]
    sales = []
    for si in (fd.findall("securitiesInformation") if fd is not None else []):
        sales.append({
            "security_class": (si.findtext("securitiesClassTitle") or "").strip() or None,
            "shares": _num(si.findtext("noOfUnitsSold")),
            "market_value": _num(si.findtext("aggregateMarketValue")),
            "shares_outstanding": _num(si.findtext("noOfUnitsOutstanding")),
            "approx_sale_date": _us_date(si.findtext("approxSaleDate")),
        })
    plan_dates = [(d.text or "").strip() for tag in ("planAdoptionDate", "planAdoptionDates")
                  for d in root.iter(tag) if (d.text or "").strip()]
    remarks = " ".join((r.text or "") for r in root.iter("remarks"))
    cik = (issuer.findtext("issuerCik") if issuer is not None else "") or ""
    return {
        "issuer_cik": int(cik) if cik.strip().isdigit() else None,
        "issuer_name": (issuer.findtext("issuerName") or "").strip() if issuer is not None else None,
        "person": ((issuer.findtext("nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") or "").strip()
                   if issuer is not None else "") or "Inconnu",
        "relationship": ", ".join(r for r in rels if r) or None,
        "plan_10b5_1": bool(plan_dates) or bool(re.search(r"10b5-?1", remarks, re.I)),
        "sales": sales,
    }


MAX_PRICE_PER_SHARE = 50_000  # au-delà : montant saisi par erreur (Berkshire A exceptée)


def plausible_value(shares: float | None, value: float | None, ticker: str | None) -> bool:
    if not shares or not value:
        return True
    return value / shares <= MAX_PRICE_PER_SHARE or (ticker or "").upper() in ("BRK.A", "BRK-A")


async def store_form144(conn, accession: str, n: dict, filed: date | None, url: str) -> int:
    if not n["sales"]:
        return 0
    # Plusieurs catégories de titres : on cumule (cas rare), date de vente la plus proche.
    shares = sum(s["shares"] or 0 for s in n["sales"]) or None
    value = sum(s["market_value"] or 0 for s in n["sales"]) or None
    first = n["sales"][0]
    ticker = await _ticker_for_cik(conn, n["issuer_cik"])
    if not plausible_value(shares, value, ticker):
        value = None  # erreur de saisie dans le dépôt (ex. 4,1 Md$ pour 12 000 actions)
    cur = await conn.execute(
        """INSERT INTO sale_notices (accession, issuer_cik, issuer_name, ticker, person, relationship, security_class,
             shares, market_value, shares_outstanding, approx_sale_date, filed_date, plan_10b5_1, url)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (accession) DO NOTHING""",
        (accession, n["issuer_cik"], n["issuer_name"], ticker, n["person"], n["relationship"], first["security_class"],
         shares, value, first["shares_outstanding"],
         min((s["approx_sale_date"] for s in n["sales"] if s["approx_sale_date"]), default=None),
         filed, n["plan_10b5_1"], url),
    )
    return cur.rowcount


# ---------------------------------------------------------------- CIK → ticker


def parse_company_tickers(data: dict) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for item in data.values():
        try:
            cik = int(item["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
        if cik not in out and item.get("ticker"):
            out[cik] = (str(item["ticker"]).upper(), str(item.get("title") or ""))
    return out


async def refresh_tickers(client: SecClient, conn) -> int:
    resp = await client.get(TICKERS_URL)
    resp.raise_for_status()
    mapping = parse_company_tickers(resp.json())
    async with conn.cursor() as cur:
        await cur.executemany(
            "INSERT INTO cik_tickers(cik, ticker, title, updated_at) VALUES (%s,%s,%s,now())"
            " ON CONFLICT (cik) DO UPDATE SET ticker=EXCLUDED.ticker, title=EXCLUDED.title, updated_at=now()",
            [(cik, t, title) for cik, (t, title) in mapping.items()],
        )
    # Complète les transactions déjà lues sans ticker.
    await conn.execute(
        "UPDATE insider_trades it SET ticker = ct.ticker FROM cik_tickers ct"
        " WHERE it.ticker IS NULL AND it.issuer_cik = ct.cik"
    )
    await conn.execute(
        "UPDATE ownership_filings o SET ticker = ct.ticker FROM cik_tickers ct"
        " WHERE o.ticker IS NULL AND o.subject_cik = ct.cik"
    )
    await conn.execute(
        "UPDATE sale_notices s SET ticker = ct.ticker FROM cik_tickers ct"
        " WHERE s.ticker IS NULL AND s.issuer_cik = ct.cik"
    )
    await conn.commit()
    return len(mapping)


# ---------------------------------------------------------------- 13D / 13G


def normalize_ownership_form(term: str) -> str | None:
    t = term.upper().strip()
    for prefix in ("SCHEDULE ", "SC "):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    if t in ("13D", "13D/A", "13G", "13G/A"):
        return t
    return None


def pair_ownership_entries(entries: list[AtomEntry]) -> list[dict]:
    by_acc: dict[str, dict] = {}
    for e in entries:
        form = normalize_ownership_form(e.form)
        if form is None:
            continue
        rec = by_acc.setdefault(
            e.accession,
            {"accession": e.accession, "form_type": form, "filed": e.filed, "link": e.link,
             "subject_cik": None, "subject_name": None, "filer_cik": None, "filer_name": None},
        )
        if e.role.lower().startswith("subject"):
            rec["subject_cik"], rec["subject_name"] = e.cik, e.name
        else:
            rec["filer_cik"], rec["filer_name"] = e.cik, e.name
        rec["filed"] = rec["filed"] or e.filed
    return list(by_acc.values())


async def poll_ownership(client: SecClient, conn) -> int:
    total = 0
    for form_type in ("SCHEDULE%2013D", "SCHEDULE%2013G"):
        for page in range(MAX_PAGES):
            resp = await client.get(CURRENT_URL.format(type=form_type, start=page * 100))
            resp.raise_for_status()
            entries = parse_atom(resp.content)
            new = 0
            for rec in pair_ownership_entries(entries):
                ticker = await _ticker_for_cik(conn, rec["subject_cik"])
                cur = await conn.execute(
                    """INSERT INTO ownership_filings (accession, form_type, subject_cik, subject_name, ticker,
                         filer_cik, filer_name, filed_date, url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (accession) DO UPDATE SET
                         subject_cik = COALESCE(ownership_filings.subject_cik, EXCLUDED.subject_cik),
                         subject_name = COALESCE(ownership_filings.subject_name, EXCLUDED.subject_name),
                         ticker = COALESCE(ownership_filings.ticker, EXCLUDED.ticker),
                         filer_cik = COALESCE(ownership_filings.filer_cik, EXCLUDED.filer_cik),
                         filer_name = COALESCE(ownership_filings.filer_name, EXCLUDED.filer_name)
                       RETURNING (xmax = 0) AS inserted""",
                    (rec["accession"], rec["form_type"], rec["subject_cik"], rec["subject_name"], ticker,
                     rec["filer_cik"], rec["filer_name"], rec["filed"], rec["link"]),
                )
                r = await cur.fetchone()
                if r and r["inserted"]:
                    new += 1
            await conn.commit()
            total += new
            if new == 0 or len(entries) < 100:
                break
    return total
