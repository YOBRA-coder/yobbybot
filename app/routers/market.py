import aiohttp
from fastapi import APIRouter

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


@router.get("/orderbook/{symbol}")
async def get_order_book(symbol: str, limit: int = 20):
    async with aiohttp.ClientSession() as s:
        return await fetch_order_book(s, symbol, limit)
