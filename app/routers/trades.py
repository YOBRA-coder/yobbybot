import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..crypto import decrypt
from ..database import get_db
from ..schemas import TradeCreate, TradeUpdate
from ..security import get_current_user
from ..services.binance_client import (
    cancel_order_list, extract_fill, place_market_order, place_oco_exit,
)
from ..services.market import live_tickers


def _broker_creds(user):
    """
    Decrypts the user's stored Binance credentials, or raises a clear 4xx
    explaining exactly what to do about it.

    Every real-money path in this router goes through here rather than
    each doing its own half-check, so "Binance isn't connected" can never
    quietly degrade into "filled on the paper wallet instead" — placing a
    real order and placing a simulated one must never be confusable.
    """
    if not (user["binance_api_key_enc"] and user["binance_api_secret_enc"]):
        raise HTTPException(400, "Binance is not connected — add your API keys in Settings first")
    api_key = decrypt(user["binance_api_key_enc"])
    api_secret = decrypt(user["binance_api_secret_enc"])
    if not api_key or not api_secret:
        raise HTTPException(400, "Stored Binance credentials could not be decrypted — please reconnect in Settings")
    return api_key, api_secret, bool(user["binance_testnet"])

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
    is_live = req.venue == "BINANCE"
    exchange_order_list_id = None
    exchange_exit_error = None

    # "when i place a trade add option to execute in binance" — a BINANCE
    # order is sent to the exchange BEFORE anything is written to the
    # database, and the row records the actual fill Binance reported, not
    # the price we quoted off the ticker cache. A market order routinely
    # fills a few ticks away from the last trade price, and a partial fill
    # changes the quantity too; recording our own estimate would leave the
    # app's P&L permanently disagreeing with the exchange's.
    #
    # If the exchange rejects the order, nothing is recorded at all — a
    # trade that never happened must not appear in history.
    if is_live:
        if req.order_type == "limit":
            raise HTTPException(
                400,
                "Live Binance orders are market-only right now — place this as a market order, "
                "or use the demo venue for limit orders.",
            )
        api_key, api_secret, testnet = _broker_creds(user)
        try:
            async with aiohttp.ClientSession() as session:
                order = await place_market_order(
                    session, api_key, api_secret, testnet, req.pair, req.side, req.amount
                )
        except Exception as e:
            raise HTTPException(502, f"Binance rejected the order: {e}")
        fill = extract_fill(order)
        if not fill:
            raise HTTPException(502, "Binance accepted the order but reported no fill — check your exchange order history before retrying")
        price = fill["avg_price"]
        amount = fill["executed_qty"]
        fee = fill["commission"] or price * amount * 0.001
        total = price * amount

        # Put the exits on the exchange itself, so they survive this app
        # being down. Only possible when BOTH levels are given — Binance's
        # OCO needs a take-profit leg and a stop leg. A failure here is NOT
        # fatal (the position is already open and must be recorded), but it
        # is recorded and surfaced rather than silently leaving the trade
        # protected only by the in-app monitor.
        if req.stop_loss and req.take_profit:
            try:
                async with aiohttp.ClientSession() as session:
                    oco = await place_oco_exit(
                        session, api_key, api_secret, testnet,
                        req.pair, req.side, amount, req.take_profit, req.stop_loss,
                    )
                exchange_order_list_id = str(oco.get("orderListId")) if oco.get("orderListId") is not None else None
            except Exception as e:
                exchange_exit_error = str(e)
        elif req.stop_loss or req.take_profit:
            exchange_exit_error = (
                "Binance needs both a stop loss AND a take profit to hold an exit order. "
                "Only one was set, so this level is monitored by the app instead."
            )
    else:
        amount = req.amount
        total = price * amount
        fee = total * 0.001

    cost = total + fee

    conn = get_db()
    # Demo/paper wallet. A LIVE order spends real funds on the exchange and
    # must NOT also be deducted from the paper balance — that would
    # double-count the same position and make the demo number drift for
    # reasons that have nothing to do with demo trading.
    if not is_live and user["balance"] < cost:
        conn.close()
        raise HTTPException(400, f"Insufficient balance: need ${cost:.2f}, have ${user['balance']:.2f}")

    reason = "Manual order executed on Binance" if is_live else "Manual order placed by user"
    conn.execute(
        """INSERT INTO trades (id,user_id,pair,side,price,amount,total,fee,pnl,stop_loss,take_profit,live,status,reason,
                               exchange_order_list_id,exchange_exit_error)
        VALUES (?,?,?,?,?,?,?,?,0,?,?,?,'FILLED',?,?,?)""",
        (tid, user["id"], req.pair, req.side, price, amount, total, fee, req.stop_loss, req.take_profit,
         int(is_live), reason, exchange_order_list_id, exchange_exit_error),
    )
    if not is_live:
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
        "amount": amount, "total": total, "fee": fee, "pnl": 0,
        "stop_loss": req.stop_loss, "take_profit": req.take_profit,
        "reason": reason, "live": int(is_live),
        "status": "FILLED", "timestamp": int(time.time() * 1000),
        "balance": new_balance,
        # Where the exits actually live: "EXCHANGE" means Binance is holding
        # a resting OCO and will enforce it even if this app is offline;
        # "APP" means only position_monitor_loop is watching.
        "sl_tp_venue": "EXCHANGE" if exchange_order_list_id else "APP",
        "exchange_order_list_id": exchange_order_list_id,
        "exchange_exit_error": exchange_exit_error,
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

    # For a LIVE trade, the levels aren't just a database row — there may be
    # a real OCO resting on Binance enforcing the OLD ones. Editing without
    # replacing it would leave the exchange protecting the position at
    # levels the user can no longer see anywhere in the UI. Cancel, then
    # re-place at the new levels.
    exchange_order_list_id = trade["exchange_order_list_id"]
    exchange_exit_error = None
    if trade["live"]:
        api_key, api_secret, testnet = _broker_creds(user)
        async with aiohttp.ClientSession() as session:
            if exchange_order_list_id:
                try:
                    await cancel_order_list(
                        session, api_key, api_secret, testnet, trade["pair"], int(exchange_order_list_id)
                    )
                except Exception as e:
                    # Refuse rather than proceed: if the old OCO is still
                    # live and we place a second one, the account ends up
                    # with two resting exits for one position, and whichever
                    # fires second sells a position that no longer exists.
                    conn.close()
                    raise HTTPException(
                        502,
                        f"Could not cancel the existing Binance exit order, so TP/SL was not changed: {e}",
                    )
                exchange_order_list_id = None
            if new_sl and new_tp:
                try:
                    oco = await place_oco_exit(
                        session, api_key, api_secret, testnet,
                        trade["pair"], trade["side"], trade["amount"], new_tp, new_sl,
                    )
                    exchange_order_list_id = str(oco.get("orderListId")) if oco.get("orderListId") is not None else None
                except Exception as e:
                    exchange_exit_error = str(e)
            else:
                exchange_exit_error = (
                    "Binance needs both a stop loss AND a take profit to hold an exit order — "
                    "this level is monitored by the app instead."
                )

    conn.execute(
        "UPDATE trades SET stop_loss=?, take_profit=?, exchange_order_list_id=?, exchange_exit_error=? WHERE id=?",
        (new_sl, new_tp, exchange_order_list_id, exchange_exit_error, trade_id),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())
    conn.close()
    # Tells the client whether Binance is actually holding these levels or
    # whether only this app is watching them — so the UI can say so plainly
    # instead of implying the exchange has the order.
    updated["sl_tp_venue"] = "EXCHANGE" if exchange_order_list_id else "APP"
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

    # BUG FIX: a LIVE trade closed from the UI only ever updated this row.
    # The actual position stayed open on Binance — the app said "closed",
    # the exchange disagreed, and the next SL/TP sweep or a manual exchange
    # close would settle it a second time. Closing a live trade now sends
    # the opposite market order first and settles at the real fill; if the
    # exchange rejects it, the row is left open rather than being marked
    # closed against a position that still exists.
    if trade["live"]:
        api_key, api_secret, testnet = _broker_creds(user)
        closing_side = "SELL" if trade["side"] == "BUY" else "BUY"
        try:
            async with aiohttp.ClientSession() as session:
                # Cancel the resting OCO FIRST. Skipping this leaves a stop
                # and a take-profit sitting on the exchange for a position
                # that's about to stop existing — whichever one later
                # triggers would open an unintended new position in the
                # opposite direction.
                if trade["exchange_order_list_id"]:
                    try:
                        await cancel_order_list(
                            session, api_key, api_secret, testnet,
                            trade["pair"], int(trade["exchange_order_list_id"]),
                        )
                    except Exception as e:
                        conn.close()
                        raise HTTPException(
                            502,
                            f"Could not cancel the Binance exit order, so the position was NOT closed "
                            f"(closing it with the stop still resting would leave a stray order): {e}",
                        )
                order = await place_market_order(
                    session, api_key, api_secret, testnet, trade["pair"], closing_side, trade["amount"]
                )
        except HTTPException:
            raise
        except Exception as e:
            conn.close()
            raise HTTPException(502, f"Binance rejected the closing order — position is still open: {e}")
        fill = extract_fill(order)
        if fill:
            close_price = fill["avg_price"]
        conn.execute("UPDATE trades SET exchange_order_list_id=NULL WHERE id=?", (trade_id,))

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
