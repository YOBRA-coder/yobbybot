"""
AMD — Accumulation, Manipulation, Distribution (the "power of 3").

  A  ACCUMULATION  price compresses into a range (smart money builds a position)
  M  MANIPULATION  a wick runs the range's high or low and closes back inside — the
                   stop-run / "Judas swing" that pulls retail the wrong way
  D  DISTRIBUTION  displacement out of the far side of the range — the real move

It works on ANY timeframe's candles (`detect_amd`) and on the classic daily cycle
(`session_amd`: Asia range -> London/NY sweep -> expansion).

Direction follows the sweep: a swept LOW then expansion up is a bullish AMD; a
swept HIGH then expansion down is a bearish AMD. Reads only the candles it is given.
"""
from typing import Dict, List, Optional

from .indicators import calc_atr

PHASES = ("NONE", "ACCUMULATION", "MANIPULATION", "DISTRIBUTION")
MIN_RANGE_BARS = 8
MAX_RANGE_ATR = 3.6     # a box wider than this many ATRs is a trend leg, not accumulation


def _empty() -> Dict:
    return {"phase": "NONE", "direction": None, "range": None, "manipulation": None, "distribution": None,
            "text": "No AMD structure on this timeframe", "age_bars": None, "fresh": False}


def _find_box(cs: List[dict], end: int, atr: float) -> Optional[Dict]:
    """Longest compression window ending at index `end` (inclusive) whose total range stays
    under MAX_RANGE_ATR * ATR and that touches both edges at least twice."""
    hi, lo = cs[end]["high"], cs[end]["low"]
    start = end
    k = end - 1
    limit = MAX_RANGE_ATR * atr
    while k >= 0 and end - k < 60:
        nh, nl = max(hi, cs[k]["high"]), min(lo, cs[k]["low"])
        if nh - nl > limit:
            break
        hi, lo, start = nh, nl, k
        k -= 1
    # shrink from the old end until the window is FLAT (no drift) — a trend leg is not accumulation
    for s0 in range(start, end - MIN_RANGE_BARS + 2):
        w = cs[s0:end + 1]
        whi, wlo = max(c["high"] for c in w), min(c["low"] for c in w)
        q = max(2, len(w) // 4)
        drift = abs(sum(c["close"] for c in w[:q]) / q - sum(c["close"] for c in w[-q:]) / q)
        if whi > wlo and drift <= 0.3 * (whi - wlo) and whi - wlo <= limit:
            hi, lo, start = whi, wlo, s0
            break
    else:
        return None
    bars = end - start + 1
    if bars < MIN_RANGE_BARS or hi <= lo:
        return None
    edge = (hi - lo) * 0.22
    top_t = sum(1 for c in cs[start:end + 1] if c["high"] >= hi - edge)
    bot_t = sum(1 for c in cs[start:end + 1] if c["low"] <= lo + edge)
    if top_t < 2 or bot_t < 2:
        return None
    return {"high": hi, "low": lo, "start": start, "end": end, "bars": bars,
            "t0": int(cs[start]["time"] / 1000), "t1": int(cs[end]["time"] / 1000)}


def _avg_body(cs: List[dict], upto: int, n: int = 20) -> float:
    seg = cs[max(0, upto - n):upto] or cs[:1]
    return sum(abs(c["close"] - c["open"]) for c in seg) / len(seg)


def _scan_after(cs: List[dict], box: Dict, atr: float, body_avg: Optional[float] = None) -> Dict:
    """What price did after the range ended: swept an edge and came back (M), then broke out the
    other side (D)?  Returns {phase, direction, manipulation, distribution}."""
    hi, lo, e = box["high"], box["low"], box["end"]
    tol = 0.04 * atr
    body_avg = body_avg if body_avg else _avg_body(cs, e + 1)
    sweep = None
    for i in range(e + 1, len(cs)):
        c = cs[i]
        # a close beyond an edge with no return is a plain breakout — not manipulation
        if sweep is None:
            if c["low"] < lo - tol and c["close"] >= lo + 0.02 * (hi - lo):
                sweep = {"i": i, "side": "low", "wick": c["low"], "level": lo}
            elif c["high"] > hi + tol and c["close"] <= hi - 0.02 * (hi - lo):
                sweep = {"i": i, "side": "high", "wick": c["high"], "level": hi}
            elif c["close"] > hi + tol or c["close"] < lo - tol:
                # confirmed breakout without a stop-run: not AMD
                return {"phase": "NONE"}
        else:
            # keep extending the sweep wick while price is still probing beyond the edge
            if sweep["side"] == "low" and c["low"] < sweep["wick"]:
                sweep["wick"] = c["low"]
            if sweep["side"] == "high" and c["high"] > sweep["wick"]:
                sweep["wick"] = c["high"]
            # invalidation: closes decisively THROUGH the sweep wick = it was a real breakdown
            if sweep["side"] == "low" and c["close"] < sweep["wick"] - 0.15 * atr and c["close"] < lo - 0.5 * atr:
                return {"phase": "NONE"}
            if sweep["side"] == "high" and c["close"] > sweep["wick"] + 0.15 * atr and c["close"] > hi + 0.5 * atr:
                return {"phase": "NONE"}
    if sweep is None:
        return {"phase": "ACCUMULATION"}
    direction = "bullish" if sweep["side"] == "low" else "bearish"
    mid = (hi + lo) / 2
    dist = None
    for i in range(sweep["i"] + 1, len(cs)):
        c = cs[i]
        body = abs(c["close"] - c["open"])
        far = c["close"] > hi if direction == "bullish" else c["close"] < lo
        impulse = (c["close"] > mid and c["close"] > c["open"] and body >= 1.4 * body_avg) if direction == "bullish" else \
                  (c["close"] < mid and c["close"] < c["open"] and body >= 1.4 * body_avg)
        if far or (impulse and (c["close"] > hi - 0.25 * (hi - lo) if direction == "bullish" else c["close"] < lo + 0.25 * (hi - lo))):
            dist = {"i": i, "level": hi if direction == "bullish" else lo}
            break
    res = {"phase": "DISTRIBUTION" if dist else "MANIPULATION", "direction": direction,
           "manipulation": {"time": int(cs[sweep["i"]]["time"] / 1000), "side": sweep["side"],
                            "wick": sweep["wick"], "level": sweep["level"]}}
    if dist:
        res["distribution"] = {"time": int(cs[dist["i"]]["time"] / 1000), "level": dist["level"],
                               "target": (hi + (hi - lo)) if direction == "bullish" else (lo - (hi - lo))}
    return res


def detect_amd(candles: List[dict], atr: Optional[float] = None, lookback: int = 140, max_age_bars: int = 45) -> Dict:
    """Most recent AMD structure on this timeframe (closed candles). `max_age_bars` — how many bars
    after the range ended it still counts as live."""
    cs = candles[-lookback:]
    if len(cs) < MIN_RANGE_BARS + 6:
        return _empty()
    atr = atr or calc_atr(cs) or 0
    if atr <= 0:
        return _empty()
    last = len(cs) - 1
    best: Optional[Dict] = None
    # the box must END recently; scan candidate end bars from newest to oldest
    for end in range(last, max(MIN_RANGE_BARS, last - max_age_bars), -1):
        # a box end is only meaningful if the NEXT bar (if any) is not itself inside the box
        box = _find_box(cs, end, atr)
        if not box:
            continue
        after = _scan_after(cs, box, atr)
        if after["phase"] == "NONE":
            continue
        if after["phase"] == "ACCUMULATION" and end != last:
            # still range-bound after `end` but the window just did not extend — covered by a later end
            continue
        best = {**after, "box": box}
        break
    if best is None:
        return _empty()
    box = best["box"]
    phase = best["phase"]
    d = best.get("direction")
    out = {
        "phase": phase, "direction": d,
        "range": {"high": box["high"], "low": box["low"], "t0": box["t0"], "t1": box["t1"], "bars": box["bars"]},
        "manipulation": best.get("manipulation"), "distribution": best.get("distribution"),
        "age_bars": last - box["end"], "fresh": (last - box["end"]) <= 30,
    }
    side = (best.get("manipulation") or {}).get("side")
    if phase == "ACCUMULATION":
        out["text"] = f"Accumulation: {box['bars']}-bar range {box['low']:.6g}–{box['high']:.6g} — expect a stop-run of one side, then the real move the other way"
    elif phase == "MANIPULATION":
        out["text"] = (f"Manipulation: {side} of the range swept ({best['manipulation']['wick']:.6g}) and rejected — "
                       f"{d} distribution expected")
    else:
        out["text"] = (f"Distribution: {d} expansion after the {side} sweep — "
                       f"{'target ' + format(best['distribution']['target'], '.6g') if best.get('distribution') else ''}")
    return out


def session_amd(candles: List[dict], now_s: float, asia_end_h: float = 8.0, london_start_h: float = 7.0) -> Dict:
    """The daily cycle on lower-timeframe candles: Asia (00:00–08:00 UTC) builds the range,
    London / New York sweeps one side of it, then price expands the other way."""
    day = int(now_s // 86_400 * 86_400)
    asia = [c for c in candles if day <= c["time"] / 1000 < day + asia_end_h * 3600]
    after = [c for c in candles if c["time"] / 1000 >= day + london_start_h * 3600 and c["time"] / 1000 <= now_s]
    if len(asia) < 6:
        return _empty()
    hi, lo = max(c["high"] for c in asia), min(c["low"] for c in asia)
    atr = calc_atr(candles[-100:]) or (hi - lo) / 4
    box = {"high": hi, "low": lo, "t0": int(asia[0]["time"] / 1000), "t1": int(asia[-1]["time"] / 1000), "bars": len(asia)}
    if (hi - lo) > 7 * atr:
        return {**_empty(), "range": box, "text": "Asia range too wide to treat as accumulation"}
    if not after:
        return {**_empty(), "phase": "ACCUMULATION", "range": box, "age_bars": 0, "fresh": True,
                "text": f"Asia accumulation {lo:.6g}–{hi:.6g} — watch London for the sweep"}
    cs = after
    # reuse the sweep/distribution scan on the post-Asia candles
    scan = _scan_after([{"time": 0, "open": 0, "high": hi, "low": lo, "close": (hi + lo) / 2}] + cs,
                       {"high": hi, "low": lo, "end": 0}, atr, _avg_body(asia, len(asia)))
    if scan["phase"] in ("NONE", "ACCUMULATION"):
        return {**_empty(), "phase": "ACCUMULATION" if scan["phase"] == "ACCUMULATION" else "NONE", "range": box,
                "fresh": True, "age_bars": 0, "kind": "session",
                "text": f"Asia accumulation {lo:.6g}–{hi:.6g} — no sweep yet" if scan["phase"] == "ACCUMULATION"
                else "Asia range broke out without a stop-run (no AMD)"}
    out = {"phase": scan["phase"], "direction": scan["direction"], "range": box, "kind": "session",
           "manipulation": scan.get("manipulation"), "distribution": scan.get("distribution"),
           "age_bars": 0, "fresh": True}
    side = scan["manipulation"]["side"]
    out["text"] = (f"Session AMD: Asia {side} swept in London/NY → {scan['direction']} "
                   f"{'distribution under way' if scan['phase'] == 'DISTRIBUTION' else 'distribution expected'}")
    return out
