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
import math
import time
from decimal import Decimal
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
    symbol: str, side: str, quantity: float, ref_price: Optional[float] = None,
) -> dict:
    """Places a real market order. Only ever called for bots explicitly
    switched to LIVE mode by the user, after a successful connection test."""
    base = TESTNET if testnet else MAINNET
    # BUG FIX: the raw quantity was sent straight through. Position sizing
    # produces values like 0.000123456; Binance rejects anything that isn't
    # an exact multiple of the symbol's stepSize with an opaque "Filter
    # failure: LOT_SIZE", which surfaced to the user as an unexplained
    # failed order. Round to the symbol's own rules first.
    norm = await normalize_order(session, symbol, testnet, quantity, ref_price=ref_price)
    params = {
        "symbol": symbol, "side": side.upper(), "type": "MARKET",
        "quantity": norm["quantity_str"], "timestamp": int(time.time() * 1000), "recvWindow": 5000,
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


# ── Symbol filters (lot size / tick size / min notional) ─────────────────
# Binance rejects any order whose quantity isn't an exact multiple of the
# symbol's stepSize, whose price isn't a multiple of tickSize, or whose
# notional is below minNotional.
#
# This matters a lot here: position sizing in tasks.py does
# `round(risk_amount / price, 6)`, which produces values like 0.000123 —
# fine for BTCUSDT (stepSize 0.00001) but rejected outright on symbols
# with a coarser step, and the resulting "Filter failure: LOT_SIZE" error
# surfaced to the user as an opaque failed order with no explanation. Every
# real order now gets rounded to the symbol's own rules first.
_filters_cache: dict = {}
_filters_fetched_at: dict = {}
FILTERS_TTL_SECONDS = 6 * 3600


async def get_symbol_filters(session: aiohttp.ClientSession, symbol: str, testnet: bool = True) -> dict:
    """Returns {stepSize, tickSize, minQty, minNotional} for a symbol.

    Cached for 6h — exchange filters change rarely, and this is called on
    every single live order, so hitting /exchangeInfo each time would add
    a network round-trip to the latency-sensitive part of order placement.
    """
    key = (symbol, testnet)
    now = time.time()
    if key in _filters_cache and now - _filters_fetched_at.get(key, 0) < FILTERS_TTL_SECONDS:
        return _filters_cache[key]

    base = TESTNET if testnet else MAINNET
    url = f"{base}/api/v3/exchangeInfo?symbol={symbol}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance exchangeInfo error {r.status}"))
    symbols = data.get("symbols") or []
    if not symbols:
        raise ValueError(f"{symbol} is not a tradable symbol on this Binance endpoint")

    filters = {f["filterType"]: f for f in symbols[0].get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    price_f = filters.get("PRICE_FILTER", {})
    notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    result = {
        "stepSize": float(lot.get("stepSize", 0) or 0),
        "minQty": float(lot.get("minQty", 0) or 0),
        "tickSize": float(price_f.get("tickSize", 0) or 0),
        "minNotional": float(notional.get("minNotional", 0) or 0),
    }
    _filters_cache[key] = result
    _filters_fetched_at[key] = now
    return result


def _round_step(value: float, step: float) -> float:
    """Rounds DOWN to a multiple of step, then trims binary float noise.

    Rounding down rather than to-nearest is deliberate for quantities: a
    quantity rounded up can exceed the free balance and get the whole order
    rejected for insufficient funds.
    """
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-9)
    result = steps * step
    # step is a decimal like 0.00001; derive its precision so the value we
    # send is "0.00012" and not "0.00011999999999999999", which Binance
    # rejects as a filter failure even though it's numerically correct.
    decimals = max(0, -Decimal(str(step)).as_tuple().exponent)
    return float(round(result, decimals))


def _fmt(value: float, step: float) -> str:
    """Formats a number for the query string without scientific notation.

    float 0.00001 stringifies as '1e-05', which Binance's parser rejects.
    """
    decimals = max(0, -Decimal(str(step)).as_tuple().exponent) if step > 0 else 8
    return f"{value:.{decimals}f}"


async def normalize_order(
    session: aiohttp.ClientSession, symbol: str, testnet: bool,
    quantity: float, prices: Optional[dict] = None, ref_price: Optional[float] = None,
) -> dict:
    """Rounds a quantity (and any prices) to the symbol's filters and
    validates minimums. Raises ValueError with a message meant to be shown
    to the user directly."""
    f = await get_symbol_filters(session, symbol, testnet)
    qty = _round_step(quantity, f["stepSize"])
    if f["minQty"] and qty < f["minQty"]:
        raise ValueError(
            f"Order size {quantity:g} is below {symbol}'s minimum of {f['minQty']:g} — increase the amount"
        )
    if qty <= 0:
        raise ValueError(f"Order size {quantity:g} rounds to zero at {symbol}'s step size of {f['stepSize']:g}")
    if ref_price and f["minNotional"] and qty * ref_price < f["minNotional"]:
        raise ValueError(
            f"Order value ${qty * ref_price:.2f} is below {symbol}'s ${f['minNotional']:.2f} minimum — increase the amount"
        )
    out = {"quantity": qty, "quantity_str": _fmt(qty, f["stepSize"]), "filters": f}
    for name, p in (prices or {}).items():
        rounded = _round_step(p, f["tickSize"])
        out[name] = rounded
        out[f"{name}_str"] = _fmt(rounded, f["tickSize"])
    return out


