"""Form 144, AMF, 13F, FINRA : tests sur des extraits de documents réels (novembre 2026)."""
from datetime import date
from pathlib import Path

from sillage import scoring
from sillage.config import load_settings
from sillage.ingest import amf, finra, funds, sec

FIX = Path(__file__).parent / "fixtures"


def test_form144():
    n = sec.parse_form144((FIX / "form144.xml").read_text())
    assert n["issuer_cik"] == 1596532 and n["issuer_name"] == "ARISTA NETWORKS, INC."
    assert n["person"] == "LEWIS CHEW" and n["relationship"] == "Director"
    assert not n["plan_10b5_1"]  # <planAdoptionDates/> vide
    s = n["sales"][0]
    assert s["shares"] == 2500 and s["market_value"] == 492212.75
    assert s["shares_outstanding"] == 1263402335 and s["approx_sale_date"] == date(2026, 11, 13)
    with_plan = (FIX / "form144.xml").read_text().replace(
        "<planAdoptionDates/>", "<planAdoptionDates><planAdoptionDate>03/01/2026</planAdoptionDate></planAdoptionDates>")
    assert sec.parse_form144(with_plan)["plan_10b5_1"]


def test_amf_multiple_transactions():
    r = amf.parse_amf_text((FIX / "amf_multi.txt").read_text())
    assert r["person"] == "Charlie Evrard" and r["role"] == "administrateur"
    assert r["issuer"] == "MON COURTIER ENERGIE GROUPE"
    assert [t["side"] for t in r["transactions"]] == ["sell", "sell"]  # partie anglaise ignorée
    t = r["transactions"][0]
    assert t["isin"] == "FR001400H3A0"  # repris de l'en-tête
    assert t["price"] == 5.3235 and t["volume"] == 1172 and t["trade_date"] == date(2026, 11, 10)
    assert r["transactions"][1]["trade_date"] == date(2026, 11, 12)
    assert all(amf.keep(t) for t in r["transactions"])


def test_amf_legal_person_and_filters():
    r = amf.parse_amf_text((FIX / "amf_legal_person.txt").read_text())
    assert r["person"].startswith("Mathieu Petithuguenin (via PAPREC GROUP")
    assert r["role"].startswith("Président du Conseil")
    t = r["transactions"][0]
    assert t["side"] == "buy" and t["isin"] == "FR0010214064" and t["value"] == 250562.5
    gift = {**t, "nature": "Acquisition Actions Gratuites"}
    perf = {**t, "nature": "Acquisition définitive d’actions de performance", "price": 0.0}
    assert amf.keep(t) and not amf.keep(gift) and not amf.keep(perf)
    assert amf.fr_number("1 362,50") == 1362.5 and amf.fr_date("1er août 2026") == date(2026, 8, 1)


def test_french_titles_score():
    score, reasons = scoring.insider_score(
        code="P", officer_title="Président-Directeur Général", is_officer=True, is_director=False,
        is_ten_percent=False, value=300_000, delay_days=2, cluster=0, plan_10b5_1=False, price_change=None)
    assert score == 40 + 15 + 10 + 10 and any("premier plan" in r for r in reasons)


def test_13f_info_table_and_moves():
    h = funds.parse_info_table((FIX / "info_table.xml").read_bytes())
    ally = h[("02005N100", "")]
    assert ally["shares"] == 12561737 + 2803875  # deux gestionnaires agrégés
    assert ally["value"] == 577211815 + 128838056
    assert len(h) == 2
    assert funds.classify(100, None) == "new" and funds.classify(None, 100) == "sold"
    assert funds.classify(110, 100) == "added" and funds.classify(90, 100) == "reduced"
    assert funds.classify(102, 100) == "held"
    subs = {"filings": {"recent": {
        "form": ["13F-HR", "13F-HR/A", "13F-HR", "13F-HR"],
        "accessionNumber": ["a3", "a2b", "a2", "a1"],
        "reportDate": ["2026-06-30", "2026-03-31", "2026-03-31", "2025-12-31"],
        "filingDate": ["2026-08-14", "2026-06-01", "2026-05-15", "2026-02-17"]}}}
    assert [f["accession"] for f in funds.recent_13f(subs)] == ["a3", "a2"]
    assert funds.pick_ticker([{"ticker": "BRK/B", "exchCode": "US", "marketSector": "Equity", "name": "X"}])[0] == "BRK.B"


def test_funds_setting():
    assert len(funds.parse_funds_setting(None)) == len(funds.DEFAULT_FUNDS)
    assert funds.parse_funds_setting("off") == {}
    assert funds.parse_funds_setting("1067983, 123") == {1067983: funds.DEFAULT_FUNDS[1067983], 123: "CIK 123"}
    s = load_settings({"DATABASE_URL": "postgresql://x", "ADMIN_EMAIL": "a@b.test", "JWT_SECRET": "x" * 32,
                       "AMF_ENABLED": "false", "FUNDS_13F": "off"})
    assert not s.amf_enabled and s.funds_13f == {} and s.finra_enabled and s.form144_enabled


def test_finra_file():
    text = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
            "20261112|A|343594.555821|0|722448.444285|B,Q,N\n"
            "20261112|ZERO|0|0|0|Q\n"
            "20261112|BRK.B|10|0|40|N\n")
    rows = finra.parse_file(text)
    assert rows[0][:2] == (date(2026, 11, 12), "A") and len(rows) == 2  # volume nul ignoré
    assert rows[1][1] == "BRK.B" and rows[1][2] / rows[1][3] == 0.25


def test_form144_namespace_prefix():
    xml = (FIX / "form144.xml").read_text()
    prefixed = (xml.replace("<edgarSubmission xmlns=", "<own:edgarSubmission xmlns:own=")
                .replace("</edgarSubmission>", "</own:edgarSubmission>"))
    txt = f"<SEC-DOCUMENT>\n<XML>\n{prefixed}\n</XML>\n"
    block = sec.extract_xml_block(txt, "edgarSubmission")
    assert block is not None


def test_form144_implausible_value():
    assert not sec.plausible_value(12000, 4_100_000_000, "TENB")
    assert sec.plausible_value(2500, 492212.75, "ANET")
    assert sec.plausible_value(10, 7_500_000, "BRK.A")
