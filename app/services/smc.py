"""
Smart-money-concepts detectors: order blocks, fair value gaps, market
structure (swing highs/lows + trend), liquidity sweeps, and points of
interest.

WHY THIS FILE EXISTS: these were originally implemented twice — once in
TypeScript (frontend/src/components/ProChart.tsx) purely to draw the chart
overlays, and once, differently and more simply, in the Smart Money
Concepts bot strategy (services/backtest.py). Two implementations of "what
counts as an order block" drift apart the moment either one gets tweaked,
and worse, it means the chart can show a zone the bot never actually
considered, or vice versa — exactly the kind of mismatch that makes a
trading system hard to trust. This module is the ONE place that logic
lives now: the bot's strategy imports these functions directly, and the
chart gets the identical output over the API (see routers/market.py's
/market/smc endpoint) instead of recomputing its own version in JS.

Every function takes a plain list of candle dicts with
{"time" (ms epoch), "open", "high", "low", "close", "volume"} — the same
shape fetch_klines already returns everywhere else in this backend — and
is a straight, careful port of the logic in ProChart.tsx, so the two stay
provably identical instead of "similar".

CAUSALITY: every function here only ever reads the candles it's given.
Callers that need a point-in-time read for backtesting (no lookahead) get
that for free by passing a truncated slice — see how
_run_smart_money in backtest.py calls these per-bar on a growing/sliding
window rather than the full series.
"""
from typing import List, Dict, Optional, TypedDict


# ── Order blocks ─────────────────────────────────────────────────────────
class OrderBlock(TypedDict):
    top: float
    bottom: float
    kind: str  # "bullish" | "bearish"
    time: int  # seconds — origin candle's bar time
    mitigated: bool
    strength: float


def _order_blocks_pass(candles: List[dict], impulse_mult: float, origin_lookback: int) -> List[OrderBlock]:
    blocks: List[OrderBlock] = []
    if not candles:
        return blocks
    last_price = candles[-1]["close"]

    for i in range(21, len(candles)):
        impulse = candles[i]
        body = abs(impulse["close"] - impulse["open"])
        window = candles[i - 20:i]
        avg_body = (sum(abs(c["close"] - c["open"]) for c in window) / len(window)) if window else 1
        avg_body = avg_body or 1
        if body < avg_body * impulse_mult:
            continue

        bullish_impulse = impulse["close"] > impulse["open"]
        origin = None
        for back in range(1, origin_lookback + 1):
            j = i - back
            if j < 0:
                break
            c = candles[j]
            is_opposite = (c["close"] < c["open"]) if bullish_impulse else (c["close"] > c["open"])
            if is_opposite:
                origin = c
                break
        if origin is None and i - 1 >= 0:
            origin = candles[i - 1]
        if origin is None:
            continue

        top = max(origin["open"], origin["close"])
        bottom = min(origin["open"], origin["close"])
        if top == bottom:
            continue

        mitigated = False
        for j in range(i + 1, len(candles)):
            if (candles[j]["close"] < bottom) if bullish_impulse else (candles[j]["close"] > top):
                mitigated = True
                break

        blocks.append({
            "top": top, "bottom": bottom,
            "kind": "bullish" if bullish_impulse else "bearish",
            "time": int(origin["time"] / 1000),
            "mitigated": mitigated, "strength": body / avg_body,
        })

    def sort_key(b: OrderBlock):
        dist = min(abs(b["top"] - last_price), abs(b["bottom"] - last_price))
        return (1 if b["mitigated"] else 0, dist)

    blocks.sort(key=sort_key)
    return blocks


def find_order_blocks(candles: List[dict], impulse_mult: float = 1.2, max_blocks: int = 6) -> List[OrderBlock]:
    if len(candles) < 25:
        return []
    strict = _order_blocks_pass(candles, impulse_mult, 3)
    blocks = strict if strict else _order_blocks_pass(candles, max(0.8, impulse_mult - 0.4), 6)
    return blocks[:max_blocks]


# ── Supply / demand zones ─────────────────────────────────────────────────
class SDZone(TypedDict):
    top: float
    bottom: float
    kind: str  # "supply" | "demand"
    time: int
    touches: int
    fresh: bool


