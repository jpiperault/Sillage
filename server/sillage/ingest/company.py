"""Secteur d'activité des sociétés : code SIC du fichier « submissions » de la SEC, regroupé en grands secteurs."""
from __future__ import annotations

from .. import log

_log = log.get("company")
URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# (début, fin, secteur) sur les 4 chiffres du code SIC ; le premier intervalle qui contient le code l'emporte.
SECTORS = [
    (2830, 2836, "Santé"), (3841, 3851, "Santé"), (8000, 8099, "Santé"), (5122, 5122, "Santé"),
    (3570, 3579, "Technologie"), (3600, 3699, "Technologie"), (7370, 7379, "Technologie"), (3820, 3829, "Technologie"),
    (4800, 4899, "Télécoms et médias"), (2710, 2799, "Télécoms et médias"), (7800, 7899, "Télécoms et médias"),
    (1300, 1399, "Énergie"), (2900, 2999, "Énergie"), (4900, 4999, "Services publics"),
    (6798, 6798, "Immobilier"), (6500, 6599, "Immobilier"), (6000, 6799, "Finance"),
    (1000, 1499, "Matières premières"), (2800, 2829, "Matières premières"), (2840, 2899, "Matières premières"),
    (3300, 3399, "Matières premières"), (2400, 2499, "Matières premières"), (2600, 2699, "Matières premières"),
    (1500, 1799, "Industrie"), (3400, 3569, "Industrie"), (3580, 3599, "Industrie"), (3700, 3799, "Industrie"),
    (3800, 3819, "Industrie"), (4000, 4799, "Industrie"), (8700, 8799, "Industrie"),
    (5000, 5199, "Consommation"), (5200, 5999, "Consommation"), (2000, 2399, "Consommation"),
    (2500, 2599, "Consommation"), (3000, 3299, "Consommation"), (3900, 3999, "Consommation"),
    (7000, 7299, "Consommation"), (7500, 7699, "Consommation"), (7900, 7999, "Consommation"),
    (100, 999, "Consommation"), (7300, 7369, "Industrie"), (7380, 7399, "Industrie"), (8100, 8999, "Industrie"),
]


def sector_for(sic: str | None) -> str | None:
    if not sic or not str(sic).isdigit():
        return None
    n = int(sic)
    for lo, hi, name in SECTORS:
        if lo <= n <= hi:
            return name
    return "Autres"


async def refresh(client, conn, limit: int = 400) -> dict:
    """Sociétés citées par des initiés (historique et flux) dont le secteur n'est pas encore connu."""
    cur = await conn.execute(
        """SELECT cik FROM (SELECT DISTINCT issuer_cik AS cik FROM bt_signals
                           UNION SELECT DISTINCT issuer_cik FROM insider_trades) t
           WHERE cik IS NOT NULL AND cik NOT IN (SELECT cik FROM company_info) LIMIT %s""", (limit,))
    ciks = [r["cik"] for r in await cur.fetchall()]
    await conn.commit()
    ok = 0
    for cik in ciks:
        resp = await client.get(URL.format(cik=cik))
        if resp.status_code == 404:
            await conn.execute("INSERT INTO company_info (cik) VALUES (%s) ON CONFLICT DO NOTHING", (cik,))
            continue
        resp.raise_for_status()
        d = resp.json()
        sic = str(d.get("sic") or "") or None
        await conn.execute(
            "INSERT INTO company_info (cik, name, sic, sic_label, sector) VALUES (%s,%s,%s,%s,%s)"
            " ON CONFLICT (cik) DO UPDATE SET name=EXCLUDED.name, sic=EXCLUDED.sic, sic_label=EXCLUDED.sic_label,"
            " sector=EXCLUDED.sector, fetched_at=now()",
            (cik, d.get("name"), sic, d.get("sicDescription"), sector_for(sic)))
        await conn.commit()
        ok += 1
    cur = await conn.execute(
        """SELECT count(*) AS n FROM (SELECT DISTINCT issuer_cik AS cik FROM bt_signals
                                      UNION SELECT DISTINCT issuer_cik FROM insider_trades) t
           WHERE cik IS NOT NULL AND cik NOT IN (SELECT cik FROM company_info)""")
    remaining = (await cur.fetchone())["n"]
    await conn.commit()
    return {"fetched": ok, "remaining": remaining}
