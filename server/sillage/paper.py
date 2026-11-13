"""Mode Entraînement : portefeuille fictif par compte."""
from __future__ import annotations

from decimal import Decimal

from . import log

_log = log.get("paper")
STOP_PCT = Decimal("0.08")


class PaperError(Exception):
    pass


def _d(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


async def ensure_account(conn, user_id: int, start_cash: float) -> dict:
    await conn.execute(
        "INSERT INTO paper_accounts(user_id, cash, start_cash) VALUES (%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, start_cash, start_cash),
    )
    cur = await conn.execute("SELECT * FROM paper_accounts WHERE user_id=%s FOR UPDATE", (user_id,))
    return await cur.fetchone()


async def buy(conn, user_id: int, ticker: str, quantity: float, price: float, start_cash: float,
              signal_ref: str | None = None) -> dict:
    qty, px = _d(quantity), _d(price)
    if qty <= 0:
        raise PaperError("La quantité doit être positive.")
    if px <= 0:
        raise PaperError("Cours indisponible.")
    acct = await ensure_account(conn, user_id, start_cash)
    cost = qty * px
    if cost > _d(acct["cash"]):
        raise PaperError(f"Liquidités insuffisantes : {float(acct['cash']):,.2f} $ disponibles.".replace(",", " "))
    cur = await conn.execute(
        "SELECT quantity, avg_price FROM paper_positions WHERE user_id=%s AND ticker=%s FOR UPDATE", (user_id, ticker)
    )
    pos = await cur.fetchone()
    if pos:
        new_qty = _d(pos["quantity"]) + qty
        avg = (_d(pos["quantity"]) * _d(pos["avg_price"]) + cost) / new_qty
        await conn.execute(
            "UPDATE paper_positions SET quantity=%s, avg_price=%s, stop_price=%s WHERE user_id=%s AND ticker=%s",
            (new_qty, avg, avg * (1 - STOP_PCT), user_id, ticker),
        )
    else:
        await conn.execute(
            "INSERT INTO paper_positions(user_id, ticker, quantity, avg_price, stop_price, signal_ref)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (user_id, ticker, qty, px, px * (1 - STOP_PCT), signal_ref),
        )
    await conn.execute("UPDATE paper_accounts SET cash = cash - %s WHERE user_id=%s", (cost, user_id))
    await conn.execute(
        "INSERT INTO paper_orders(user_id, ticker, side, quantity, price, reason, signal_ref)"
        " VALUES (%s,%s,'buy',%s,%s,'manual',%s)",
        (user_id, ticker, qty, px, signal_ref),
    )
    return {"ticker": ticker, "side": "buy", "quantity": float(qty), "price": float(px)}


async def sell_all(conn, user_id: int, ticker: str, price: float, reason: str = "manual") -> dict:
    px = _d(price)
    cur = await conn.execute(
        "DELETE FROM paper_positions WHERE user_id=%s AND ticker=%s RETURNING quantity, avg_price, signal_ref",
        (user_id, ticker),
    )
    pos = await cur.fetchone()
    if not pos:
        raise PaperError(f"Aucune position fictive sur {ticker}.")
    qty = _d(pos["quantity"])
    pnl = (px - _d(pos["avg_price"])) * qty
    await conn.execute("UPDATE paper_accounts SET cash = cash + %s WHERE user_id=%s", (qty * px, user_id))
    await conn.execute(
        "INSERT INTO paper_orders(user_id, ticker, side, quantity, price, pnl, reason, signal_ref)"
        " VALUES (%s,%s,'sell',%s,%s,%s,%s,%s)",
        (user_id, ticker, qty, px, pnl, reason, pos["signal_ref"]),
    )
    return {"ticker": ticker, "side": "sell", "quantity": float(qty), "price": float(px), "pnl": float(pnl)}


async def reset(conn, user_id: int, start_cash: float) -> None:
    await conn.execute("DELETE FROM paper_positions WHERE user_id=%s", (user_id,))
    await conn.execute("DELETE FROM paper_orders WHERE user_id=%s", (user_id,))
    await conn.execute(
        "INSERT INTO paper_accounts(user_id, cash, start_cash, reset_at) VALUES (%s,%s,%s,now())"
        " ON CONFLICT (user_id) DO UPDATE SET cash=EXCLUDED.cash, start_cash=EXCLUDED.start_cash, reset_at=now()",
        (user_id, start_cash, start_cash),
    )


async def summary(conn, user_id: int, start_cash: float) -> dict:
    acct = await ensure_account(conn, user_id, start_cash)
    cur = await conn.execute(
        """SELECT p.ticker, p.quantity, p.avg_price, p.stop_price, p.signal_ref, p.opened_at,
                  pr.price AS last_price, pr.fetched_at AS price_at
           FROM paper_positions p LEFT JOIN prices pr ON pr.ticker = p.ticker
           WHERE p.user_id=%s ORDER BY p.opened_at""",
        (user_id,),
    )
    positions = []
    invested = market = Decimal(0)
    for r in await cur.fetchall():
        qty, avg = _d(r["quantity"]), _d(r["avg_price"])
        last = _d(r["last_price"]) if r["last_price"] is not None else avg
        value = qty * last
        invested += qty * avg
        market += value
        positions.append({
            "ticker": r["ticker"], "quantity": float(qty), "avg_price": float(avg),
            "stop_price": float(r["stop_price"]) if r["stop_price"] is not None else None,
            "last_price": float(last), "price_at": r["price_at"], "value": float(value),
            "pnl": float(value - qty * avg), "pnl_pct": float((last / avg - 1) * 100) if avg else 0.0,
            "signal_ref": r["signal_ref"], "opened_at": r["opened_at"],
        })
    cur = await conn.execute(
        "SELECT id, ticker, side, quantity, price, pnl, reason, signal_ref, created_at FROM paper_orders"
        " WHERE user_id=%s ORDER BY created_at DESC, id DESC LIMIT 200",
        (user_id,),
    )
    orders = [{**r, "quantity": float(r["quantity"]), "price": float(r["price"]),
               "pnl": float(r["pnl"]) if r["pnl"] is not None else None} for r in await cur.fetchall()]
    cur = await conn.execute(
        "SELECT coalesce(sum(pnl),0) AS realized, count(*) FILTER (WHERE pnl > 0) AS wins,"
        " count(*) FILTER (WHERE pnl IS NOT NULL) AS closed FROM paper_orders WHERE user_id=%s AND side='sell'",
        (user_id,),
    )
    st = await cur.fetchone()
    cash = _d(acct["cash"])
    equity = cash + market
    start = _d(acct["start_cash"])
    await conn.commit()
    return {
        "cash": float(cash), "start_cash": float(start), "equity": float(equity),
        "invested": float(invested), "market_value": float(market),
        "unrealized": float(market - invested), "realized": float(st["realized"]),
        "total_pnl": float(equity - start), "total_pct": float((equity / start - 1) * 100) if start else 0.0,
        "closed": st["closed"], "wins": st["wins"],
        "win_rate": (st["wins"] / st["closed"] * 100) if st["closed"] else None,
        "positions": positions, "orders": orders,
    }


async def apply_stops(conn) -> int:
    """Vend les positions dont le dernier cours connu est passé sous le stop (−8 %)."""
    cur = await conn.execute(
        """SELECT p.user_id, p.ticker, pr.price FROM paper_positions p JOIN prices pr ON pr.ticker = p.ticker
           WHERE p.stop_price IS NOT NULL AND pr.price <= p.stop_price"""
    )
    rows = await cur.fetchall()
    n = 0
    for r in rows:
        try:
            await sell_all(conn, r["user_id"], r["ticker"], r["price"], reason="stop")
            n += 1
            _log.info("stop déclenché : %s à %s", r["ticker"], r["price"])
        except PaperError:
            pass
    await conn.commit()
    return n
