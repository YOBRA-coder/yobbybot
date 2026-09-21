"""Deterministic candle builders for the SMC tests — no network, no randomness
that isn't seeded, so a failure here is a real regression in smc.py."""
import random
from typing import List

T0 = 1_700_000_000_000  # ms epoch
STEP = 15 * 60 * 1000   # 15m bars


def candle(i: int, o: float, h: float, l: float, c: float, v: float = 100.0) -> dict:
    return {"time": T0 + i * STEP, "open": o, "high": max(h, o, c), "low": min(l, o, c), "close": c, "volume": v}


def from_waypoints(points: List[tuple], seed: int = 7, noise: float = 0.15) -> List[dict]:
    """Piecewise-linear close path through (bar_index, price) waypoints, with
    small seeded wicks/bodies so it looks like real candles."""
    rng = random.Random(seed)
    pts = sorted(points)
    closes = []
    for (i0, p0), (i1, p1) in zip(pts, pts[1:]):
        for i in range(i0, i1):
            closes.append(p0 + (p1 - p0) * (i - i0) / (i1 - i0))
    closes.append(pts[-1][1])
    out, prev = [], closes[0]
    for i, cl in enumerate(closes):
        o = prev
        jitter = rng.uniform(-noise, noise) * 0.3
        c = cl + jitter
        h = max(o, c) + rng.uniform(0.02, noise)
        l = min(o, c) - rng.uniform(0.02, noise)
        out.append(candle(i, o, h, l, c))
        prev = c
    return out
