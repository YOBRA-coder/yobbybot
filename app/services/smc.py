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
    formed_at: int  # seconds — the impulse candle that confirmed the block


def _order_blocks_pass(candles: List[dict], impulse_mult: float, origin_lookback: int) -> List[OrderBlock]:
    blocks: List[OrderBlock] = []
    seen_origin: Dict[int, OrderBlock] = {}
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

        origin_time = int(origin["time"] / 1000)
        prev = seen_origin.get(origin_time)
        if prev is not None:
            # Several impulse candles in a row all point back at the SAME
            # origin candle. That used to emit one identical rectangle per
            # impulse — stacked copies eating the max_blocks slots and
            # drawn on top of each other. Keep the earliest one (its
            # mitigation scan starts at the true formation, so its
            # `mitigated` flag is the honest one) and just remember the
            # strongest impulse seen.
            prev["strength"] = max(prev["strength"], body / avg_body)
            continue
        block: OrderBlock = {
            "top": top, "bottom": bottom,
            "kind": "bullish" if bullish_impulse else "bearish",
            "time": origin_time,
            "mitigated": mitigated, "strength": body / avg_body,
            "formed_at": int(impulse["time"] / 1000),
        }
        seen_origin[origin_time] = block
        blocks.append(block)

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
    formed_at: int  # seconds — the departure candle that confirmed the zone


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
            "formed_at": int(departure["time"] / 1000),
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


# ── Retests ─────────────────────────────────────────────────────────────────
class Retest(TypedDict):
    time: int        # seconds — the bar where price came back into the zone
    kind: str        # "bullish" (into demand/support) | "bearish" (into supply/resistance)
    source: str      # "OB" | "FVG" | "S/D"
    top: float
    bottom: float
    zone_time: int   # seconds — the zone's own anchor time (for drawing)
    confirmed: bool  # closed back OUT of the zone on the zone's side = a rejection, not just a touch
    active: bool     # the newest candle is the one doing the retest right now


def _touches_zone(c: dict, top: float, bottom: float, kind: str) -> bool:
    """Same test the bot has always used for 'price is retesting a bullish
    zone': the candle reached down into it (low <= top) without closing
    through the far side (close > bottom). Mirrored for bearish."""
    if kind == "bullish":
        return c["low"] <= top and c["close"] > bottom
    return c["high"] >= bottom and c["close"] < top


