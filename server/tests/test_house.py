import io
import zipfile
from datetime import date

from sillage.config import load_settings
from sillage.ingest import congress, house

# Texte réel extrait par pypdf d'un PTR déposé en ligne (Filing ID #20033783), raccourci.
# Les intertitres du PDF sortent avec des caractères nuls.
PTR_TEXT = (
    "P\x00\x00\x00\x00\x00\x00\x00 T\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00 R\x00\x00\x00\x00\x00\n"
    "Clerk of the House of Representatives • Legislative Resource Center • B81 Cannon Building\n"
    "F\x00\x00\x00\x00 I\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\nName: Hon. Dr. Roger Williams\nStatus: Member\n"
    "State/District:TX25\nT\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\nID Owner Asset Transaction\nType\n"
    "Date Notification\nDate\nAmount Cap.\nGains >\n$200?\n"
    "Chevron Corporation Common Stock\n(CVX) [ST]\nS (partial) 12/22/202512/22/2025$15,001 -\n$50,000\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\nS\x00\x00\x00\x00\x00\x00\x00\x00\x00 O\x00: Charles Schwab 4067\n"
    "JP Morgan Chase & Co. Common\nStock (JPM) [ST]\nP 12/22/202512/22/2025$1,001 - $15,000\n"
    "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00: New\n"
    "SP Procter & Gamble Company (PG) [ST]S 01/29/202601/30/2026$1,001 - $15,000\n"
    "Apple Inc. Call Options (AAPL) [OP]\nP 12/22/202512/23/2025$15,001 - $50,000\n"
    "Some Holding Corp (SHC) [ST]\nE 12/22/202512/23/2025$1,001 - $15,000\n"
    "Big Co (BIG) [ST]\nP 12/20/202512/23/2025Over $50,000,000\n"
    "Digitally Signed: Hon. Roger Williams , 01/15/2026\nFiling ID #20033783"
)


def test_parse_ptr_text():
    r = house.parse_ptr_text(PTR_TEXT)
    assert r["name"] == "Roger Williams" and r["state_dst"] == "TX25"
    tx = r["transactions"]
    assert [(t["ticker"], t["side"]) for t in tx] == [("CVX", "sell"), ("JPM", "buy"), ("PG", "sell"), ("BIG", "buy")]
    cvx = tx[0]
    assert cvx["partial"] and cvx["amount_min"] == 15001 and cvx["amount_max"] == 50000
    assert cvx["trade_date"] == date(2025, 12, 22)
    assert tx[2]["notification_date"] == date(2026, 1, 30)
    assert tx[3]["amount_min"] == 50_000_000
    # numéros de ligne stables (clé de dédoublonnage) : option et échange comptés mais ignorés
    assert [t["line"] for t in tx] == [1, 2, 3, 6]


def _zip(xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", xml)
    return buf.getvalue()


def test_parse_index_zip():
    xml = """<?xml version="1.0" encoding="utf-8"?><FinancialDisclosure>
      <Member><Prefix>Hon.</Prefix><Last>Williams</Last><First>Roger</First><Suffix /><FilingType>P</FilingType>
        <StateDst>TX25</StateDst><Year>2026</Year><FilingDate>1/15/2026</FilingDate><DocID>20033783</DocID></Member>
      <Member><Prefix /><Last>Doe</Last><First>Jane</First><Suffix>Jr.</Suffix><FilingType>P</FilingType>
        <StateDst>CA12</StateDst><Year>2026</Year><FilingDate>2/3/2026</FilingDate><DocID>9116361</DocID></Member>
      <Member><Prefix /><Last>Aaron</Last><First>Richard</First><Suffix /><FilingType>W</FilingType>
        <StateDst>MI04</StateDst><Year>2026</Year><FilingDate>4/15/2026</FilingDate><DocID>8068</DocID></Member>
    </FinancialDisclosure>"""
    rows = house.parse_index_zip(_zip(xml))
    assert [r.doc_id for r in rows] == ["20033783", "9116361"]  # seulement les PTR
    assert rows[0].name == "Roger Williams" and rows[0].electronic
    assert rows[0].filing_date == date(2026, 1, 15)
    assert rows[1].name == "Jane Doe Jr." and not rows[1].electronic  # papier : scanné, ignoré


def test_house_settings_and_quiver_dedup():
    base = {"DATABASE_URL": "postgresql://x", "ADMIN_EMAIL": "a@b.test", "JWT_SECRET": "x" * 32}
    s = load_settings(base)
    assert s.house_enabled and s.congress_enabled and s.congress_sources == ["Greffe de la Chambre"]
    s = load_settings({**base, "HOUSE_ENABLED": "false", "QUIVER_API_KEY": "k"})
    assert s.congress_sources == ["Quiver Quantitative"]
    assert not load_settings({**base, "HOUSE_ENABLED": "false"}).congress_enabled
    assert congress.is_house({"House": "Representatives"}) and not congress.is_house({"House": "Senate"})


def test_clean_name():
    assert house.clean_name("Hon. Richard Dean Dr McCormick") == "Richard Dean McCormick"
    assert house.clean_name("John J Mr McGuire III") == "John J McGuire III"
    assert house.clean_name("Hon. Dr. Roger Williams") == "Roger Williams"
    assert house.clean_name("Scott Scott Franklin") == "Scott Franklin"
    assert house.clean_name("Drew Ferguson") == "Drew Ferguson"  # « Dr » seulement en mot entier
