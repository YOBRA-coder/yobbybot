import json
import random
import time
from typing import Dict, List

import aiohttp

from ..config import BASE_PRICES, BINANCE_HOSTS, DEMO_MODE, PAIRS, log
from . import candle_store, demo_feed

live_prices: Dict[str, float] = dict(BASE_PRICES)
live_tickers: Dict[str, dict] = {}


_good_host: Dict[str, int] = {"i": 0}   # index into BINANCE_HOSTS that last worked


async def _get_json(session: aiohttp.ClientSession, path: str, params: dict | None = None, timeout: float = 8):
    """GET a PUBLIC Binance endpoint, trying each host in BINANCE_HOSTS
    (starting with the one that worked last). Returns parsed JSON, or None if
    every host failed. Region blocks (HTTP 451/403) just move on to the next
    host instead of leaving the chart empty."""
    n = len(BINANCE_HOSTS)
    start = _good_host["i"] % n
    for k in range(n):
        idx = (start + k) % n
        host = BINANCE_HOSTS[idx]
        try:
            async with session.get(f"{host}{path}", params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    _good_host["i"] = idx
                    return await r.json()
                log.warning("%s%s -> HTTP %s", host, path, r.status)
        except Exception as e:  # noqa: BLE001
            log.warning("%s%s failed: %s", host, path, e)
    return None


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
        data = await _get_json(session, "/api/v3/ticker/24hr", {"symbols": symbols}, timeout=6)
        if data:
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
    except Exception as e:
        log.warning("Binance API error: %s", e)

    if DEMO_MODE and not live_tickers:
        return _demo_tickers()
    if live_tickers:
        log.info("Ticker fetch failed — holding last known real tickers instead of fabricating data")
        return list(live_tickers.values())
    return []


def _demo_tickers() -> List[dict]:
    """CRYPTO_DEMO=1 only — flagged by the UI; also fills live_tickers so the bots page works offline."""
    out = []
    for sym in PAIRS:
        k = demo_feed.klines(sym, "1d", 2)
        day = demo_feed.klines(sym, "1m", 1440)
        if not k or not day:
            continue
        p = day[-1]["close"]
        first = day[0]["open"]
        t = {"symbol": sym, "price": p, "change24h": p - first, "changePct": (p - first) / first * 100,
             "high24h": max(c["high"] for c in day), "low24h": min(c["low"] for c in day),
             "volume24h": sum(c["volume"] for c in day), "bid": p * 0.9999, "ask": p * 1.0001,
             "lastUpdate": int(time.time() * 1000)}
        live_prices[sym] = p
        live_tickers[sym] = t
        out.append(t)
    return out


async def fetch_order_book(session: aiohttp.ClientSession, symbol: str, limit: int = 20) -> dict:
    """Real order-book depth from Binance, with a simulated fallback built
    around the current price when Binance is unreachable — same pattern as
    tickers/klines, so the frontend always gets a usable book."""
    try:
        data = await _get_json(session, "/api/v3/depth", {"symbol": symbol, "limit": limit}, timeout=5)
        if data:
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
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time:
            params["endTime"] = end_time
        data = await _get_json(session, "/api/v3/klines", params, timeout=8)
        if data:
            candles = [{"time": k[0], "open": float(k[1]), "high": float(k[2]),
                        "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])} for k in data]
            if candles:
                live_prices[symbol] = candles[-1]["close"]
                if end_time is None:
                    _last_real_klines[key] = candles
                # Persist everything real we ever see (see candle_store.py).
                try:
                    candle_store.upsert(symbol, interval, candles)
                except Exception as e:  # noqa: BLE001
                    log.warning("candle store write failed: %s", e)
            return candles
    except Exception as e:
        log.warning("Binance klines error for %s: %s", symbol, e)

    # Exchange unreachable. Hold the last REAL read: in-memory first, then the
    # candle database (survives restarts), and only in opt-in demo mode a
    # synthetic series. Never fabricates unless CRYPTO_DEMO=1.
    if end_time is None:
        cached = _last_real_klines.get(key)
        if cached:
            log.info("Klines fetch failed for %s — holding last known real candles", key)
            return list(cached)
    try:
        stored = candle_store.load(symbol, interval, limit, end_time)
    except Exception:  # noqa: BLE001
        stored = []
    if stored:
        log.info("Klines fetch failed for %s — serving %d candles from the candle store", key, len(stored))
        return stored
    if DEMO_MODE:
        return demo_feed.klines(symbol, interval, limit, end_time)
    return []


async def fetch_history(session: aiohttp.ClientSession, symbol: str, interval: str, total: int) -> List[dict]:
    """`total` candles ending now, paged backwards 1000 at a time (Binance's cap).
    If the candle store already holds enough, fresh candles, it is used instead of re-downloading."""
    from . import candle_store
    try:
        stored = candle_store.load(symbol, interval, total)
        bar_ms = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
                  "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}.get(interval, 60_000)
        if len(stored) >= total and time.time() * 1000 - stored[-1]["time"] < bar_ms * 3:
            return stored
    except Exception:  # noqa: BLE001
        pass
    out: List[dict] = []
    end: int | None = None
    while len(out) < total:
        page = await fetch_klines(session, symbol, interval, min(1000, total - len(out)), end_time=end)
        if not page:
            break
        out = page + out
        end = page[0]["time"] - 1
        if len(page) < 2:
            break
    # de-dup by time (paging edges) and keep order
    seen, uniq = set(), []
    for c in out:
        if c["time"] not in seen:
            seen.add(c["time"])
            uniq.append(c)
    return uniq[-total:]


def is_demo() -> bool:
    """True when the data being served is the synthetic opt-in feed (no real exchange data reachable)."""
    return DEMO_MODE and not live_tickers_real()


def live_tickers_real() -> bool:
    return any(t for t in _last_real_klines.values())


# Last known REAL candles per "symbol:interval" — see the no-fallback note
# on fetch_klines above. Only ever populated with genuine Binance data.
_last_real_klines: Dict[str, List[dict]] = {}