def find_retests(
    candles: List[dict],
    order_blocks: List[OrderBlock],
    fvgs: List[FVG],
    sd_zones: Optional[List[SDZone]] = None,
    max_age: int = 60,
    max_retests: int = 8,
) -> List[Retest]:
    """Every time price came back into a still-live zone after it formed.

    A "retest" is the FIRST bar of each return visit into an unmitigated
    order block, unfilled fair value gap or (unbroken) supply/demand zone —
    consecutive bars sitting inside the same zone are one visit, not many.
    Only the most recent visit per zone is kept, and only visits within the
    last `max_age` bars, so the chart marks what's relevant now instead of
    every historical touch.
    """
    n = len(candles)
    if n < 5:
        return []
    idx_by_time = {int(c["time"] / 1000): i for i, c in enumerate(candles)}
    last_idx = n - 1

    zones: List[Dict] = []
    for b in order_blocks:
        if not b["mitigated"]:
            zones.append({"top": b["top"], "bottom": b["bottom"], "kind": b["kind"], "source": "OB",
                          "zone_time": b["time"], "formed_at": b.get("formed_at", b["time"])})
    for g in fvgs:
        if not g["filled"]:
            zones.append({"top": g["top"], "bottom": g["bottom"], "kind": g["kind"], "source": "FVG",
                          "zone_time": g["time"], "formed_at": g["time"]})
    for z in (sd_zones or []):
        zones.append({"top": z["top"], "bottom": z["bottom"],
                      "kind": "bullish" if z["kind"] == "demand" else "bearish", "source": "S/D",
                      "zone_time": z["time"], "formed_at": z.get("formed_at", z["time"])})

    retests: List[Retest] = []
    for z in zones:
        start = idx_by_time.get(z["formed_at"])
        if start is None:
            # Formed before the window we were handed (or on a bar we
            # can't locate) — scan the whole window rather than drop it.
            start = -1
        # Walk forward once, remembering where the LATEST visit into the
        # zone began. A close through the far side kills the zone, and with
        # it any retest we'd have reported for it.
        visit_start: Optional[int] = None
        prev_inside = False
        for j in range(start + 1, n):
            c = candles[j]
            if z["kind"] == "bullish" and c["close"] <= z["bottom"]:
                visit_start = None
                break
            if z["kind"] == "bearish" and c["close"] >= z["top"]:
                visit_start = None
                break
            inside = _touches_zone(c, z["top"], z["bottom"], z["kind"])
            if inside and not prev_inside:
                visit_start = j
            prev_inside = inside
        if visit_start is None or last_idx - visit_start > max_age:
            continue
        # The visit's first bar, and whether the LATEST bar is still in it.
        c = candles[visit_start]
        if z["kind"] == "bullish":
            confirmed = c["close"] > z["top"]
        else:
            confirmed = c["close"] < z["bottom"]
        retests.append({
            "time": int(c["time"] / 1000), "kind": z["kind"], "source": z["source"],
            "top": z["top"], "bottom": z["bottom"], "zone_time": z["zone_time"],
            "confirmed": confirmed,
            "active": _touches_zone(candles[last_idx], z["top"], z["bottom"], z["kind"]),
        })

    retests.sort(key=lambda r: r["time"])
    return retests[-max_retests:]


# ── One-call snapshot ──────────────────────────────────────────────────────
# Candles the chart AND every SMC bot look at. One number in one place, so a
# zone drawn on the chart is a zone the bot saw — see analyze() below.
SMC_WINDOW = 300


def analyze(candles: List[dict]) -> Dict:
    """Everything the chart draws and everything the bot decides on, from
    ONE pass over ONE candle list. The /market/smc endpoint serialises this
    for the chart; evaluate_entry() below reads it to make the trade
    decision. There is no second implementation to drift."""
    order_blocks = find_order_blocks(candles)
    fvgs = find_fair_value_gaps(candles)
    sd_zones = find_supply_demand_zones(candles)
    structure = find_market_structure(candles)
    sweeps = find_liquidity_sweeps(candles, structure["swings"])
    poi = find_points_of_interest(candles, order_blocks, fvgs, sd_zones)
    retests = find_retests(candles, order_blocks, fvgs, sd_zones)
    return {
        "order_blocks": order_blocks, "fvgs": fvgs, "sd_zones": sd_zones,
        "structure": structure, "sweeps": sweeps, "poi": poi, "retests": retests,
    }


# ── The decision ───────────────────────────────────────────────────────────
def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def _step(step: str, status: str, message: str, sig: Optional[str] = None) -> Dict:
    """One line of the decision trace.

    status: "pass"  the condition is met
            "wait"  not met (yet) — normal, just no trade
            "block" a hard filter that vetoes entries outright
            "info"  context, not a condition
    sig:    a short, price-free fingerprint of the line, so the bot log can
            tell "same situation as last cycle" from "something changed"
            without spamming an identical row every 15 seconds.
    """
    return {"step": step, "status": status, "message": message, "sig": sig if sig is not None else f"{status}"}


