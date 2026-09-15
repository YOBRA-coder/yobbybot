import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..crypto import encrypt
from ..database import get_db
from ..schemas import BrokerSettings, RiskSettings, TelegramSettings
from ..security import get_current_user
from ..services.binance_client import get_account
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
            "🤖 <b>NexusAI Connected!</b>\n\nYour trading signals will appear here.\n\n⚡ NexusAI Trading Platform",
        )
    if not ok:
        raise HTTPException(502, "Telegram connection failed")
    return {"ok": True}


@router.patch("/risk")
async def update_risk(req: RiskSettings, user=Depends(get_current_user)):
    """How much of a bot's capital it risks per position — previously
    hardcoded to 8% for every bot everywhere with no way to change it."""
    conn = get_db()
    conn.execute("UPDATE users SET risk_per_trade=? WHERE id=?", (req.risk_per_trade, user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True, "risk_per_trade": req.risk_per_trade}


@router.post("/broker/test")
async def test_broker(req: BrokerSettings):
    """
    Validate Binance API keys BEFORE saving them — never persist credentials
    that haven't been proven to work. Returns real account balances so the
    person can see exactly what they're connecting before committing to it.
    Defaults to testnet (Binance's sandbox with fake funds); real mainnet
    trading requires explicitly setting testnet=false.
    """
    async with aiohttp.ClientSession() as session:
        try:
            account = await get_account(session, req.api_key, req.api_secret, req.testnet)
        except Exception as e:
            raise HTTPException(400, f"Could not connect to Binance: {e}")
    balances = [b for b in account["balances"] if float(b["free"]) > 0 or float(b["locked"]) > 0]
    return {
        "ok": True, "can_trade": account.get("canTrade", False),
        "testnet": req.testnet, "balances": balances,
    }


@router.patch("/broker")
async def connect_broker(req: BrokerSettings, user=Depends(get_current_user)):
    """Save Binance API credentials — only after a successful /broker/test,
    enforced client-side; also re-validated here so a stale/bad key can
    never be saved even if the frontend skips the test step."""
    async with aiohttp.ClientSession() as session:
        try:
            await get_account(session, req.api_key, req.api_secret, req.testnet)
        except Exception as e:
            raise HTTPException(400, f"Could not connect to Binance: {e}")

    conn = get_db()
    conn.execute(
        "UPDATE users SET binance_api_key_enc=?, binance_api_secret_enc=?, binance_testnet=? WHERE id=?",
        (encrypt(req.api_key), encrypt(req.api_secret), int(req.testnet), user["id"]),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "testnet": req.testnet}


@router.delete("/broker")
async def disconnect_broker(user=Depends(get_current_user)):
    """Disconnects Binance. Any bot currently in LIVE mode is force-stopped
    first — a LIVE bot with no working credentials would just fail silently
    on its next cycle otherwise."""
    conn = get_db()
    conn.execute(
        "UPDATE bots SET status='STOPPED' WHERE user_id=? AND mode='LIVE'", (user["id"],)
    )
    conn.execute(
        "UPDATE users SET binance_api_key_enc=NULL, binance_api_secret_enc=NULL WHERE id=?",
        (user["id"],),
    )
    conn.commit()
    conn.close()
    return {"ok": True}
