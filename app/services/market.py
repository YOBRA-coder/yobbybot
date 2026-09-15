import json
import random
import time
from typing import Dict, List

import aiohttp

from ..config import BASE_PRICES, BINANCE_REST, PAIRS, log

live_prices: Dict[str, float] = dict(BASE_PRICES)
live_tickers: Dict[str, dict] = {}


async def fetch_binance_tickers(session: aiohttp.ClientSession) -> List[dict]:
    """Fetch real ticker data from Binance REST API.

    NO MORE SIMULATED/DEMO DATA (same reasoning as fetch_klines above) —
    this used to fall back to a random-walk "simulated" ticker feed
    whenever Binance blipped, and that fake feed fed EVERYTHING that reads
    a price: the dashboard, every bot's live signal evaluation and P&L,
    manual trade entry/exit pricing, and auto/manual signal generation.
    A brief Binance hiccup could mean a bot's SL/TP got checked against a
    fabricated price, or — worse — a manual trade filled at one (see the
    fix in routers/trades.py). live_tickers only ever gets written to on
    a REAL successful fetch now, so on a failure this just returns
    whatever's already cached there (the last known REAL read for each
    symbol) unchanged, or an empty list if nothing's been fetched yet.
    """
    try:
        # NOTE: Binance's multi-symbol ticker endpoint requires the `symbols`
        # array with NO spaces (e.g. ["BTCUSDT","ETHUSDT"]). json.dumps()'s
        # default separators insert ", " between elements, which Binance
        # rejects with a 400 — silently sending every request here into the
        # `except`/non-200 branch below and permanently falling back to
        # generate_sim_tickers(), forever, even with a perfectly healthy
        # connection. This is why tickers/dashboard/bots/signals were all
        # running on fake prices while klines (single-symbol, no array) kept
        # working and looked real. separators=(",", ":") removes the space.
        symbols = json.dumps(PAIRS, separators=(",", ":"))
        url = f"{BINANCE_REST}/api/v3/ticker/24hr"
        async with session.get(url, params={"symbols": symbols}, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                tickers = []
                for d in data:
                    p = float(d["lastPrice"])
                    live_prices[d["symbol"]] = p
                    t = {
                        "symbol": d["symbol"],
                        "price": p,
                        "change24h": float(d["priceChange"]),
                        "changePct": float(d["priceChangePercent"]),
                        "high24h": float(d["highPrice"]),
                        "low24h": float(d["lowPrice"]),
                        "volume24h": float(d["volume"]),
                        "bid": float(d["bidPrice"]),
                        "ask": float(d["askPrice"]),
                        "lastUpdate": int(time.time() * 1000),
                    }
                    tickers.append(t)
                    live_tickers[d["symbol"]] = t
                log.debug("Fetched %d live tickers from Binance", len(tickers))
                return tickers
            log.warning("Binance ticker request returned status %s", r.status)
    except Exception as e:
        log.warning("Binance API error: %s", e)

    if live_tickers:
        log.info("Ticker fetch failed — holding last known real tickers instead of fabricating data")
        return list(live_tickers.values())
    return []


async def fetch_order_book(session: aiohttp.ClientSession, symbol: str, limit: int = 20) -> dict:
    """Real order-book depth from Binance, with a simulated fallback built
    around the current price when Binance is unreachable — same pattern as
    tickers/klines, so the frontend always gets a usable book."""
    try:
        url = f"{BINANCE_REST}/api/v3/depth?symbol={symbol}&limit={limit}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 200:
                data = await r.json()
                return {
                    "bids": [{"price": float(p), "size": float(q)} for p, q in data["bids"]],
                    "asks": [{"price": float(p), "size": float(q)} for p, q in data["asks"]],
                }
    except Exception as e:
        log.warning("Binance depth error for %s: %s — using simulation", symbol, e)
    return _gen_sim_order_book(live_prices.get(symbol, BASE_PRICES.get(symbol, 100)), limit)


def _gen_sim_order_book(mid: float, levels: int) -> dict:
    bids, asks = [], []
    for i in range(1, levels + 1):
        spread = mid * 0.0004 * i
        bids.append({"price": mid - spread, "size": random.random() * 5 + 0.1})
        asks.append({"price": mid + spread, "size": random.random() * 5 + 0.1})
    return {"bids": bids, "asks": asks}


async def fetch_klines(
    session: aiohttp.ClientSession, symbol: str, interval: str = "1h", limit: int = 100,
    end_time: int | None = None,
) -> List[dict]:
    """Fetch OHLCV candles from Binance.

    `end_time` (ms epoch) lets a caller page backwards for older history —
    Binance's `endTime` param returns the `limit` candles ending at/before
    that timestamp — used by the chart's "load more on scroll back" feature
    so scrolling back doesn't just hit a hard wall at the initial 100 bars.

    NO MORE SIMULATED/DEMO CANDLES. This used to fall back to randomly
    generated candles whenever Binance blipped (rate limit, brief timeout,
    network hiccup). Those fake candles got merged straight into the
    chart's real candle history by time — since a simulated bar sits at a
    different price than the real market, that merge is exactly what
    showed up as the chart "breaking"/jumping after loading real data
    fine at first. Fabricated data also has no place in bot decisions,
    signals, or backtests, which all read from this same function.

    On a failure this now returns the last known REAL candles for this
    exact symbol+interval (so the chart/bot just holds its last genuine
    read instead of jumping to fake data), or an empty list if we've never
    successfully fetched this symbol+interval yet — callers treat an empty
    list as "still loading, nothing to show/act on" rather than crashing
    or trading on it.
    """
    key = f"{symbol}:{interval}"
    try:
        url = f"{BINANCE_REST}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        if end_time:
            url += f"&endTime={end_time}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                data = await r.json()
                candles = [{"time": k[0], "open": float(k[1]), "high": float(k[2]),
                            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in data]
                if candles:
                    live_prices[symbol] = candles[-1]["close"]
                    # Only cache the CURRENT window (end_time is None) as
                    # "last known real data" — a paged-backwards request
                    # for older history is a different, independent time
                    # window and shouldn't overwrite the live cache.
                    if end_time is None:
                        _last_real_klines[key] = candles
                return candles
            log.warning("Binance klines request for %s returned status %s", symbol, r.status)
    except Exception as e:
        log.warning("Binance klines error for %s: %s", symbol, e)

    if end_time is None:
        cached = _last_real_klines.get(key)
        if cached:
            log.info("Klines fetch failed for %s — holding last known real candles instead of fabricating data", key)
            return list(cached)
    return []


# Last known REAL candles per "symbol:interval" — see the no-fallback note
# on fetch_klines above. Only ever populated with genuine Binance data.
_last_real_klines: Dict[str, List[dict]] = {}
