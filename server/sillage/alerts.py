"""Alertes Telegram.

Mise en place : l'administrateur crée un bot avec @BotFather et colle son jeton dans Compte → Réglages.
Chaque utilisateur clique « Lier Telegram » : Sillage donne un lien t.me/<bot>?start=<code> ; le message /start <code>
reçu par le bot associe la conversation au compte. Ensuite, après chaque cycle de collecte, l'utilisateur reçoit :
- les nouveaux signaux dont le score atteint son seuil ;
- toute nouvelle activité (signal, Form 144) sur les tickers de sa watchlist ;
- les stops déclenchés dans son portefeuille fictif.
"""
from __future__ import annotations

import asyncio
import html
import secrets
from datetime import datetime, timezone

import httpx

from . import log

_log = log.get("alerts")

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LINES = 15


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, **params):
        resp = await self._client.post(API.format(token=self.token, method=method), json=params)
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"réponse illisible ({resp.status_code})") from exc
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"erreur {resp.status_code}")
        return data["result"]

    async def username(self) -> str:
        return (await self.call("getMe"))["username"]

    async def send(self, chat_id: int, text: str) -> None:
        await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                        disable_web_page_preview=True)


def new_link_code() -> str:
    return secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]


def parse_start(text: str | None) -> str | None:
    """« /start ABC » → « ABC »."""
    if not text or not text.startswith("/start"):
        return None
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


