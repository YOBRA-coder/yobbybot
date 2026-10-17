"""
OPT-IN synthetic market for testing the UI with no internet (CRYPTO_DEMO=1).

One seeded 1-minute random walk per symbol (trend regimes, volatility
clusters, occasional impulse legs, so order blocks / FVGs / BOS / CHoCH
actually appear), resampled to every timeframe — so 4h, 1h, 15m and 5m all
describe the SAME price path, like a real exchange. Never used unless the
env flag is set, never stored in the candle database, and every API response
that used it says "demo": true.
"""
import random
import time
from typing import Dict, List

from ..config import BASE_PRICES

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}
_DAYS = 90


class _Feed:
    def __init__(self, symbol: str):
        self.rng = random.Random(sum(ord(ch) * (i + 1) for i, ch in enumerate(symbol)))
        base = BASE_PRICES.get(symbol, 100.0)
        now_min = int(time.time() // 60)
        self.t0 = (now_min - _DAYS * 1440) * 60_000
        self.rows: List[List[float]] = []     # [t, o, h, l, c, v]
        self.price = base * self.rng.uniform(0.85, 1.0)
        self.drift = 0.0
        self.vol = 0.0006
        self.regime_left = 0
        self.impulse_left = 0
        self.impulse_dir = 1
        self._extend(now_min)

    def _step(self, t: int):
        r = self.rng
        if self.regime_left <= 0:
            self.regime_left = r.randint(300, 1800)
            self.drift = r.choice([-1, 1, 0, 1, -1]) * r.uniform(0.00001, 0.00005)
            self.vol = r.uniform(0.0003, 0.0009)
        self.regime_left -= 1
        if self.impulse_left <= 0 and r.random() < 0.0015:
            self.impulse_left = r.randint(8, 40)
            self.impulse_dir = r.choice([-1, 1])
        shock = r.gauss(0, self.vol)
        if self.impulse_left > 0:
            shock += self.impulse_dir * self.vol * r.uniform(1.0, 2.6)
            self.impulse_left -= 1
        o = self.price
        c = max(o * (1 + self.drift + shock), 1e-9)
        wick = abs(r.gauss(0, self.vol * 0.7))
        h = max(o, c) * (1 + wick * r.random())
        l = min(o, c) * (1 - wick * r.random())
        v = r.uniform(5, 30) * (1 + abs(shock) / self.vol) * (3 if self.impulse_left > 0 else 1)
        self.rows.append([t, o, h, l, c, v])
        self.price = c

    def _extend(self, now_min: int):
        start_min = self.t0 // 60_000 + len(self.rows)
        for m in range(start_min, now_min + 1):
            self._step(m * 60_000)


_feeds: Dict[str, _Feed] = {}


def klines(symbol: str, interval: str, limit: int, end_time: int | None = None) -> List[dict]:
    step = INTERVAL_MS.get(interval)
    if step is None:
        return []
    feed = _feeds.get(symbol)
    if feed is None:
        feed = _feeds[symbol] = _Feed(symbol)
    feed._extend(int(time.time() // 60))
    rows = feed.rows
    if end_time:
        rows = [r for r in rows if r[0] <= end_time]
    buckets: Dict[int, List[float]] = {}
    order: List[int] = []
    for r in rows:
        b = int(r[0] // step * step)
        cur = buckets.get(b)
        if cur is None:
            buckets[b] = [b, r[1], r[2], r[3], r[4], r[5]]
            order.append(b)
        else:
            cur[2] = max(cur[2], r[2])
            cur[3] = min(cur[3], r[3])
            cur[4] = r[4]
            cur[5] += r[5]
    out = [buckets[b] for b in order][-limit:]
    return [{"time": int(b[0]), "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in out]


def last_price(symbol: str) -> float:
    k = klines(symbol, "1m", 1)
    return k[-1]["close"] if k else BASE_PRICES.get(symbol, 100.0)
