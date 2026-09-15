"""
Minimal signed Binance client — just enough for account balance and market
order placement. Spot trading only.

SAFETY DEFAULT: testnet=True talks to https://testnet.binance.vision,
Binance's own sandbox with fake funds — not real money. A user must
explicitly opt out of testnet (in Settings) before any real order is
placed on mainnet. This default is intentional and should not be flipped
without the user's explicit action.
"""
import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp


MAINNET = "https://api.binance.com"
TESTNET = "https://testnet.binance.vision"


def _sign(params: dict, secret: str) -> str:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


async def get_account(session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool = True) -> dict:
    """Real account balances + can-trade status. Raises on failure — callers
    should catch and surface a clear "connection failed" message rather
    than silently falling back to anything, since this is used to confirm
    real credentials work before anyone flips a bot to LIVE."""
    base = TESTNET if testnet else MAINNET
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/account?{query}"
    async with session.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance error {r.status}"))
        return data


async def get_equity_usdt(session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool, live_prices: dict) -> dict:
    """Account balances plus a rough total-equity-in-USDT estimate, using
    already-fetched live ticker prices rather than one API call per asset."""
    account = await get_account(session, api_key, api_secret, testnet)
    balances = [b for b in account["balances"] if float(b["free"]) > 0 or float(b["locked"]) > 0]
    total_usdt = 0.0
    enriched = []
    for b in balances:
        asset = b["asset"]
        qty = float(b["free"]) + float(b["locked"])
        if asset in ("USDT", "BUSD", "USDC"):
            usdt_value = qty
        else:
            price = live_prices.get(f"{asset}USDT")
            usdt_value = qty * price if price else 0.0
        total_usdt += usdt_value
        enriched.append({"asset": asset, "free": float(b["free"]), "locked": float(b["locked"]), "usdt_value": usdt_value})
    enriched.sort(key=lambda x: x["usdt_value"], reverse=True)
    return {"balances": enriched, "total_equity_usdt": total_usdt, "can_trade": account.get("canTrade", False)}


async def place_market_order(
    session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool,
    symbol: str, side: str, quantity: float,
) -> dict:
    """Places a real market order. Only ever called for bots explicitly
    switched to LIVE mode by the user, after a successful connection test."""
    base = TESTNET if testnet else MAINNET
    params = {
        "symbol": symbol, "side": side.upper(), "type": "MARKET",
        "quantity": quantity, "timestamp": int(time.time() * 1000), "recvWindow": 5000,
    }
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/order?{query}"
    async with session.post(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance order error {r.status}"))
        return data


def extract_fill(order_response: dict) -> Optional[dict]:
    """Pulls average fill price and executed quantity out of a Binance
    order response — market orders can fill across multiple price levels."""
    fills = order_response.get("fills", [])
    executed_qty = float(order_response.get("executedQty", 0))
    if not fills or executed_qty <= 0:
        return None
    total_cost = sum(float(f["price"]) * float(f["qty"]) for f in fills)
    avg_price = total_cost / executed_qty
    total_commission = sum(float(f["commission"]) for f in fills)
    return {"avg_price": avg_price, "executed_qty": executed_qty, "commission": total_commission}
