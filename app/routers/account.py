import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..crypto import decrypt
from ..database import get_db
from ..security import get_current_user
from ..services.binance_client import get_equity_usdt
from ..services.market import live_prices

router = APIRouter(prefix="/account", tags=["account"])

# Demo-funds top-up ("faucet"). Exists because demo balance has no other way
# to go up besides winning trades — someone who blows through their starting
# $10,000 (or just wants more capital so a bot's position sizing isn't
# starved) had no way to keep testing. Capped + cooldown-gated so it's a
# genuine unstick mechanism rather than infinite free money that makes the
# demo P&L numbers meaningless.
DEMO_DEPOSIT_AMOUNT = 5000.0
DEMO_DEPOSIT_COOLDOWN_SECONDS = 60 * 60  # 1 hour
DEMO_BALANCE_CAP = 250_000.0


@router.get("/summary")
async def account_summary(user=Depends(get_current_user)):
    """
    "shows balance and equity and everything" — this is the single endpoint
    the dashboard/wallet UI calls. Demo balance is always available (it's
    just the users.balance column). Live balance/equity is only included
    if Binance is connected, and any failure to reach Binance is reported
    as a field on the response rather than raising, so a temporarily
    unreachable exchange doesn't take down the whole account view.
    """
    now = int(time.time())
    next_deposit_at = (user["last_demo_deposit_at"] or 0) + DEMO_DEPOSIT_COOLDOWN_SECONDS
    result = {
        "demo_balance": user["balance"],
        "broker_connected": bool(user["binance_api_key_enc"]),
        "testnet": bool(user["binance_testnet"]),
        "live": None,
        "live_error": None,
        "deposit_available": user["balance"] < DEMO_BALANCE_CAP and now >= next_deposit_at,
        "deposit_available_at": next_deposit_at,
        "deposit_amount": DEMO_DEPOSIT_AMOUNT,
    }
    if user["binance_api_key_enc"] and user["binance_api_secret_enc"]:
        api_key = decrypt(user["binance_api_key_enc"])
        api_secret = decrypt(user["binance_api_secret_enc"])
        if not api_key or not api_secret:
            result["live_error"] = "Stored credentials could not be decrypted — please reconnect Binance in Settings"
        else:
            try:
                async with aiohttp.ClientSession() as session:
                    result["live"] = await get_equity_usdt(
                        session, api_key, api_secret, bool(user["binance_testnet"]), live_prices
                    )
            except Exception as e:
                result["live_error"] = f"Could not reach Binance: {e}"
    return result


@router.post("/deposit")
async def deposit_demo_funds(user=Depends(get_current_user)):
    """
    Top up DEMO balance only — never touches a LIVE/Binance account, there's
    no such thing as free real money. Gated by a cooldown and a cap so a
    user whose wallet balance is too low to fund a new bot (or top up an
    existing one) has a real way to get unstuck without turning the demo
    balance into an infinite number. Note this tops up the wallet, not a
    bot directly — a bot's own capital is reserved from this balance at
    creation and adjusted via PATCH /bots/{id} (see routers/bots.py).
    """
    conn = get_db()
    row = conn.execute(
        "SELECT balance, last_demo_deposit_at FROM users WHERE id=?", (user["id"],)
    ).fetchone()
    now = int(time.time())
    next_available_at = (row["last_demo_deposit_at"] or 0) + DEMO_DEPOSIT_COOLDOWN_SECONDS
    if now < next_available_at:
        conn.close()
        raise HTTPException(429, f"Next demo deposit available in {next_available_at - now}s")
    if row["balance"] >= DEMO_BALANCE_CAP:
        conn.close()
        raise HTTPException(400, f"Demo balance is already at the ${DEMO_BALANCE_CAP:,.0f} cap")

    new_balance = min(row["balance"] + DEMO_DEPOSIT_AMOUNT, DEMO_BALANCE_CAP)
    conn.execute(
        "UPDATE users SET balance=?, last_demo_deposit_at=? WHERE id=?",
        (new_balance, now, user["id"]),
    )
    conn.commit()
    conn.close()
    return {
        "ok": True, "balance": new_balance,
        "deposited": new_balance - row["balance"],
        "deposit_available_at": now + DEMO_DEPOSIT_COOLDOWN_SECONDS,
    }
