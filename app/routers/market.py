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
async def get_smc(symbol: str, interval: str = "1h", limit: int = smc.SMC_WINDOW):
    """Everything the chart draws for Smart Money Concepts — order blocks,
    fair value gaps, supply/demand zones, swing structure (HH/HL/LH/LL) and
    trend, liquidity sweeps, points of interest and retests — PLUS the
    strategy's own verdict on the newest bar (`decision`, with the step-by-
    step working the bot logs).

    All of it comes from `smc.analyze()` / `smc.evaluate_entry()`, the same
    two functions the Smart Money Concepts bot strategy and its backtest run,
    over the same `SMC_WINDOW` candles. So what's drawn is what the bot saw
    and `decision` is what it would do — not a lookalike computed elsewhere.
    """
    async with aiohttp.ClientSession() as s:
        candles = await fetch_klines(s, symbol, interval, limit)
    if not candles:
        return {
            "orderBlocks": [], "fvgs": [], "sdZones": [], "structure": {"swings": [], "trend": "ranging"},
            "sweeps": [], "poi": [], "retests": [], "decision": None,
        }

    snap = smc.analyze(candles)
    decision = smc.evaluate_entry(candles, snap)

    # smc.py's internal dicts use snake_case (fill_pct) like the rest of
    # this backend's Python; the frontend interfaces expect camelCase,
    # matching every other API response here.
    def _to_camel(g: dict) -> dict:
        out = dict(g)
        out["fillPct"] = out.pop("fill_pct")
        return out

    return {
        "orderBlocks": snap["order_blocks"],
        "fvgs": [_to_camel(g) for g in snap["fvgs"]],
        "sdZones": snap["sd_zones"],
        "structure": snap["structure"],
        "sweeps": snap["sweeps"],
        "poi": snap["poi"],
        "retests": snap["retests"],
        "decision": decision,
        "candles": len(candles),
        "lastTime": int(candles[-1]["time"] / 1000),
    }


@router.get("/orderbook/{symbol}")
async def get_order_book(symbol: str, limit: int = 20):
    async with aiohttp.ClientSession() as s:
        return await fetch_order_book(s, symbol, limit)