# ── Exchange-native exit orders (OCO) ────────────────────────────────────
# Until now, stop-loss and take-profit for a LIVE position existed only
# inside this app: position_monitor_loop watched the price and fired a
# market order when a level was touched. That means no protection at all
# whenever the backend is stopped, restarting, or has lost its market feed
# — which is precisely when you most need a stop to be sitting safely at
# the exchange.
#
# An OCO ("one-cancels-other") puts both exits on Binance itself: a limit
# order at the take-profit and a stop-limit at the stop-loss, where filling
# either automatically cancels the other. The exchange enforces it whether
# or not this app is running.
async def place_oco_exit(
    session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool,
    symbol: str, position_side: str, quantity: float,
    take_profit: float, stop_loss: float, stop_limit_offset: float = 0.002,
) -> dict:
    """Places the OCO that closes an open position.

    position_side is the side of the POSITION being protected — the exit
    orders are the opposite side (a long is closed by selling).
    """
    exit_side = "SELL" if position_side.upper() == "BUY" else "BUY"
    norm = await normalize_order(
        session, symbol, testnet, quantity,
        prices={"tp": take_profit, "sl": stop_loss},
    )
    # The stop-limit's limit price is set slightly WORSE than the trigger so
    # the order still fills if price gaps through the stop. A stop-limit
    # priced exactly at the trigger frequently doesn't fill in a fast move,
    # which would leave the position open with no protection at all — the
    # single most common way a stop-limit fails people in practice.
    raw_stop_limit = (
        norm["sl"] * (1 - stop_limit_offset) if exit_side == "SELL"
        else norm["sl"] * (1 + stop_limit_offset)
    )
    stop_limit = await normalize_order(
        session, symbol, testnet, quantity, prices={"p": raw_stop_limit}
    )

    base = TESTNET if testnet else MAINNET
    params = {
        "symbol": symbol,
        "side": exit_side,
        "quantity": norm["quantity_str"],
        "price": norm["tp_str"],                 # take-profit limit leg
        "stopPrice": norm["sl_str"],             # stop trigger
        "stopLimitPrice": stop_limit["p_str"],   # stop-limit leg
        "stopLimitTimeInForce": "GTC",
        "timestamp": int(time.time() * 1000),
        "recvWindow": 5000,
    }
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/order/oco?{query}"
    async with session.post(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance OCO error {r.status}"))
        return data


async def cancel_order_list(
    session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool,
    symbol: str, order_list_id: int,
) -> dict:
    """Cancels an OCO pair. Used before replacing SL/TP and before a manual
    close — leaving a stale OCO behind would sell a position you no longer
    hold the moment price touched the old level."""
    base = TESTNET if testnet else MAINNET
    params = {
        "symbol": symbol, "orderListId": order_list_id,
        "timestamp": int(time.time() * 1000), "recvWindow": 5000,
    }
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/orderList?{query}"
    async with session.delete(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        # -2011 "Unknown order sent" means it's already gone (filled or
        # cancelled elsewhere) — that's the desired end state, not an error.
        if r.status != 200 and data.get("code") != -2011:
            raise ValueError(data.get("msg", f"Binance OCO cancel error {r.status}"))
        return data


async def get_order_list(
    session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool,
    order_list_id: int,
) -> dict:
    """Current status of an OCO pair, so the app can notice the exchange
    closed a position and settle its own record to match."""
    base = TESTNET if testnet else MAINNET
    params = {
        "orderListId": order_list_id,
        "timestamp": int(time.time() * 1000), "recvWindow": 5000,
    }
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/orderList?{query}"
    async with session.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance orderList error {r.status}"))
        return data


async def get_order(
    session: aiohttp.ClientSession, api_key: str, api_secret: str, testnet: bool,
    symbol: str, order_id: int,
) -> dict:
    """Single order detail — used to read the actual fill price of whichever
    OCO leg executed, rather than assuming it filled exactly at the level."""
    base = TESTNET if testnet else MAINNET
    params = {
        "symbol": symbol, "orderId": order_id,
        "timestamp": int(time.time() * 1000), "recvWindow": 5000,
    }
    query = _sign(params, api_secret)
    url = f"{base}/api/v3/order?{query}"
    async with session.get(url, headers={"X-MBX-APIKEY": api_key}, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
        if r.status != 200:
            raise ValueError(data.get("msg", f"Binance order lookup error {r.status}"))
        return data
