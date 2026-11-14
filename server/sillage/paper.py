"""Mode Entraînement : portefeuilles fictifs par compte.

Deux portefeuilles (« books ») par compte :
- manual : ordres passés à la main depuis les tickets ;
- rule : règle automatique (copie des signaux au-dessus d'un score, sortie à horizon ou au stop).
Les actions européennes (ISIN, cours Euronext en euros) sont converties en dollars au taux BCE du jour.
"""
from __future__ import annotations

from decimal import Decimal

from . import log

_log = log.get("paper")
STOP_PCT = Decimal("0.08")
BOOKS = ("manual", "rule")


class PaperError(Exception):
    pass


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


async def ensure_account(conn, user_id: int, start_cash: float, book: str = "manual") -> dict:
    await conn.execute(
        "INSERT INTO paper_accounts(user_id, book, cash, start_cash) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (user_id, book, start_cash, start_cash),
    )
    cur = await conn.execute("SELECT * FROM paper_accounts WHERE user_id=%s AND book=%s FOR UPDATE", (user_id, book))
    return await cur.fetchone()


async def buy(conn, user_id: int, ticker: str, quantity: float, price: float, start_cash: float,
              signal_ref: str | None = None, book: str = "manual", reason: str = "manual",
              currency: str = "USD", note: str | None = None) -> dict:
    """price : en dollars (déjà converti pour une action européenne)."""
    qty, px = _d(quantity), _d(price)
    if qty <= 0:
        raise PaperError("La quantité doit être positive.")
    if px <= 0:
        raise PaperError("Cours indisponible.")
    acct = await ensure_account(conn, user_id, start_cash, book)
    cost = qty * px
    if cost > _d(acct["cash"]):
        raise PaperError(f"Liquidités insuffisantes : {float(acct['cash']):,.2f} $ disponibles.".replace(",", " "))
    cur = await conn.execute(
        "SELECT quantity, avg_price FROM paper_positions WHERE user_id=%s AND book=%s AND ticker=%s FOR UPDATE",
        (user_id, book, ticker))
    pos = await cur.fetchone()
    if pos:
        new_qty = _d(pos["quantity"]) + qty
        avg = (_d(pos["quantity"]) * _d(pos["avg_price"]) + cost) / new_qty
        await conn.execute(
            "UPDATE paper_positions SET quantity=%s, avg_price=%s, stop_price=%s"
            " WHERE user_id=%s AND book=%s AND ticker=%s",
            (new_qty, avg, avg * (1 - STOP_PCT), user_id, book, ticker))
    else:
        await conn.execute(
            "INSERT INTO paper_positions(user_id, book, ticker, quantity, avg_price, stop_price, signal_ref, currency)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, book, ticker, qty, px, px * (1 - STOP_PCT), signal_ref, currency))
    await conn.execute("UPDATE paper_accounts SET cash = cash - %s WHERE user_id=%s AND book=%s", (cost, user_id, book))
    await conn.execute(
        "INSERT INTO paper_orders(user_id, book, ticker, side, quantity, price, reason, signal_ref, note)"
        " VALUES (%s,%s,%s,'buy',%s,%s,%s,%s,%s)",
        (user_id, book, ticker, qty, px, reason, signal_ref, note))
    return {"ticker": ticker, "side": "buy", "quantity": float(qty), "price": float(px), "book": book}


async def sell_all(conn, user_id: int, ticker: str, price: float, reason: str = "manual", book: str = "manual",
                   note: str | None = None) -> dict:
    px = _d(price)
    cur = await conn.execute(
        "DELETE FROM paper_positions WHERE user_id=%s AND book=%s AND ticker=%s"
        " RETURNING quantity, avg_price, signal_ref", (user_id, book, ticker))
    pos = await cur.fetchone()
    if not pos:
        raise PaperError(f"Aucune position fictive sur {ticker}.")
    qty = _d(pos["quantity"])
    pnl = (px - _d(pos["avg_price"])) * qty
    await conn.execute("UPDATE paper_accounts SET cash = cash + %s WHERE user_id=%s AND book=%s",
                       (qty * px, user_id, book))
    await conn.execute(
        "INSERT INTO paper_orders(user_id, book, ticker, side, quantity, price, pnl, reason, signal_ref, note)"
        " VALUES (%s,%s,%s,'sell',%s,%s,%s,%s,%s,%s)",
        (user_id, book, ticker, qty, px, pnl, reason, pos["signal_ref"], note))
    return {"ticker": ticker, "side": "sell", "quantity": float(qty), "price": float(px), "pnl": float(pnl),
            "book": book}


async def reset(conn, user_id: int, start_cash: float, book: str = "manual") -> None:
    await conn.execute("DELETE FROM paper_positions WHERE user_id=%s AND book=%s", (user_id, book))
    await conn.execute("DELETE FROM paper_orders WHERE user_id=%s AND book=%s", (user_id, book))
    await conn.execute("DELETE FROM paper_equity WHERE user_id=%s AND book=%s", (user_id, book))
    await conn.execute(
        "INSERT INTO paper_accounts(user_id, book, cash, start_cash, reset_at) VALUES (%s,%s,%s,%s,now())"
        " ON CONFLICT (user_id, book) DO UPDATE SET cash=EXCLUDED.cash, start_cash=EXCLUDED.start_cash, reset_at=now()",
        (user_id, book, start_cash, start_cash))


# Cours en dollars : les cours en euros (actions européennes) sont convertis au taux BCE.
PRICE_USD_SQL = """CASE WHEN pr.currency = 'EUR' THEN pr.price * (SELECT per_eur FROM fx_rates WHERE currency='USD')
                        ELSE pr.price END"""


async def summary(conn, user_id: int, start_cash: float, book: str = "manual") -> dict:
    acct = await ensure_account(conn, user_id, start_cash, book)
    cur = await conn.execute(
        f"""SELECT p.ticker, p.quantity, p.avg_price, p.stop_price, p.signal_ref, p.opened_at, p.currency,
                  {PRICE_USD_SQL} AS last_price, pr.price AS local_price, pr.fetched_at AS price_at
           FROM paper_positions p LEFT JOIN prices pr ON pr.ticker = p.ticker
           WHERE p.user_id=%s AND p.book=%s ORDER BY p.opened_at""",
        (user_id, book))
    positions = []
    invested = market = Decimal(0)
    for r in await cur.fetchall():
        qty, avg = _d(r["quantity"]), _d(r["avg_price"])
        last = _d(r["last_price"]) if r["last_price"] is not None else avg
        value = qty * last
        invested += qty * avg
        market += value
        positions.append({
            "ticker": r["ticker"], "quantity": float(qty), "avg_price": float(avg), "currency": r["currency"],
            "stop_price": float(r["stop_price"]) if r["stop_price"] is not None else None,
            "last_price": float(last), "price_at": r["price_at"], "value": float(value),
            "pnl": float(value - qty * avg), "pnl_pct": float((last / avg - 1) * 100) if avg else 0.0,
            "signal_ref": r["signal_ref"], "opened_at": r["opened_at"],
        })
    cur = await conn.execute(
        "SELECT id, ticker, side, quantity, price, pnl, reason, signal_ref, note, created_at FROM paper_orders"
        " WHERE user_id=%s AND book=%s ORDER BY created_at DESC, id DESC LIMIT 200",
        (user_id, book))
    orders = [{**r, "quantity": float(r["quantity"]), "price": float(r["price"]),
               "pnl": float(r["pnl"]) if r["pnl"] is not None else None} for r in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT coalesce(sum(pnl),0) AS realized, count(*) FILTER (WHERE pnl > 0) AS wins,"
        " count(*) FILTER (WHERE pnl IS NOT NULL) AS closed FROM paper_orders"
        " WHERE user_id=%s AND book=%s AND side='sell'", (user_id, book))
    st = await cur.fetchone()
    cash = _d(acct["cash"])
    equity = cash + market
    start = _d(acct["start_cash"])
    await conn.commit()
    return {
        "book": book, "cash": float(cash), "start_cash": float(start), "equity": float(equity),
        "invested": float(invested), "market_value": float(market),
        "unrealized": float(market - invested), "realized": float(st["realized"]),
        "total_pnl": float(equity - start), "total_pct": float((equity / start - 1) * 100) if start else 0.0,
        "closed": st["closed"], "wins": st["wins"],
        "win_rate": (st["wins"] / st["closed"] * 100) if st["closed"] else None,
        "positions": positions, "orders": orders,
    }


async def apply_stops(conn) -> int:
    """Vend les positions dont le dernier cours connu (en dollars) est passé sous le stop (−8 %)."""
    cur = await conn.execute(
        f"""SELECT p.user_id, p.book, p.ticker, {PRICE_USD_SQL} AS price
           FROM paper_positions p JOIN prices pr ON pr.ticker = p.ticker
           WHERE p.stop_price IS NOT NULL AND {PRICE_USD_SQL} <= p.stop_price""")
    rows = await cur.fetchall()
    n = 0
    for r in rows:
        if r["price"] is None:
            continue
        try:
            await sell_all(conn, r["user_id"], r["ticker"], r["price"], reason="stop", book=r["book"])
            n += 1
            _log.info("stop déclenché : %s à %s", r["ticker"], r["price"])
        except PaperError:
            pass
    await conn.commit()
    return n


async def snapshot_equity(conn) -> int:
    """Valeur du jour de chaque portefeuille (courbe) : une ligne par jour, remplacée à chaque cycle."""
    cur = await conn.execute(
        f"""INSERT INTO paper_equity (user_id, book, day, equity, cash)
            SELECT a.user_id, a.book, current_date,
                   a.cash + coalesce((SELECT sum(p.quantity * coalesce({PRICE_USD_SQL}, p.avg_price))
                                      FROM paper_positions p LEFT JOIN prices pr ON pr.ticker = p.ticker
                                      WHERE p.user_id = a.user_id AND p.book = a.book), 0),
                   a.cash
            FROM paper_accounts a
            ON CONFLICT (user_id, book, day) DO UPDATE SET equity=EXCLUDED.equity, cash=EXCLUDED.cash""")
    await conn.commit()
    return cur.rowcount


async def equity_curve(conn, user_id: int, book: str) -> list[dict]:
    cur = await conn.execute(
        """SELECT e.day, e.equity, (SELECT close FROM price_history h WHERE h.ticker='SPY' AND h.day <= e.day
                                    ORDER BY h.day DESC LIMIT 1) AS spy
           FROM paper_equity e WHERE e.user_id=%s AND e.book=%s ORDER BY e.day""", (user_id, book))
    rows = await cur.fetchall()
    await conn.commit()
    if not rows:
        return []
    spy0 = next((float(r["spy"]) for r in rows if r["spy"]), None)
    eq0 = float(rows[0]["equity"])
    return [{"d": r["day"], "equity": float(r["equity"]),
             "pct": round((float(r["equity"]) / eq0 - 1) * 100, 2) if eq0 else 0.0,
             "spy_pct": round((float(r["spy"]) / spy0 - 1) * 100, 2) if spy0 and r["spy"] else None}
            for r in rows]
