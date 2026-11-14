"""Alertes de cours : « préviens-moi si AAPL passe au-dessus / en dessous de X ».

Vérifiées à chaque cycle rapide sur le dernier cours connu (dans la devise de cotation) ; une alerte se déclenche
une seule fois. Notification Telegram si le compte est lié, et affichage dans l'onglet Résumé.
"""
from __future__ import annotations

import html

from . import log

_log = log.get("price_alerts")


async def check(conn, tg, public_url: str) -> dict:
    cur = await conn.execute(
        """UPDATE price_alerts a SET triggered_at=now(), triggered_price=pr.price
           FROM prices pr WHERE pr.ticker = a.ticker AND a.triggered_at IS NULL
             AND ((a.direction='above' AND pr.price >= a.price) OR (a.direction='below' AND pr.price <= a.price))
           RETURNING a.id""")
    fired = len(await cur.fetchall())
    await conn.commit()
    sent = 0
    if tg is not None:
        cur = await conn.execute(
            """SELECT a.id, a.ticker, a.direction, a.price, a.triggered_price, u.chat_id FROM price_alerts a
               JOIN user_alerts u ON u.user_id = a.user_id
               WHERE a.triggered_at IS NOT NULL AND NOT a.notified AND u.chat_id IS NOT NULL""")
        for a in await cur.fetchall():
            word = "au-dessus de" if a["direction"] == "above" else "en dessous de"
            text = (f"📈 <b>{html.escape(a['ticker'])}</b> est passé {word} {float(a['price']):g} "
                    f"(dernier cours {float(a['triggered_price']):g})")
            if public_url:
                text += f'\\n<a href="{html.escape(public_url)}/#action/{html.escape(a["ticker"])}">Voir la fiche</a>'
            try:
                await tg.send(a["chat_id"], text)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("alerte de cours non envoyée : %s", exc)
            await conn.execute("UPDATE price_alerts SET notified=true WHERE id=%s", (a["id"],))
        await conn.commit()
    return {"triggered": fired, "sent": sent}
