"""
Extra smart-money / ICT concepts that sit on top of smc.py:

  * AMD (Accumulation – Manipulation – Distribution, a.k.a. Power of 3) and the key time-based levels —
    the Asia range, previous day / week high & low, the day and week open. Crypto trades 24/7 so the
    "sessions" are fixed UTC windows (Asia 00–07, London 07–12, New York 12:30–20) rather than exchange hours;
    that makes AMD a decent guide in crypto, a little looser than in forex where the session close is a hard edge.
  * OTE — the optimal trade entry: the 62–79% retracement of the latest impulse leg.
  * Breaker blocks — an order block that failed (price closed through it) and now acts from the other side.

All functions are pure and only read the candles they are given (no look-ahead).
"""
import statistics
from datetime import datetime, timezone
from typing import Dict, List, Optional

ASIA_END_H = 7.0
LONDON_END_H = 12.0
NY_START_H = 12.5
NY_END_H = 20.0
DAY_S = 86400


def _bar_s(candles: List[dict]) -> float:
    d = [(candles[i + 1]["time"] - candles[i]["time"]) / 1000 for i in range(min(len(candles) - 1, 40))]
    d = [x for x in d if x > 0]
    return statistics.median(d) if d else 3600.0


def _hl(cs: List[dict]):
    return (max(c["high"] for c in cs), min(c["low"] for c in cs)) if cs else (None, None)