def evaluate_entry(candles: List[dict], snapshot: Optional[Dict] = None) -> Dict:
    """The Smart Money Concepts entry decision, with its working shown.

    Long-only (spot can't be shorted, and every other preset here is
    long-only too). Bearish structure/sweeps/zones are still detected and
    reported in the trace — they just never open a trade.

    ENTER when ALL of:
      1. structure is not bearish (bullish = confidence bonus), AND
      2. on the newest bar, either
           a. price swept a swing low and closed back above it, OR
           b. price is retesting a live bullish zone (order block, fair
              value gap, or a first-touch demand zone).

    Returns {"type": "BUY"|"HOLD", "confidence", "reason", "trigger",
    "zone", "invalidation", "trend", "steps"}.
    """
    if len(candles) < 30:
        return {
            "type": "HOLD", "confidence": 0, "trigger": None, "zone": None, "invalidation": None,
            "trend": "ranging",
            "reason": f"Not enough candles yet ({len(candles)}/30)",
            "steps": [_step("DATA", "wait", f"Only {len(candles)} candles loaded — need at least 30", "few")],
        }

    snap = snapshot or analyze(candles)
    c = candles[-1]
    price = c["close"]
    last_t = int(c["time"] / 1000)
    idx_by_time = {int(x["time"] / 1000): i for i, x in enumerate(candles)}
    last_idx = len(candles) - 1
    steps: List[Dict] = []

    steps.append(_step("DATA", "info", f"{len(candles)} candles read, last close {_fmt(price)}", "data"))

    # 1 ── structure ───────────────────────────────────────────────────────
    structure = snap["structure"]
    trend = structure["trend"]
    swings = structure["swings"]
    last_high = next((s for s in reversed(swings) if s["kind"] == "high" and s["label"]), None)
    last_low = next((s for s in reversed(swings) if s["kind"] == "low" and s["label"]), None)
    seq = " / ".join(
        f"{s['label']} {_fmt(s['price'])}" for s in (last_high, last_low) if s
    ) or "not enough swings yet"
    if trend == "bearish":
        steps.append(_step("STRUCTURE", "block",
                           f"Trend BEARISH ({seq}) — long-only, so entries are blocked", "bearish"))
    elif trend == "bullish":
        steps.append(_step("STRUCTURE", "pass", f"Trend BULLISH ({seq})", "bullish"))
    else:
        steps.append(_step("STRUCTURE", "info", f"Trend RANGING ({seq}) — allowed, no bullish bonus", "ranging"))

    # 2 ── liquidity sweeps ────────────────────────────────────────────────
    sweeps = snap["sweeps"]
    swept_now = next((s for s in sweeps if s["kind"] == "bullish" and s["time"] == last_t), None)
    bear_now = next((s for s in sweeps if s["kind"] == "bearish" and s["time"] == last_t), None)
    if swept_now:
        steps.append(_step("SWEEP", "pass",
                           f"Bullish sweep — wick to {_fmt(swept_now['wick'])} took out swing low "
                           f"{_fmt(swept_now['level'])}, closed back above it", "sweep-now"))
    else:
        prev = next((s for s in reversed(sweeps) if s["kind"] == "bullish"), None)
        if prev:
            ago = last_idx - idx_by_time.get(prev["time"], last_idx)
            steps.append(_step("SWEEP", "wait",
                               f"No bullish sweep on this bar (last one {ago} bars ago at {_fmt(prev['level'])})",
                               "sweep-none"))
        else:
            steps.append(_step("SWEEP", "wait", "No bullish liquidity sweep in the recent swings", "sweep-none"))
    if bear_now:
        steps.append(_step("SWEEP", "info",
                           f"Bearish sweep of swing high {_fmt(bear_now['level'])} on this bar — "
                           f"noted, but long-only so no short is taken", "sweep-bear"))

    # 3 ── zones on the map ────────────────────────────────────────────────
    obs = [b for b in snap["order_blocks"] if not b["mitigated"]]
    gaps = [g for g in snap["fvgs"] if not g["filled"]]
    sd = snap["sd_zones"]
    poi = snap["poi"]
    sup = next((p for p in poi if p["top"] <= price), None)
    res = next((p for p in poi if p["bottom"] >= price), None)
    parts = [f"{len(obs)} order block{'s' if len(obs) != 1 else ''}",
             f"{len(gaps)} open FVG{'s' if len(gaps) != 1 else ''}",
             f"{len(sd)} S/D zone{'s' if len(sd) != 1 else ''}"]
    poi_txt = ""
    if sup:
        poi_txt += f" | POI below: {sup['source']} {_fmt(sup['bottom'])}–{_fmt(sup['top'])} ({(sup['top'] - price) / price * 100:+.2f}%)"
    if res:
        poi_txt += f" | POI above: {res['source']} {_fmt(res['bottom'])}–{_fmt(res['top'])} ({(res['bottom'] - price) / price * 100:+.2f}%)"
    steps.append(_step("ZONES", "info", "Live zones: " + ", ".join(parts) + poi_txt,
                       f"zones-{len(obs)}-{len(gaps)}-{len(sd)}"))

    # 4 ── retest of a live bullish zone (newest bar) ──────────────────────
    # A zone must already exist BEFORE the newest bar to be retested by it.
    # Without this the bar that CREATES a zone counted as its own retest —
    # an order block's impulse candle opens at the block, and a fair value
    # gap's top IS its third candle's low — so the bot "bought the retest"
    # on the very candle the zone formed, i.e. chased the impulse instead of
    # waiting for price to come back.
    zone = None
    for b in obs:
        if b.get("formed_at", b["time"]) >= last_t:
            continue
        if b["kind"] == "bullish" and _touches_zone(c, b["top"], b["bottom"], "bullish"):
            zone = {"source": "OB", "kind": "bullish", "top": b["top"], "bottom": b["bottom"], "time": b["time"]}
            break
    if zone is None:
        for g in gaps:
            if g["time"] >= last_t:
                continue
            if g["kind"] == "bullish" and _touches_zone(c, g["top"], g["bottom"], "bullish"):
                zone = {"source": "FVG", "kind": "bullish", "top": g["top"], "bottom": g["bottom"], "time": g["time"]}
                break
    if zone is None:
        for z in sd:
            # A demand zone only counts on its first real return — one
            # that's been hit over and over is spent liquidity.
            if z.get("formed_at", z["time"]) >= last_t:
                continue
            if z["kind"] == "demand" and z["touches"] <= 1 and _touches_zone(c, z["top"], z["bottom"], "bullish"):
                zone = {"source": "S/D", "kind": "bullish", "top": z["top"], "bottom": z["bottom"], "time": z["time"]}
                break
    if zone:
        steps.append(_step("RETEST", "pass",
                           f"Retest of bullish {zone['source']} zone {_fmt(zone['bottom'])}–{_fmt(zone['top'])} "
                           f"(low {_fmt(c['low'])}, close {_fmt(price)})", f"retest-{zone['source']}"))
    else:
        in_bear = next(
            (b for b in obs if b["kind"] == "bearish" and _touches_zone(c, b["top"], b["bottom"], "bearish")), None)
        if in_bear:
            steps.append(_step("RETEST", "info",
                               f"Price is inside a bearish OB {_fmt(in_bear['bottom'])}–{_fmt(in_bear['top'])} "
                               f"— resistance, no long", "retest-bear"))
        elif sup:
            steps.append(_step("RETEST", "wait",
                               f"Not in a bullish zone — nearest support is {sup['source']} "
                               f"{_fmt(sup['bottom'])}–{_fmt(sup['top'])}, {(price - sup['top']) / price * 100:.2f}% below",
                               "retest-none"))
        else:
            steps.append(_step("RETEST", "wait", "Not in a bullish zone — no live support zone below price", "retest-none"))

    # 5 ── decision ────────────────────────────────────────────────────────
    entered = trend != "bearish" and (swept_now is not None or zone is not None)
    confidence = 0
    trigger = None
    invalidation = None
    confluence = ""
    if entered:
        if swept_now and zone:
            trigger = "sweep+retest"
            confluence = (f"liquidity sweep of swing low {swept_now['level']:.4f} "
                          f"into a bullish {zone['source']} zone ({zone['bottom']:.4f}-{zone['top']:.4f})")
            invalidation = min(swept_now["wick"], zone["bottom"])
        elif swept_now:
            trigger = "sweep"
            confluence = f"liquidity sweep of swing low {swept_now['level']:.4f}"
            invalidation = swept_now["wick"]
        else:
            trigger = f"retest-{zone['source']}"
            confluence = f"retest of a bullish {zone['source']} zone ({zone['bottom']:.4f}-{zone['top']:.4f})"
            invalidation = zone["bottom"]
        confidence = 70
        if trend == "bullish":
            confidence += 10
        if swept_now and zone:
            confidence += 5
        if zone:
            # A second live bullish zone stacked on the same price = more
            # orders resting there than a lone zone.
            stacked = [
                o for o in obs
                if o["kind"] == "bullish" and not (o["top"] == zone["top"] and o["bottom"] == zone["bottom"])
                and o["bottom"] <= zone["top"] and o["top"] >= zone["bottom"]
            ] + [
                g for g in gaps
                if g["kind"] == "bullish" and not (g["top"] == zone["top"] and g["bottom"] == zone["bottom"])
                and g["bottom"] <= zone["top"] and g["top"] >= zone["bottom"]
            ]
            if stacked:
                confidence += 5
        confidence = max(60, min(95, confidence))
        reason = f"{confluence[0].upper()}{confluence[1:]} (structure: {trend})"
        steps.append(_step("DECISION", "pass",
                           f"BUY — {confluence}; structure {trend}; confidence {confidence}%; "
                           f"invalidation below {_fmt(invalidation)}", f"buy-{trigger}"))
    else:
        if trend == "bearish":
            why = "structure is bearish (long-only)"
        else:
            why = "no bullish sweep and no retest of a live bullish zone on this bar"
        reason = f"No confirmed sweep or POI retest yet (structure: {trend})"
        steps.append(_step("DECISION", "wait", f"HOLD — {why}", f"hold-{trend}"))

    return {
        "type": "BUY" if entered else "HOLD", "confidence": confidence, "reason": reason,
        "trigger": trigger, "zone": zone, "invalidation": invalidation, "trend": trend, "steps": steps,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Multi-timeframe additions — BOS / CHoCH, equal highs/lows, premium/discount,
# trend lines. Kept in THIS module so the chart, the scanner, the bot and the
# backtest keep reading one set of detectors (see the note at the top).
# ═══════════════════════════════════════════════════════════════════════════
from .indicators import calc_atr  # noqa: E402


class StructureBreak(TypedDict):
    time: int        # seconds — the candle that CLOSED through the level
    level: float
    from_time: int   # seconds — the swing that was broken
    kind: str        # "BOS" (continuation) | "CHoCH" (change of character)
    dir: str         # "bullish" | "bearish"


def find_structure_breaks(candles: List[dict], lookback: int = 3, max_events: int = 14) -> Dict:
    """Break of Structure / Change of Character, on candle CLOSES.

    Walks the candles once, in order, remembering the newest *confirmed*
    unbroken swing high and swing low (a pivot is only known `lookback` bars
    after it printed, so nothing here peeks at the future). A close above the
    swing high is a bullish break; below the swing low a bearish one. It is a
    BOS when it continues the current trend and a CHoCH when it flips it —
    the first break after a range is called a BOS. `trend` is the direction of
    the most recent break (None if none yet).
    """
    n = len(candles)
    events: List[StructureBreak] = []
    trend: Optional[str] = None
    hi = lo = None  # (price, time_s)
    for i in range(n):
        k = i - lookback
        if k >= lookback:
            c = candles[k]
            win = range(k - lookback, k + lookback + 1)
            if all(candles[j]["high"] < c["high"] for j in win if j != k):
                hi = (c["high"], int(c["time"] / 1000))
            if all(candles[j]["low"] > c["low"] for j in win if j != k):
                lo = (c["low"], int(c["time"] / 1000))
        cur = candles[i]
        t = int(cur["time"] / 1000)
        if hi is not None and cur["close"] > hi[0]:
            events.append({"time": t, "level": hi[0], "from_time": hi[1],
                           "kind": "CHoCH" if trend == "bearish" else "BOS", "dir": "bullish"})
            trend = "bullish"
            hi = None
        elif lo is not None and cur["close"] < lo[0]:
            events.append({"time": t, "level": lo[0], "from_time": lo[1],
                           "kind": "CHoCH" if trend == "bullish" else "BOS", "dir": "bearish"})
            trend = "bearish"
            lo = None
    return {"events": events[-max_events:], "trend": trend}


class EqualLevel(TypedDict):
    kind: str     # "EQH" | "EQL"
    price: float
    t1: int
    t2: int
    swept: bool   # a later candle already ran through it (liquidity taken)


def find_equal_levels(candles: List[dict], swings: List[SwingPoint], tol_atr: float = 0.25, max_each: int = 3) -> List[EqualLevel]:
    """Equal highs / equal lows: two swing points within `tol_atr` x ATR of
    each other — resting stop liquidity just beyond them."""
    if len(swings) < 2 or len(candles) < 20:
        return []
    tol = max(calc_atr(candles) * tol_atr, candles[-1]["close"] * 0.0002)
    out: List[EqualLevel] = []
    for kind, tag in (("high", "EQH"), ("low", "EQL")):
        pts = [s for s in swings if s["kind"] == kind]
        found: List[EqualLevel] = []
        for bi in range(len(pts) - 1, 0, -1):
            b = pts[bi]
            for ai in range(bi - 1, max(-1, bi - 4), -1):
                a = pts[ai]
                if abs(a["price"] - b["price"]) <= tol:
                    level = max(a["price"], b["price"]) if kind == "high" else min(a["price"], b["price"])
                    swept = any(
                        (c["high"] > level + tol * 0.2) if kind == "high" else (c["low"] < level - tol * 0.2)
                        for c in candles if int(c["time"] / 1000) > b["time"]
                    )
                    found.append({"kind": tag, "price": level, "t1": a["time"], "t2": b["time"], "swept": swept})
                    break
            if len(found) >= max_each:
                break
        out.extend(found)
    return out


def premium_discount(candles: List[dict], window: int = 80) -> Optional[Dict]:
    """Dealing range = highest high / lowest low of the last `window` candles.
    Above 50% is premium (sell side of the range), below discount (buy side)."""
    if len(candles) < 10:
        return None
    seg = candles[-window:]
    hi = max(c["high"] for c in seg)
    lo = min(c["low"] for c in seg)
    if hi <= lo:
        return None
    t_hi = next(int(c["time"] / 1000) for c in seg if c["high"] == hi)
    t_lo = next(int(c["time"] / 1000) for c in seg if c["low"] == lo)
    price = candles[-1]["close"]
    pct = (price - lo) / (hi - lo) * 100
    return {
        "high": hi, "low": lo, "eq": (hi + lo) / 2, "pct": pct, "t_high": t_hi, "t_low": t_lo,
        "t_start": int(seg[0]["time"] / 1000),
        "zone": "premium" if pct > 55 else ("discount" if pct < 45 else "equilibrium"),
    }


def find_trendlines(candles: List[dict], swings: List[SwingPoint]) -> List[Dict]:
    """Ascending support through the last two rising swing lows and descending
    resistance through the last two falling swing highs. `broken` once a candle
    has closed through the projected line after the second touch."""
    out: List[Dict] = []
    if len(candles) < 20:
        return out
    step = (candles[-1]["time"] - candles[-2]["time"]) / 1000 or 1

    def build(kind: str, pts: List[SwingPoint]) -> None:
        if len(pts) < 2:
            return
        a, b = pts[-2], pts[-1]
        if b["time"] - a["time"] < step * 4:
            return
        if kind == "support" and not b["price"] > a["price"]:
            return
        if kind == "resistance" and not b["price"] < a["price"]:
            return
        slope = (b["price"] - a["price"]) / (b["time"] - a["time"])
        broken = False
        for c in candles:
            t = int(c["time"] / 1000)
            if t <= b["time"]:
                continue
            line = b["price"] + slope * (t - b["time"])
            if (kind == "support" and c["close"] < line) or (kind == "resistance" and c["close"] > line):
                broken = True
                break
        out.append({"kind": kind, "t1": a["time"], "p1": a["price"], "t2": b["time"], "p2": b["price"],
                    "slope": slope, "broken": broken})

    build("support", [s for s in swings if s["kind"] == "low"])
    build("resistance", [s for s in swings if s["kind"] == "high"])
    return out


def bias_state(analysis: Dict) -> Dict:
    """One word of direction for a timeframe: the last BOS/CHoCH direction,
    vetoed to neutral if the swing labels (HH/HL vs LH/LL) point the other way."""
    bt = (analysis.get("breaks") or {}).get("trend")
    st = (analysis.get("structure") or {}).get("trend")
    opp = {"bullish": "bearish", "bearish": "bullish"}
    if bt in opp and st != opp[bt]:
        bias = bt
    elif bt is None and st in opp:
        bias = st
    else:
        bias = "neutral"
    ev = (analysis.get("breaks") or {}).get("events") or []
    return {"bias": bias, "break_trend": bt, "swing_trend": st,
            "last_break": ev[-1] if ev else None,
            "strength": 2 if (bt == st and bias != "neutral") else (1 if bias != "neutral" else 0)}


def analyze_full(candles: List[dict], window: int = SMC_WINDOW) -> Dict:
    """Everything one timeframe's chart draws AND the multi-timeframe engine
    reasons over (see services/mtf.py): the classic snapshot plus BOS/CHoCH,
    equal highs/lows, premium/discount and trend lines, with larger zone limits
    than the bot-loop's analyze()."""
    candles = candles[-window:]
    if len(candles) < 30:
        return {"empty": True, "atr": 0.0, "order_blocks": [], "fvgs": [], "sd_zones": [],
                "structure": {"swings": [], "trend": "ranging"}, "breaks": {"events": [], "trend": None},
                "sweeps": [], "poi": [], "retests": [], "eq": [], "pd": None, "trendlines": [],
                "amd": None, "ote": None, "breakers": []}
    order_blocks = find_order_blocks(candles, max_blocks=8)
    fvgs = find_fair_value_gaps(candles, max_gaps=10)
    sd_zones = find_supply_demand_zones(candles, max_zones=5)
    all_swings = find_swings(candles)
    label_swings(all_swings)
    structure = find_market_structure(candles)
    sweeps = find_liquidity_sweeps(candles, all_swings[-40:], max_sweeps=8)
    snap = {
        "order_blocks": order_blocks, "fvgs": fvgs, "sd_zones": sd_zones, "structure": structure,
        "sweeps": sweeps,
        "poi": find_points_of_interest(candles, order_blocks, fvgs, sd_zones),
        "retests": find_retests(candles, order_blocks, fvgs, sd_zones),
        "breaks": find_structure_breaks(candles),
        "eq": find_equal_levels(candles, all_swings),
        "pd": premium_discount(candles),
        "trendlines": find_trendlines(candles, all_swings),
        "atr": calc_atr(candles),
    }
    snap["bias"] = bias_state(snap)
    try:                                           # time-based levels / AMD, OTE and breaker blocks
        from . import concepts
        snap["amd"] = concepts.key_levels(candles)
        snap["ote"] = concepts.find_ote(candles, all_swings)
        snap["breakers"] = concepts.find_breakers(candles, order_blocks)
    except Exception:                              # never let an extra concept break the core read
        snap["amd"], snap["ote"], snap["breakers"] = None, None, []
    return snap
