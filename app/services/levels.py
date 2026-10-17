"""
Key levels — the reference prices smart money trades around.

Built from only two candle series so it is cheap to compute on every scan and on
every bar of a backtest:

  * 1h candles  -> today's open / high / low, Asia / London / New York session
                   ranges, weekly + monthly opens, anchored VWAP
  * 1d candles  -> previous day, previous week, previous month high / low / close

Crypto trades 24/7, so every period is cut on UTC boundaries: the day opens at
00:00 UTC, the week on Monday 00:00 UTC, the month on the 1st.

Each level is {id, label, price, kind, group, t, swept, active}
  kind   high | low | open | close | vwap | round
  t      second the line starts at on the chart
  swept  price has already traded through it since it formed (a swept high/low
         is spent liquidity — it is not a target any more)

`liquidity_above/below` return only UNSWEPT highs/lows — the stops resting in
the trade's path that price is drawn to, and that a trade has to clear.
"""
import bisect
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

DAY = 86_400
# UTC hour windows. London and New York overlap by design (12:00-16:00).
SESSIONS: Dict[str, Tuple[float, float]] = {
    "Asia": (0.0, 8.0),
    "London": (7.0, 16.0),
    "New York": (12.0, 21.0),
}
SESSION_KEY = {"Asia": "ASIA", "London": "LON", "New York": "NY"}


def _day_start(ts: float) -> int:
    return int(ts // DAY * DAY)


def _week_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return _day_start(ts) - d.weekday() * DAY


def _month_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())


def _prev_month_start(ts: float) -> int:
    d = datetime.fromtimestamp(_month_start(ts) - 1, tz=timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())


def session_status(now_s: float) -> List[Dict]:
    """Open/closed state of each session at `now_s` (UTC) plus the next change."""
    d = datetime.fromtimestamp(now_s, tz=timezone.utc)
    h = d.hour + d.minute / 60 + d.second / 3600
    out = []
    for name, (a, b) in SESSIONS.items():
        is_open = a <= h < b
        if is_open:
            change_s = (b - h) * 3600
        else:
            change_s = ((a - h) % 24) * 3600
        out.append({
            "name": name, "open": is_open, "start_utc": a, "end_utc": b,
            "changes_in_s": int(change_s), "kill_zone": name in ("London", "New York"),
            "weekend": d.weekday() >= 5,
        })
    return out


def _slice(cs: List[dict], times_s: List[float], t0: float, t1: float) -> List[dict]:
    """Candles with open time in [t0, t1) — `times_s` is the sorted open-time list in seconds."""
    i = bisect.bisect_left(times_s, t0)
    j = bisect.bisect_left(times_s, t1)
    return cs[i:j]


def _hl(cs: List[dict]) -> Optional[Tuple[float, float]]:
    if not cs:
        return None
    return max(c["high"] for c in cs), min(c["low"] for c in cs)


def _lvl(lid: str, label: str, price: float, kind: str, group: str, t: float, swept: bool = False,
         active: bool = False) -> Dict:
    return {"id": lid, "label": label, "price": price, "kind": kind, "group": group, "t": int(t),
            "swept": swept, "active": active}