def key_levels(candles: List[dict]) -> Optional[Dict]:
    """AMD read + key levels for the day of the LAST candle (UTC). Needs candles of 1h or smaller for the Asia
    range; on larger bars only the day / week levels are returned."""
    if len(candles) < 10:
        return None
    bar = _bar_s(candles)
    last = candles[-1]
    t_last = last["time"] / 1000
    price = last["close"]
    day0 = int(t_last // DAY_S * DAY_S)
    dt = datetime.fromtimestamp(t_last, tz=timezone.utc)
    week0 = day0 - dt.weekday() * DAY_S
    sec = lambda c: c["time"] / 1000  # noqa: E731

    today = [c for c in candles if sec(c) >= day0]
    prev_day = [c for c in candles if day0 - DAY_S <= sec(c) < day0]
    this_week = [c for c in candles if sec(c) >= week0]
    prev_week = [c for c in candles if week0 - 7 * DAY_S <= sec(c) < week0]
    out: Dict = {"day_start": day0, "price": price, "bar_s": bar, "asia": None, "london": None,
                 "pdh": None, "pdl": None, "pwh": None, "pwl": None, "day_open": None, "week_open": None,
                 "expected": None, "swept": None, "phase": None, "clock_phase": None, "text": "", "weak": False}
    if today and sec(today[0]) <= day0 + bar:
        out["day_open"] = today[0]["open"]
    if this_week and sec(this_week[0]) <= week0 + max(bar, 14400):
        out["week_open"] = this_week[0]["open"]
    if prev_day and sec(prev_day[0]) <= day0 - DAY_S + max(bar, 14400):
        out["pdh"], out["pdl"] = _hl(prev_day)
    if prev_week and len(prev_week) >= 5:
        out["pwh"], out["pwl"] = _hl(prev_week)

    h = (t_last - day0) / 3600
    out["clock_phase"] = ("ACCUMULATION" if h < ASIA_END_H else "MANIPULATION" if h < LONDON_END_H
                          else "DISTRIBUTION" if h < NY_END_H else "LATE")
    if bar > 3600:          # 4h / 1d bars can't resolve the Asia range
        out["text"] = f"Levels only ({_pretty(bar)} bars)"
        return out
    asia = [c for c in today if sec(c) < day0 + ASIA_END_H * 3600]
    if len(asia) < max(2, int(1.5 * 3600 / bar)):
        out["text"] = "Asia range not available yet"
        out["phase"] = out["clock_phase"]
        return out
    ah, al = _hl(asia)
    complete = t_last >= day0 + ASIA_END_H * 3600
    out["asia"] = {"high": ah, "low": al, "t0": day0, "t1": day0 + int(ASIA_END_H * 3600), "complete": complete}
    if not complete:
        out["phase"] = "ACCUMULATION"
        out["text"] = f"Accumulation: Asia range {al:.6g}–{ah:.6g} is still forming — wait for the London sweep"
        return out
    after = [c for c in today if sec(c) >= day0 + ASIA_END_H * 3600]
    ld = [c for c in after if sec(c) < day0 + LONDON_END_H * 3600]
    if ld:
        out["london"] = {"high": max(c["high"] for c in ld), "low": min(c["low"] for c in ld)}
    hi_w = max((c["high"] for c in after), default=ah)
    lo_w = min((c["low"] for c in after), default=al)
    out["high_wick"], out["low_wick"] = hi_w, lo_w
    swept_hi, swept_lo = hi_w > ah, lo_w < al
    if swept_hi and swept_lo:
        out["swept"], out["phase"] = "both", "MANIPULATION"
        out["text"] = "Both sides of the Asia range were swept — choppy, no clean AMD read"
    elif swept_hi:
        out["swept"] = "high"
        if price < ah:
            out["expected"], out["phase"] = "bearish", "DISTRIBUTION"
            out["text"] = f"Manipulation: Asia high {ah:.6g} swept and price is back inside → distribution expected DOWN"
        else:
            out["phase"], out["weak"] = "BREAKOUT", True
            out["text"] = f"Price is holding above the Asia high {ah:.6g} — breakout, not a Judas swing (yet)"
    elif swept_lo:
        out["swept"] = "low"
        if price > al:
            out["expected"], out["phase"] = "bullish", "DISTRIBUTION"
            out["text"] = f"Manipulation: Asia low {al:.6g} swept and price is back inside → distribution expected UP"
        else:
            out["phase"], out["weak"] = "BREAKOUT", True
            out["text"] = f"Price is holding below the Asia low {al:.6g} — breakdown, not a Judas swing (yet)"
    else:
        out["phase"] = "MANIPULATION" if h < LONDON_END_H else "RANGE"
        out["text"] = (f"Waiting for manipulation: price is still inside the Asia range {al:.6g}–{ah:.6g}" if h < LONDON_END_H
                       else "The Asia range was never swept today — no AMD read")
    if out["expected"] and h >= NY_END_H:
        out["weak"] = True            # late in the day the move has usually played out
    return out


def _pretty(bar_s: float) -> str:
    return f"{int(bar_s // 3600)}h" if bar_s >= 3600 else f"{int(bar_s // 60)}m"


def find_ote(candles: List[dict], swings: List[Dict]) -> Optional[Dict]:
    """62–79% retracement zone of the latest impulse leg. After an up-leg the zone is a LONG area (bullish);
    after a down-leg it is a SHORT area (bearish)."""
    if len(swings) < 2 or not candles:
        return None
    a, b = swings[-2], swings[-1]
    if a["kind"] == b["kind"]:
        return None
    price = candles[-1]["close"]
    if b["kind"] == "high":                      # low -> high: pullback down is the long entry
        lo, hi = a["price"], b["price"]
        if hi <= lo:
            return None
        r = hi - lo
        top, bottom = hi - 0.62 * r, hi - 0.79 * r
        direction = "bullish"
        pct = (hi - price) / r * 100
    else:                                        # high -> low: pullback up is the short entry
        hi, lo = a["price"], b["price"]
        if hi <= lo:
            return None
        r = hi - lo
        bottom, top = lo + 0.62 * r, lo + 0.79 * r
        direction = "bearish"
        pct = (price - lo) / r * 100
    return {"dir": direction, "top": top, "bottom": bottom, "from": a["price"], "to": b["price"],
            "t0": a["time"], "t1": b["time"], "retrace_pct": round(pct, 1),
            "inside": bottom <= price <= top}


def find_breakers(candles: List[dict], order_blocks: List[Dict], max_n: int = 4) -> List[Dict]:
    """An order block that price closed THROUGH flips polarity: a failed bullish OB becomes a bearish breaker
    (resistance on the retest) and vice versa. It stays valid until price closes back through the far side."""
    out: List[Dict] = []
    for ob in order_blocks:
        if not ob.get("mitigated"):
            continue
        formed = ob.get("formed_at", ob["time"])
        brk_i = None
        for i, c in enumerate(candles):
            if c["time"] / 1000 <= formed:
                continue
            if (ob["kind"] == "bullish" and c["close"] < ob["bottom"]) or (ob["kind"] == "bearish" and c["close"] > ob["top"]):
                brk_i = i
                break
        if brk_i is None:
            continue
        later = candles[brk_i + 1:]
        if ob["kind"] == "bullish":
            dead = any(c["close"] > ob["top"] for c in later)       # reclaimed -> not a breaker any more
            kind = "bearish"
        else:
            dead = any(c["close"] < ob["bottom"] for c in later)
            kind = "bullish"
        if dead:
            continue
        out.append({"top": ob["top"], "bottom": ob["bottom"], "kind": kind, "time": int(candles[brk_i]["time"] / 1000),
                    "formed_at": int(candles[brk_i]["time"] / 1000), "strength": ob.get("strength", 1.0),
                    "origin_kind": ob["kind"], "src": "BRK"})
    last = candles[-1]["close"] if candles else 0
    out.sort(key=lambda z: min(abs(z["top"] - last), abs(z["bottom"] - last)))
    return out[:max_n]