async def poll_links(conn, tg: Telegram, offset: int, long_poll: int = 10) -> int:
    """Lit les messages reçus par le bot et associe les comptes. Renvoie le nouvel offset."""
    updates = await tg.call("getUpdates", offset=offset, timeout=long_poll, allowed_updates=["message"])
    for u in updates:
        offset = max(offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        code = parse_start(msg.get("text"))
        if not chat_id:
            continue
        if not code:
            await tg.send(chat_id, "Bonjour ! Pour recevoir les alertes Sillage, utilisez le lien affiché dans "
                                   "l'onglet <b>Compte</b> du site.")
            continue
        cur = await conn.execute(
            """UPDATE user_alerts SET chat_id=%s, link_code=NULL, linked_at=now(), last_check=now()
               WHERE link_code=%s RETURNING user_id""", (chat_id, code))
        row = await cur.fetchone()
        await conn.commit()
        if row:
            cur = await conn.execute("SELECT email FROM users WHERE id=%s", (row["user_id"],))
            email = (await cur.fetchone())["email"]
            await conn.commit()
            await tg.send(chat_id, f"✅ Alertes Sillage activées pour <b>{html.escape(email)}</b>.")
            _log.info("Telegram lié pour le compte %s", row["user_id"])
        else:
            await tg.send(chat_id, "Lien expiré ou déjà utilisé : recommencez depuis l'onglet Compte.")
    return offset


def _fmt_money(v, currency: str | None = None) -> str:
    if v is None:
        return ""
    sym = "€" if currency == "EUR" else "$"
    if v >= 1e6:
        return f"{v / 1e6:.1f} M{sym}".replace(".", ",")
    if v >= 1e3:
        return f"{v / 1e3:.0f} K{sym}"
    return f"{v:.0f} {sym}"


SIDE = {"buy": "Achat", "sell": "Vente", "option": "Options"}


def format_signal(s: dict) -> str:
    label = html.escape(s.get("ticker") or s.get("company") or "?")
    amount = s.get("amount_range") or _fmt_money(s.get("value"), s.get("currency"))
    who = html.escape(f"{s['person']} ({s['role']})" if s.get("role") else s["person"])
    return f"• <b>{s['score']}</b> {label} — {SIDE.get(s['side'], s['side'])} {html.escape(amount)} — {who}"


def format_message(signals: list[dict], notices: list[dict], stops: list[dict], public_url: str) -> str | None:
    lines: list[str] = []
    if signals:
        lines.append(f"🔔 <b>{len(signals)} nouveau(x) signal(aux)</b>")
        lines += [format_signal(s) for s in signals[:MAX_LINES]]
        if len(signals) > MAX_LINES:
            lines.append(f"… et {len(signals) - MAX_LINES} autre(s)")
    if notices:
        lines.append("📣 <b>Ventes annoncées (Form 144) sur votre watchlist</b>")
        lines += [f"• {html.escape(n['ticker'])} — {html.escape(n['person'])} {_fmt_money(n['market_value'])}"
                  for n in notices[:MAX_LINES]]
    if stops:
        lines.append("🛑 <b>Stop déclenché (portefeuille fictif)</b>")
        lines += [f"• {html.escape(o['ticker'])} vendu à {float(o['price']):.2f} $ ({float(o['pnl']):+.2f} $)"
                  for o in stops]
    if not lines:
        return None
    if public_url:
        lines.append(f'\n<a href="{html.escape(public_url)}/">Ouvrir Sillage</a>')
    return "\n".join(lines)


async def collect_for_user(conn, user: dict, since: datetime, signals_recent: list[dict], new_ids: set[str]) -> tuple:
    cur = await conn.execute("SELECT ticker FROM watchlist WHERE user_id=%s", (user["user_id"],))
    watch = {r["ticker"] for r in await cur.fetchall()}
    picked = [s for s in signals_recent if s["id"] in new_ids
              and (s["score"] >= user["min_score"] or (user["watchlist"] and s.get("ticker") in watch))]
    picked.sort(key=lambda s: s["score"], reverse=True)
    notices = []
    if user["watchlist"] and watch:
        cur = await conn.execute(
            "SELECT ticker, person, market_value FROM sale_notices WHERE created_at > %s AND ticker = ANY(%s)",
            (since, list(watch)))
        notices = await cur.fetchall()
    stops = []
    if user["stops"]:
        cur = await conn.execute(
            "SELECT ticker, price, pnl FROM paper_orders WHERE user_id=%s AND reason='stop' AND created_at > %s",
            (user["user_id"], since))
        stops = await cur.fetchall()
    await conn.commit()
    return picked, notices, stops


async def new_signal_ids(conn, since: datetime) -> set[str]:
    ids: set[str] = set()
    for prefix, sql in (
        ("i", "SELECT min(id) AS id FROM insider_trades WHERE created_at > %s GROUP BY accession, code"),
        ("c", "SELECT id FROM congress_trades WHERE created_at > %s"),
        ("a", "SELECT id FROM amf_trades WHERE created_at > %s"),
    ):
        cur = await conn.execute(sql, (since,))
        ids |= {f"{prefix}{r['id']}" for r in await cur.fetchall()}
    await conn.commit()
    return ids


async def send_alerts(conn, tg: Telegram, build_signals, public_url: str) -> dict:
    """build_signals(conn) → signaux récents scorés (fonction de l'API, passée pour éviter un import circulaire)."""
    cur = await conn.execute("SELECT * FROM user_alerts WHERE chat_id IS NOT NULL")
    users = await cur.fetchall()
    await conn.commit()
    if not users:
        return {"users": 0, "sent": 0}
    now = datetime.now(timezone.utc)
    oldest = min(u["last_check"] for u in users)
    new_ids = await new_signal_ids(conn, oldest)
    recent = await build_signals(conn) if new_ids else []
    sent = 0
    for u in users:
        ids_u = await new_signal_ids(conn, u["last_check"]) if u["last_check"] > oldest else new_ids
        signals, notices, stops = await collect_for_user(conn, u, u["last_check"], recent, ids_u)
        text = format_message(signals, notices, stops, public_url)
        if text:
            try:
                await tg.send(u["chat_id"], text)
                sent += 1
            except TelegramError as exc:
                _log.warning("alerte non envoyée au compte %s : %s", u["user_id"], exc)
                if "blocked" in str(exc).lower() or "chat not found" in str(exc).lower():
                    await conn.execute("UPDATE user_alerts SET chat_id=NULL WHERE user_id=%s", (u["user_id"],))
        await conn.execute("UPDATE user_alerts SET last_check=%s WHERE user_id=%s", (now, u["user_id"]))
        await conn.commit()
    return {"users": len(users), "sent": sent}


class Linker:
    """Petite boucle qui écoute le bot (getUpdates) pour lier les comptes. Tourne tant qu'un jeton est réglé."""

    def __init__(self, pool, get_token):
        self.pool = pool
        self.get_token = get_token
        self.task: asyncio.Task | None = None
        self.stop = asyncio.Event()

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="telegram-linker")

    async def shutdown(self) -> None:
        self.stop.set()
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        offset = 0
        tg: Telegram | None = None
        while not self.stop.is_set():
            token = self.get_token()
            if not token:
                await asyncio.sleep(15)
                continue
            if tg is None or tg.token != token:
                if tg:
                    await tg.close()
                tg, offset = Telegram(token), 0
            try:
                async with self.pool.connection() as conn:
                    offset = await poll_links(conn, tg, offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("écoute Telegram : %s", exc)
                await asyncio.sleep(30)
        if tg:
            await tg.close()
