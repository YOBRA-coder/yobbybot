import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db
from ..schemas import CopySignalRequest
from ..security import get_current_user
from ..services.ai_signals import get_ai_signal
from ..services.indicators import calc_macd, calc_rsi, generate_signal_from_ta
from ..services.market import fetch_binance_tickers, fetch_klines
from ..services.telegram import format_signal_message, send_telegram

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("")
async def list_signals(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM signals WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("/generate")
async def generate_signals(user=Depends(get_current_user)):
    """User-triggered signal generation (the 'Generate' button). Results are
    returned directly in the response — NOT also broadcast over the socket,
    since the caller already adds them to local state from this response.
    (Auto-generated signals, added in the signal-engine phase, are the ones
    that go out over the socket, since nobody made a matching REST call.)"""
    # "bring accurate recent signals and verify market every time" — every
    # call fetches a brand-new ticker snapshot and fresh candles from
    # Binance (nothing here is cached/reused between calls), and reads
    # 15m candles rather than 1h so "recent" actually means the last
    # ~24h at 15-minute resolution instead of the last ~2.5 days at
    # hourly resolution.
    results = []
    async with aiohttp.ClientSession() as session:
        tickers = await fetch_binance_tickers(session)
        # "ensure i get accurate data for accurate results and
        # consistency" — fetch_binance_tickers no longer fabricates data
        # (see services/market.py), so a real outage now means an empty
        # list here. Fail loudly rather than silently returning [] and
        # looking like "no signals right now" when it's actually "no real
        # market data right now" — same reasoning as the backtest 503.
        if not tickers:
            raise HTTPException(
                503,
                "Couldn't fetch live market data to generate signals right now "
                "(Binance may be temporarily unreachable) — try again shortly.",
            )
        conn = get_db()
        for ticker in tickers[:8]:
            candles = await fetch_klines(session, ticker["symbol"], "15m", 96)
            ta = generate_signal_from_ta(candles, ticker)
            sid = f"sig_{int(time.time()*1000)}_{ticker['symbol']}"
            conn.execute(
                """INSERT INTO signals
                (id,user_id,pair,type,strength,price,target_price,stop_loss,confidence,reason,ai_generated,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,0,'ACTIVE')""",
                (sid, user["id"], ticker["symbol"], ta["type"], ta["strength"],
                 ticker["price"], ta["targetPrice"], ta["stopLoss"], ta["confidence"], ta["reason"]),
            )
            results.append({
                "id": sid, "pair": ticker["symbol"], **ta,
                "price": ticker["price"], "aiGenerated": False,
                "timestamp": int(time.time() * 1000), "status": "ACTIVE",
            })

        # AI-backed signals for the top pairs (skipped silently if no API key configured)
        for ticker in tickers[:2]:
            candles = await fetch_klines(session, ticker["symbol"], "15m", 96)
            closes = [c["close"] for c in candles]
            rsi = calc_rsi(closes)
            macd = calc_macd(closes)
            ai = await get_ai_signal(session, ticker["symbol"], ticker, rsi, macd)
            if ai:
                sid = f"aisig_{int(time.time()*1000)}_{ticker['symbol']}"
                target_price = float(ai.get("targetPrice", ticker["price"] * 1.03))
                stop_loss = float(ai.get("stopLoss", ticker["price"] * 0.97))
                confidence = float(ai.get("confidence", 75))
                reason = ai.get("reason", "AI analysis")
                conn.execute(
                    """INSERT INTO signals
                    (id,user_id,pair,type,strength,price,target_price,stop_loss,confidence,reason,ai_generated,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,'ACTIVE')""",
                    (sid, user["id"], ticker["symbol"], ai["type"], 5, ticker["price"],
                     target_price, stop_loss, confidence, reason),
                )
                results.insert(0, {
                    "id": sid, "pair": ticker["symbol"], "type": ai["type"], "confidence": confidence,
                    "targetPrice": target_price, "stopLoss": stop_loss, "reason": reason,
                    "price": ticker["price"], "strength": 5, "aiGenerated": True,
                    "timestamp": int(time.time() * 1000), "status": "ACTIVE",
                })
        conn.commit()
        conn.close()
    return results


@router.post("/copy")
async def copy_signal(req: CopySignalRequest, user=Depends(get_current_user)):
    """
    Copy a signal into a trade. Previously this endpoint took an untyped
    dict, tried to use it directly as a SQL parameter, and had a debug
    print that concatenated a string with a dict — it could not have
    worked. Fixed to take a real request body, validate the signal exists
    and belongs to the caller, and use the signal's own price/side.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM signals WHERE id=? AND user_id=?", (req.signal_id, user["id"])
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Signal not found")
    data = dict(row)
    if data["type"] not in ("BUY", "SELL"):
        conn.close()
        raise HTTPException(400, "Cannot copy a HOLD signal into a trade")

    total = data["price"] * req.amount
    fee = total * 0.001
    trade_id = f"ct_{int(time.time()*1000)}"
    conn.execute(
        """INSERT INTO trades (id,user_id,bot_id,signal_id,pair,side,price,amount,total,fee,pnl,stop_loss,take_profit,status,reason)
        VALUES (?,?,NULL,?,?,?,?,?,?,?,0,?,?,'FILLED',?)""",
        (trade_id, user["id"], data["id"], data["pair"], data["type"], data["price"],
         req.amount, total, fee, data["stop_loss"], data["target_price"],
         data["reason"] or "Copied from signal"),
    )
    conn.commit()
    conn.close()
    # No socket broadcast here either — the caller adds this trade to state
    # from the response, same as any other user-initiated trade.
    return {
        "id": trade_id, "pair": data["pair"], "side": data["type"], "price": data["price"],
        "amount": req.amount, "total": total, "fee": fee, "pnl": 0,
        "stop_loss": data["stop_loss"], "take_profit": data["target_price"],
        "reason": data["reason"] or "Copied from signal",
        "status": "FILLED", "timestamp": int(time.time() * 1000),
    }


@router.post("/{signal_id}/telegram")
async def send_signal_to_telegram(signal_id: str, user=Depends(get_current_user)):
    if not user["telegram_token"] or not user["telegram_chat_id"]:
        raise HTTPException(400, "Telegram not configured. Set in Settings.")
    conn = get_db()
    sig = conn.execute("SELECT * FROM signals WHERE id=? AND user_id=?", (signal_id, user["id"])).fetchone()
    conn.close()
    if not sig:
        raise HTTPException(404, "Signal not found")
    msg = format_signal_message(dict(sig))
    async with aiohttp.ClientSession() as session:
        ok = await send_telegram(session, user["telegram_token"], user["telegram_chat_id"], msg)
    if not ok:
        raise HTTPException(502, "Failed to send Telegram message")
    return {"ok": True}
