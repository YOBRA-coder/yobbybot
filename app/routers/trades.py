import time

from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db
from ..schemas import TradeCreate, TradeUpdate
from ..security import get_current_user
from ..services.market import live_tickers

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("")
async def list_trades(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY created_at DESC LIMIT 500", (user["id"],)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("")
async def place_trade(req: TradeCreate, user=Depends(get_current_user)):
    ticker = live_tickers.get(req.pair)
    # BUG FIX: a market order with no cached ticker used to silently fill
    # at a hardcoded $100 — completely fabricated, and wildly wrong for
    # almost every real pair (e.g. filling a BTCUSDT "market" order at
    # $100 when BTC is actually tens of thousands). This was masked
    # before because the simulated ticker feed guaranteed SOME price
    # always existed; now that fetch_binance_tickers no longer fabricates
    # data (see services/market.py), a genuinely stale/cold cache must
    # reject the order rather than invent a price for it. A limit order
    # is unaffected — it prices off the number the user typed, not a
    # live read.
    if req.order_type == "limit" and req.limit_price:
        price = req.limit_price
    elif ticker:
        price = ticker["price"]
    else:
        raise HTTPException(
            503,
            f"No live price available for {req.pair} right now (market data may be temporarily "
            "unreachable) — try again shortly, or place a limit order with your own price instead.",
        )

    # BUG FIX: stop_loss/take_profit were accepted here with no direction
    # check at all — only the later PATCH ("click to adjust TP/SL") endpoint
    # validated that a BUY's stop sits below entry and target above (and
    # vice-versa for SELL). A stop_loss placed on the wrong side of entry at
    # creation time (e.g. a BUY with stop_loss ABOVE the entry price) reads
    # as already-hit the moment position_monitor_loop's next poll runs, so
    # the trade closes itself within seconds no matter what happened to
    # price — exactly "trades that don't hold for long" for entirely the
    # wrong reason. Applying the same check here that PATCH already uses
    # stops a malformed SL/TP from ever being recorded in the first place.
    if req.side == "BUY":
        if req.stop_loss is not None and req.stop_loss >= price:
            raise HTTPException(400, f"Stop loss ({req.stop_loss}) must be below entry price ({price}) for a BUY")
        if req.take_profit is not None and req.take_profit <= price:
            raise HTTPException(400, f"Take profit ({req.take_profit}) must be above entry price ({price}) for a BUY")
    else:  # SELL
        if req.stop_loss is not None and req.stop_loss <= price:
            raise HTTPException(400, f"Stop loss ({req.stop_loss}) must be above entry price ({price}) for a SELL")
        if req.take_profit is not None and req.take_profit >= price:
            raise HTTPException(400, f"Take profit ({req.take_profit}) must be below entry price ({price}) for a SELL")

    tid = f"t_{int(time.time()*1000)}"
    total = price * req.amount
    fee = total * 0.001
    cost = total + fee

    conn = get_db()
    # This is the demo/paper wallet — placing a trade actually spends from
    # it now. Previously users.balance existed but nothing ever read or
    # wrote it, so the number never moved no matter what you traded.
    if user["balance"] < cost:
        conn.close()
        raise HTTPException(400, f"Insufficient balance: need ${cost:.2f}, have ${user['balance']:.2f}")

    conn.execute(
        """INSERT INTO trades (id,user_id,pair,side,price,amount,total,fee,pnl,stop_loss,take_profit,status,reason)
        VALUES (?,?,?,?,?,?,?,?,0,?,?,'FILLED',?)""",
        (tid, user["id"], req.pair, req.side, price, req.amount, total, fee, req.stop_loss, req.take_profit,
         "Manual order placed by user"),
    )
    conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (cost, user["id"]))
    conn.commit()
    new_balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
    conn.close()
    # IMPORTANT: no WS broadcast here. This was the cause of the "trade shows
    # twice" bug — the frontend already appends the trade returned below to
    # its own state, and used to ALSO get it a second time via the NEW_TRADE
    # socket event this endpoint used to fire. Only system/bot-initiated
    # trades (see tasks.py) go out over the socket, since nothing else adds
    # those to state.
    return {
        "id": tid, "pair": req.pair, "side": req.side, "price": price,
        "amount": req.amount, "total": total, "fee": fee, "pnl": 0,
        "stop_loss": req.stop_loss, "take_profit": req.take_profit,
        "reason": "Manual order placed by user",
        "status": "FILLED", "timestamp": int(time.time() * 1000),
        "balance": new_balance,
    }