def _supply_demand_pass(
    candles: List[dict], dep_body_ratio: float, avg_mult: float, base_body_ratio: float, require_unbroken: bool,
) -> List[SDZone]:
    zones: List[SDZone] = []
    for i in range(25, len(candles)):
        departure = candles[i]
        dep_body = abs(departure["close"] - departure["open"])
        dep_range = departure["high"] - departure["low"]
        if dep_range <= 0:
            continue
        if dep_body / dep_range < dep_body_ratio:
            continue

        window = candles[i - 20:i]
        avg_range = (sum(c["high"] - c["low"] for c in window) / len(window)) if window else 1
        avg_range = avg_range or 1
        if dep_body < avg_range * avg_mult:
            continue

        base = []
        for back in range(1, 5):
            j = i - back
            if j < 0:
                break
            c = candles[j]
            r = c["high"] - c["low"]
            if r <= 0:
                break
            if abs(c["close"] - c["open"]) / r > base_body_ratio:
                break
            base.append(c)
        if not base:
            continue

        top = max(c["high"] for c in base)
        bottom = min(c["low"] for c in base)
        kind = "demand" if departure["close"] > departure["open"] else "supply"

        touches, broken = 0, False
        for j in range(i + 1, len(candles)):
            c = candles[j]
            if c["low"] <= top and c["high"] >= bottom:
                touches += 1
            if (c["close"] < bottom) if kind == "demand" else (c["close"] > top):
                broken = True
                break
        if broken and require_unbroken:
            continue

        zones.append({
            "time": int(base[-1]["time"] / 1000),
            "top": top, "bottom": bottom, "kind": kind,
            "touches": touches, "fresh": touches == 0,
        })
    return zones


def find_supply_demand_zones(candles: List[dict], max_zones: int = 4) -> List[SDZone]:
    if len(candles) < 30:
        return []
    strict = _supply_demand_pass(candles, 0.6, 1.3, 0.5, True)
    zones = strict if strict else _supply_demand_pass(candles, 0.45, 0.9, 0.65, False)
    last_price = candles[-1]["close"]

    def sort_key(z: SDZone):
        dist = min(abs(z["top"] - last_price), abs(z["bottom"] - last_price))
        return (0 if z["fresh"] else 1, dist)

    zones.sort(key=sort_key)
    zones = zones[:max_zones]
    zones.sort(key=lambda z: z["time"])
    return zones


# ── Fair value gaps ────────────────────────────────────────────────────────
class FVG(TypedDict):
    top: float
    bottom: float
    kind: str  # "bullish" | "bearish"
    time: int
    filled: bool
    fill_pct: float


def find_fair_value_gaps(candles: List[dict], max_gaps: int = 8) -> List[FVG]:
    if len(candles) < 5:
        return []
    gaps: List[FVG] = []
    for i in range(2, len(candles)):
        a, c = candles[i - 2], candles[i]
        top = bottom = 0.0
        kind: Optional[str] = None
        if c["low"] > a["high"]:
            bottom, top, kind = a["high"], c["low"], "bullish"
        elif c["high"] < a["low"]:
            top, bottom, kind = a["low"], c["high"], "bearish"
        if kind is None:
            continue
        size = top - bottom
        if size <= 0:
            continue

        filled, deepest = False, 0.0
        for j in range(i + 1, len(candles)):
            cj = candles[j]
            if kind == "bullish":
                # Price sits ABOVE a bullish gap when it forms — "filling"
                # means a later candle's LOW dropping back down into the
                # zone from above.
                intrusion = max(0.0, top - max(bottom, cj["low"]))
                deepest = max(deepest, intrusion)
                if cj["low"] <= bottom:
                    filled, deepest = True, size
                    break
            else:
                # Price sits BELOW a bearish gap when it forms — "filling"
                # means a later candle's HIGH climbing back up into the
                # zone from below.
                intrusion = max(0.0, min(top, cj["high"]) - bottom)
                deepest = max(deepest, intrusion)
                if cj["high"] >= top:
                    filled, deepest = True, size
                    break

        gaps.append({
            "top": top, "bottom": bottom, "kind": kind,
            "time": int(c["time"] / 1000), "filled": filled,
            "fill_pct": min(100.0, (deepest / size) * 100),
        })

    last_price = candles[-1]["close"]

    def sort_key(g: FVG):
        dist = min(abs(g["top"] - last_price), abs(g["bottom"] - last_price))
        return (1 if g["filled"] else 0, dist)

    gaps.sort(key=sort_key)
    gaps = gaps[:max_gaps]
    gaps.sort(key=lambda g: g["time"])
    return gaps


# ── Market structure: swing highs/lows, HH/HL/LH/LL, trend ──────────────
class SwingPoint(TypedDict):
    time: int
    price: float
    kind: str  # "high" | "low"
    label: Optional[str]  # "HH" | "LH" | "HL" | "LL" | None