def build_levels(h1: List[dict], d1: List[dict], now_s: Optional[float] = None) -> List[Dict]:
    """All time-anchored key levels as of `now_s` (default: the last 1h candle's close).
    `h1` / `d1` are CLOSED candles, oldest first, time in ms."""
    if not h1:
        return []
    if now_s is None:
        now_s = h1[-1]["time"] / 1000 + 3600
    h1t = [c["time"] / 1000 for c in h1]
    d1t = [c["time"] / 1000 for c in d1]
    price = h1[-1]["close"]
    out: List[Dict] = []

    today = _day_start(now_s)
    wk = _week_start(now_s)
    mo = _month_start(now_s)

    # ── daily ────────────────────────────────────────────────────────────────
    today_h1 = _slice(h1, h1t, today, now_s + 1)
    prev_d = _slice(d1, d1t, today - DAY, today)
    if prev_d:
        p = prev_d[-1]
        t = p["time"] / 1000
        out.append(_lvl("PDH", "PDH", p["high"], "high", "daily", t, bool(today_h1) and max(c["high"] for c in today_h1) > p["high"]))
        out.append(_lvl("PDL", "PDL", p["low"], "low", "daily", t, bool(today_h1) and min(c["low"] for c in today_h1) < p["low"]))
        out.append(_lvl("PDC", "PDC", p["close"], "close", "daily", t))
    if today_h1:
        out.append(_lvl("DO", "Day open", today_h1[0]["open"], "open", "open", today))
        hl = _hl(today_h1)
        out.append(_lvl("DH", "Day high", hl[0], "high", "daily", today, False, True))
        out.append(_lvl("DL", "Day low", hl[1], "low", "daily", today, False, True))
        # anchored VWAP from the UTC day open
        pv = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in today_h1)
        vv = sum(c["volume"] for c in today_h1)
        if vv > 0:
            out.append(_lvl("VWAP", "VWAP", pv / vv, "vwap", "vwap", today))

    # ── weekly ───────────────────────────────────────────────────────────────
    wk_h1 = _slice(h1, h1t, wk, now_s + 1)
    prev_w = _slice(d1, d1t, wk - 7 * DAY, wk)
    if prev_w:
        hl = _hl(prev_w)
        t = prev_w[0]["time"] / 1000
        out.append(_lvl("PWH", "PWH", hl[0], "high", "weekly", t, bool(wk_h1) and max(c["high"] for c in wk_h1) > hl[0]))
        out.append(_lvl("PWL", "PWL", hl[1], "low", "weekly", t, bool(wk_h1) and min(c["low"] for c in wk_h1) < hl[1]))
    if wk_h1:
        out.append(_lvl("WO", "Week open", wk_h1[0]["open"], "open", "open", wk))

    # ── monthly ──────────────────────────────────────────────────────────────
    mo_h1 = _slice(h1, h1t, mo, now_s + 1)
    prev_m = _slice(d1, d1t, _prev_month_start(now_s), mo)
    if len(prev_m) >= 10:
        hl = _hl(prev_m)
        t = prev_m[0]["time"] / 1000
        out.append(_lvl("PMH", "PMH", hl[0], "high", "monthly", t, bool(mo_h1) and max(c["high"] for c in mo_h1) > hl[0]))
        out.append(_lvl("PML", "PML", hl[1], "low", "monthly", t, bool(mo_h1) and min(c["low"] for c in mo_h1) < hl[1]))
    if mo_h1:
        out.append(_lvl("MO", "Month open", mo_h1[0]["open"], "open", "open", mo))

    # ── sessions: the latest window of each (active or finished) ─────────────
    for name, (a, b) in SESSIONS.items():
        key = SESSION_KEY[name]
        w0 = today + a * 3600
        w1 = today + b * 3600
        if now_s < w0:            # today's window has not started — use yesterday's
            w0 -= DAY
            w1 -= DAY
        active = w0 <= now_s < w1
        cs = _slice(h1, h1t, w0, min(w1, now_s + 1))
        hl = _hl(cs)
        if not hl:
            continue
        after = _slice(h1, h1t, w1, now_s + 1) if not active else []
        hi_swept = bool(after) and max(c["high"] for c in after) > hl[0]
        lo_swept = bool(after) and min(c["low"] for c in after) < hl[1]
        out.append(_lvl(f"{key}_H", f"{name} H", hl[0], "high", "session", w0, hi_swept, active))
        out.append(_lvl(f"{key}_L", f"{name} L", hl[1], "low", "session", w0, lo_swept, active))

    # ── psychological round numbers around price ─────────────────────────────
    out.extend(round_levels(price, now_s))
    return out


def round_levels(price: float, now_s: float = 0, per_side: int = 2) -> List[Dict]:
    if price <= 0:
        return []
    mag = 10 ** math.floor(math.log10(price))
    step = mag / 10
    base = math.floor(price / step) * step
    out = []
    for k in range(per_side):
        for lvl in (base - k * step, base + (k + 1) * step):
            if lvl > 0:
                out.append(_lvl(f"RN{lvl:.8g}", f"{lvl:,.8g}", lvl, "round", "round", now_s - 6 * 3600))
    return out


def liquidity_above(levels: List[Dict], price: float) -> List[Tuple[float, str]]:
    """Unswept highs above price, nearest first — buy-side liquidity."""
    xs = [(l["price"], l["label"]) for l in levels if l["kind"] == "high" and not l["swept"] and l["price"] > price]
    return sorted(xs, key=lambda x: x[0])


def liquidity_below(levels: List[Dict], price: float) -> List[Tuple[float, str]]:
    xs = [(l["price"], l["label"]) for l in levels if l["kind"] == "low" and not l["swept"] and l["price"] < price]
    return sorted(xs, key=lambda x: -x[0])


def levels_in_zone(levels: List[Dict], bottom: float, top: float, tol: float) -> List[Dict]:
    """Key levels sitting inside (or within `tol` of) a zone — confluence for an order block / FVG."""
    return [l for l in levels if l["kind"] != "round" and bottom - tol <= l["price"] <= top + tol]


def key_level_sweep(levels: List[Dict], candles: List[dict], direction: str, lookback: int = 24) -> Optional[Dict]:
    """A recent wick through a key level that closed back on the other side — the stop-run
    smart money needs before it reverses. direction 'bullish' = a LOW was swept (sell-side
    liquidity taken), 'bearish' = a HIGH was swept."""
    want = "low" if direction == "bullish" else "high"
    recent = candles[-lookback:]
    best = None
    for l in levels:
        if l["kind"] != want or l["group"] == "round":
            continue
        for k in range(len(recent) - 1, -1, -1):
            c = recent[k]
            if int(c["time"] / 1000) <= l["t"] + 3600 and l["active"]:
                continue
            if direction == "bullish":
                hit = c["low"] < l["price"] and c["close"] > l["price"]
                wick = c["low"]
            else:
                hit = c["high"] > l["price"] and c["close"] < l["price"]
                wick = c["high"]
            if hit:
                cand = {"level": l["price"], "label": l["label"], "wick": wick, "time": int(c["time"] / 1000), "kind": direction}
                if best is None or cand["time"] > best["time"]:
                    best = cand
                break
    return best


def nearest(levels: List[Dict], price: float, n: int = 3) -> Dict:
    """Closest unswept levels above and below price (for the UI's key-level strip)."""
    above = sorted((l for l in levels if l["price"] > price and not l["swept"] and l["kind"] != "round"), key=lambda l: l["price"])[:n]
    below = sorted((l for l in levels if l["price"] < price and not l["swept"] and l["kind"] != "round"), key=lambda l: -l["price"])[:n]
    return {"above": above, "below": below}