@router.patch("/{trade_id}")
async def update_trade_tp_sl(trade_id: str, req: TradeUpdate, user=Depends(get_current_user)):
    """Click-to-adjust stop-loss / take-profit on an existing trade."""
    if req.stop_loss is None and req.take_profit is None:
        raise HTTPException(400, "Provide stop_loss and/or take_profit")

    conn = get_db()
    row = conn.execute("SELECT * FROM trades WHERE id=? AND user_id=?", (trade_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Trade not found")
    trade = dict(row)
    if trade["status"] == "CLOSED":
        conn.close()
        raise HTTPException(400, "Cannot modify TP/SL on a closed trade")

    entry = trade["price"]
    side = trade["side"]
    new_sl = req.stop_loss if req.stop_loss is not None else trade["stop_loss"]
    new_tp = req.take_profit if req.take_profit is not None else trade["take_profit"]

    # Direction validation: for a BUY, stop-loss must sit below entry and
    # take-profit above; for a SELL, the reverse. Prevents a fat-fingered
    # SL/TP that would trigger immediately or never.
    if side == "BUY":
        if new_sl is not None and new_sl >= entry:
            conn.close()
            raise HTTPException(400, f"Stop loss ({new_sl}) must be below entry price ({entry}) for a BUY")
        if new_tp is not None and new_tp <= entry:
            conn.close()
            raise HTTPException(400, f"Take profit ({new_tp}) must be above entry price ({entry}) for a BUY")
    else:  # SELL
        if new_sl is not None and new_sl <= entry:
            conn.close()
            raise HTTPException(400, f"Stop loss ({new_sl}) must be above entry price ({entry}) for a SELL")
        if new_tp is not None and new_tp >= entry:
            conn.close()
            raise HTTPException(400, f"Take profit ({new_tp}) must be below entry price ({entry}) for a SELL")

    conn.execute("UPDATE trades SET stop_loss=?, take_profit=? WHERE id=?", (new_sl, new_tp, trade_id))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())
    conn.close()
    return updated


@router.post("/{trade_id}/close")
async def close_trade(trade_id: str, user=Depends(get_current_user)):
    """
    Manually close an open trade at the current market price and realize
    its P&L. Previously the frontend's "Close Trade" button only flipped
    local React state — nothing was persisted, so a refresh (or another
    device) would show the trade as still open. This makes it real, and
    also credits the demo balance back (principal + realized P&L) — see
    the matching deduction in place_trade above.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM trades WHERE id=? AND user_id=?", (trade_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Trade not found")
    trade = dict(row)
    if trade["status"] == "CLOSED":
        conn.close()
        raise HTTPException(400, "Trade is already closed")

    ticker = live_tickers.get(trade["pair"])
    # BUG FIX: closing with no cached ticker used to silently settle at
    # the trade's own ENTRY price — always reporting exactly 0 P&L, which
    # isn't "unknown P&L", it's a specific wrong number that happens to
    # look like a plausible (if boring) real result. Reject instead so a
    # real close always reflects a real price — position_monitor_loop's
    # own SL/TP checks already skip a trade the same way when its
    # ticker's missing (see tasks.py), so this stays consistent with how
    # the rest of the app treats "no real price available right now".
    if not ticker:
        conn.close()
        raise HTTPException(
            503,
            f"No live price available for {trade['pair']} right now (market data may be temporarily "
            "unreachable) — try closing again in a moment.",
        )
    close_price = ticker["price"]
    pnl = ((close_price - trade["price"]) if trade["side"] == "BUY" else (trade["price"] - close_price)) * trade["amount"]
    pnl -= trade["fee"]

    conn.execute(
        "UPDATE trades SET status='CLOSED', pnl=?, closed_at=? WHERE id=?",
        (pnl, int(time.time()), trade_id),
    )
    if not trade["bot_id"] and not trade["live"]:
        # Manual/copied demo trades settle against the user's own balance.
        # Bot-owned trades are handled by the bot loop itself (DEMO bots
        # use the bot's own capital allocation; LIVE bots touch Binance,
        # never this app-internal balance).
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (trade["total"] + pnl, user["id"]))
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())
    new_balance = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()["balance"]
    conn.close()
    return {**updated, "balance": new_balance}