def find_swings(candles: List[dict], lookback: int = 3) -> List[SwingPoint]:
    """Confirmed swing highs/lows only (see the pivot test below) — no
    label attached yet. Used both to build market structure and as the
    causal, confirm-only-after-`lookback`-more-bars input the backtest
    runner needs to stay lookahead-free (see `confirmed_at` note in
    backtest.py's own copy of this idea for _run_smart_money)."""
    n = len(candles)
    if n < lookback * 2 + 1:
        return []
    raw: List[SwingPoint] = []
    for i in range(lookback, n - lookback):
        c = candles[i]
        window = range(i - lookback, i + lookback + 1)
        is_high = all(candles[j]["high"] < c["high"] for j in window if j != i)
        is_low = all(candles[j]["low"] > c["low"] for j in window if j != i)
        if is_high:
            raw.append({"time": int(c["time"] / 1000), "price": c["high"], "kind": "high", "label": None})
        if is_low:
            raw.append({"time": int(c["time"] / 1000), "price": c["low"], "kind": "low", "label": None})
    raw.sort(key=lambda s: s["time"])
    return raw


def label_swings(swings: List[SwingPoint]) -> None:
    """Mutates `swings` in place, attaching HH/LH/HL/LL labels by
    comparing each swing to the previous CONFIRMED swing of the same
    kind — the actual definition of market structure."""
    last_high = last_low = None
    for s in swings:
        if s["kind"] == "high":
            s["label"] = None if last_high is None else ("HH" if s["price"] > last_high else "LH")
            last_high = s["price"]
        else:
            s["label"] = None if last_low is None else ("HL" if s["price"] > last_low else "LL")
            last_low = s["price"]


def find_market_structure(candles: List[dict], lookback: int = 3) -> Dict:
    swings = find_swings(candles, lookback)
    label_swings(swings)

    recent_high = next((s for s in reversed(swings) if s["kind"] == "high" and s["label"]), None)
    recent_low = next((s for s in reversed(swings) if s["kind"] == "low" and s["label"]), None)
    trend = "ranging"
    if recent_high and recent_low:
        if recent_high["label"] == "HH" and recent_low["label"] == "HL":
            trend = "bullish"
        elif recent_high["label"] == "LH" and recent_low["label"] == "LL":
            trend = "bearish"

    return {"swings": swings[-24:], "trend": trend}


# ── Liquidity sweeps ───────────────────────────────────────────────────────
class Sweep(TypedDict):
    time: int
    level: float
    wick: float
    kind: str  # "bullish" | "bearish"


def find_liquidity_sweeps(candles: List[dict], swings: List[SwingPoint], max_sweeps: int = 6) -> List[Sweep]:
    if not swings or len(candles) < 5:
        return []
    highs = [s for s in swings if s["kind"] == "high"]
    lows = [s for s in swings if s["kind"] == "low"]
    sweeps: List[Sweep] = []

    for i in range(1, len(candles)):
        c = candles[i]
        t = int(c["time"] / 1000)
        prior_high = next((h for h in reversed(highs) if h["time"] < t and h["price"] < c["high"]), None)
        if prior_high and c["close"] < prior_high["price"] and c["open"] < prior_high["price"]:
            sweeps.append({"time": t, "level": prior_high["price"], "wick": c["high"], "kind": "bearish"})
        prior_low = next((l for l in reversed(lows) if l["time"] < t and l["price"] > c["low"]), None)
        if prior_low and c["close"] > prior_low["price"] and c["open"] > prior_low["price"]:
            sweeps.append({"time": t, "level": prior_low["price"], "wick": c["low"], "kind": "bullish"})

    return sweeps[-max_sweeps:]


# ── Points of interest ─────────────────────────────────────────────────────
def find_points_of_interest(
    candles: List[dict],
    order_blocks: List[OrderBlock],
    fvgs: List[FVG],
    sd_zones: Optional[List[SDZone]] = None,
) -> List[Dict]:
    """The single closest live zone above price and the single closest one
    below it, pooled across order blocks, fair value gaps and (optionally)
    supply/demand zones — deliberately just one or two results, not every
    zone on the chart, so it reads as "watch this" rather than more noise."""
    if not candles:
        return []
    last_price = candles[-1]["close"]
    candidates: List[Dict] = []
    for b in order_blocks:
        if not b["mitigated"]:
            candidates.append({"top": b["top"], "bottom": b["bottom"], "time": b["time"], "kind": b["kind"], "source": "OB"})
    for g in fvgs:
        if not g["filled"]:
            candidates.append({"top": g["top"], "bottom": g["bottom"], "time": g["time"], "kind": g["kind"], "source": "FVG"})
    for z in (sd_zones or []):
        if z["fresh"]:
            candidates.append({
                "top": z["top"], "bottom": z["bottom"], "time": z["time"],
                "kind": "bullish" if z["kind"] == "demand" else "bearish", "source": "S/D",
            })

    below = sorted((c for c in candidates if c["top"] <= last_price), key=lambda c: last_price - c["top"])
    above = sorted((c for c in candidates if c["bottom"] >= last_price), key=lambda c: c["bottom"] - last_price)
    result = []
    if below:
        result.append(below[0])
    if above:
        result.append(above[0])
    return result
