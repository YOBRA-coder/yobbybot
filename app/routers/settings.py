import asyncio
import time

import aiohttp
from fastapi import APIRouter, Body, Depends, HTTPException

from ..database import get_db
from ..schemas import TelegramSettings
from ..security import get_current_user
from ..services import engine_config
from ..services.telegram import send_telegram

router = APIRouter(prefix="/settings", tags=["settings"])


@router.patch("/telegram")
async def update_telegram(req: TelegramSettings, user=Depends(get_current_user)):
    conn = get_db()
    conn.execute(
        "UPDATE users SET telegram_token=?, telegram_chat_id=? WHERE id=?",
        (req.telegram_token, req.telegram_chat_id, user["id"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True}


@router.post("/telegram/test")
async def test_telegram(user=Depends(get_current_user)):
    if not user["telegram_token"] or not user["telegram_chat_id"]:
        raise HTTPException(400, "Telegram not configured")
    async with aiohttp.ClientSession() as session:
        ok = await send_telegram(
            session, user["telegram_token"], user["telegram_chat_id"],
            "✅ <b>CryptoSignal connected</b>\n\nNew signals and their win/loss results will arrive here.",
        )
    if not ok:
        raise HTTPException(502, "Telegram connection failed")
    return {"ok": True}


@router.put("/engine")
async def update_engine(payload: dict = Body(...), user=Depends(get_current_user)):
    """Save engine settings (sensitivity, targets, gates, scanner defaults). The background scanner that
    fires Telegram alerts reads these, so changing them changes which signals are fired."""
    try:
        cfg = await asyncio.to_thread(engine_config.set_, payload)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": cfg}


@router.put("/engine/profile/{symbol}")
async def save_engine_profile(symbol: str, payload: dict = Body(...), user=Depends(get_current_user)):
    """Store the tuned settings for ONE pair (the backtest optimizer's result). They then apply to that pair's
    chart, scanner rows, entry-timeframe scan and Telegram alerts. An empty body clears the profile."""
    sym = symbol.upper()
    if not sym.isalnum() or len(sym) > 20:
        raise HTTPException(400, "bad symbol")
    try:
        prof = engine_config.clean_profiles({sym: {**payload, "tuned_at": int(time.time())}}).get(sym, {}) if payload else {}
        cfg = await asyncio.to_thread(engine_config.set_profile, sym, prof)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "profile": engine_config.profile_for(sym, cfg), "config": cfg}
