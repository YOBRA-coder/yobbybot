import aiohttp
from fastapi import APIRouter

from ..services import smc
from ..services.market import fetch_binance_tickers, fetch_klines, fetch_order_book

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/tickers")
async def get_tickers():
    async with aiohttp.ClientSession() as s:
        return await fetch_binance_tickers(s)


@router.get("/klines/{symbol}")
async def get_klines(symbol: str, interval: str = "1h", limit: int = 1000, before: int | None = None):
    # `before` (ms epoch) pages backwards for older history — the chart
    # calls this with the timestamp of its oldest loaded candle when the
    # user scrolls back near the edge of what's loaded, so history keeps
    # extending instead of hitting a hard wall at the first ~100 bars.
    async with aiohttp.ClientSession() as s:
        return await fetch_klines(s, symbol, interval, limit, end_time=before)


@router.get("/smc/{symbol}")
async def get_smc(symbol: str, interval: str = "1h", limit: int = 300):
    """Order blocks, fair value gaps, market structure, liquidity sweeps
    and points of interest — computed by the EXACT SAME functions
    (app/services/smc.py) the Smart Money Concepts bot strategy trades
    off of. This is what the chart's OB/FVG/MS/SWEEP/POI overlays call
    instead of recomputing their own version client-side, so what a bot
    running that strategy actually saw is provably the same thing the
    chart shows for that symbol/timeframe — not just a similar-looking
    approximation of it.
    """
    async with aiohttp.ClientSession() as s:
        candles = await fetch_klines(s, symbol, interval, limit)
    if not candles:
        return {"orderBlocks": [], "fvgs": [], "structure": {"swings": [], "trend": "ranging"}, "sweeps": [], "poi": []}

    order_blocks = smc.find_order_blocks(candles)
    fvgs = smc.find_fair_value_gaps(candles)
    sd_zones = smc.find_supply_demand_zones(candles)
    structure = smc.find_market_structure(candles)
    sweeps = smc.find_liquidity_sweeps(candles, structure["swings"])
    poi = smc.find_points_of_interest(candles, order_blocks, fvgs, sd_zones)
    # smc.py's internal dicts use snake_case (fill_pct) like the rest of
    # this backend's Python; the frontend's existing OrderBlock/FVG/etc.
    # interfaces (originally written for the client-side TS version of
    # these detectors) expect camelCase, matching every other API
    # response here (see targetPrice/stopLoss in evaluate_preset_live).
    def _to_camel(g: dict) -> dict:
        out = dict(g)
        out["fillPct"] = out.pop("fill_pct")
        return out
    fvgs_out = [_to_camel(g) for g in fvgs]
    return {
        "orderBlocks": order_blocks, "fvgs": fvgs_out, "sdZones": sd_zones,
        "structure": structure, "sweeps": sweeps, "poi": poi,
    }


@router.get("/orderbook/{symbol}")
async def get_order_book(symbol: str, limit: int = 20):
    async with aiohttp.ClientSession() as s:
        return await fetch_order_book(s, symbol, limit)
