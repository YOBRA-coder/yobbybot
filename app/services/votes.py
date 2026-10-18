"""
Strategy vote — what OTHER classic strategies say about the same pair, next to the SMC engine.

Six independent strategies each vote BUY (+1), SELL (-1) or NEUTRAL (0) on closed candles:
  EMA trend and SuperTrend (read on the setup timeframe), MACD momentum, RSI, Donchian breakout and
  Bollinger reversion (read on the entry timeframe). SMC's own decision counts as a seventh vote.

Each strategy also gets a short TRACK RECORD on the stored candles — every time it flipped to a new
vote, what did price do over the next 12 bars (after 0.2% round-trip fees)? "Use the best" then
means: weight the strategy with the best recent record 3x. A track record is a description of the
recent past, not a promise — the UI says so.

Everything here is a pure function of candles (no network), so it is unit-tested directly.
"""
from typing import Dict, List, Optional

from .indicators import calc_ema

HORIZON = 12          # bars looked forward when scoring a vote
LOOKBACK = 700        # bars of history used for the track record
MIN_SAMPLES = 12
FEE_PCT = 0.2         # round trip
TF_ROLE = {"ema": "setup", "supertrend": "setup", "macd": "entry", "rsi": "entry", "breakout": "entry", "boll": "entry"}
NAMES = {
    "smc": "SMC top-down", "ema": "EMA trend (20/50/200)", "supertrend": "SuperTrend (10, 3)", "macd": "MACD momentum",
    "rsi": "RSI (14)", "breakout": "Donchian breakout (20)", "boll": "Bollinger reversion (20, 2)",
}


# ── vectorised strategy series: one vote per bar ────────────────────────────
def ema_series(cs: List[dict]) -> List[int]:
    c = [x["close"] for x in cs]
    if len(c) < 55:
        return [0] * len(c)
    e20, e50 = calc_ema(c, 20), calc_ema(c, 50)
    e200 = calc_ema(c, 200) if len(c) >= 200 else None
    out = []
    for i, p in enumerate(c):
        if i < 50:
            out.append(0)
            continue
        up = p > e20[i] > e50[i] and (e200 is None or i < 200 or p > e200[i])
        dn = p < e20[i] < e50[i] and (e200 is None or i < 200 or p < e200[i])
        out.append(1 if up else -1 if dn else 0)
    return out


def macd_series(cs: List[dict]) -> List[int]:
    c = [x["close"] for x in cs]
    if len(c) < 40:
        return [0] * len(c)
    e12, e26 = calc_ema(c, 12), calc_ema(c, 26)
    ml = [a - b for a, b in zip(e12, e26)]
    sg = calc_ema(ml, 9)
    h = [a - b for a, b in zip(ml, sg)]
    out = [0] * len(c)
    for i in range(35, len(c)):
        out[i] = 1 if h[i] > 0 and h[i] >= h[i - 1] else -1 if h[i] < 0 and h[i] <= h[i - 1] else 0
    return out


def _rsi_values(c: List[float], period: int = 14) -> List[float]:
    out = [50.0] * len(c)
    if len(c) <= period:
        return out
    g = l = 0.0
    for i in range(1, period + 1):
        d = c[i] - c[i - 1]
        g, l = g + max(d, 0), l - min(d, 0)
    g, l = g / period, l / period
    out[period] = 100 - 100 / (1 + g / (l or 1e-9))
    for i in range(period + 1, len(c)):
        d = c[i] - c[i - 1]
        g = (g * (period - 1) + max(d, 0)) / period
        l = (l * (period - 1) - min(d, 0)) / period
        out[i] = 100 - 100 / (1 + g / (l or 1e-9))
    return out


def rsi_series(cs: List[dict]) -> List[int]:
    c = [x["close"] for x in cs]
    r = _rsi_values(c)
    out = [0] * len(c)
    for i in range(15, len(c)):
        if r[i] < 25:
            out[i] = 1                                   # oversold bounce
        elif r[i] > 75:
            out[i] = -1                                  # overbought fade
        elif r[i] > 55 and r[i] > r[i - 1]:
            out[i] = 1
        elif r[i] < 45 and r[i] < r[i - 1]:
            out[i] = -1
    return out


def breakout_series(cs: List[dict], n: int = 20) -> List[int]:
    out, state = [0] * len(cs), 0
    for i in range(n, len(cs)):
        hi = max(x["high"] for x in cs[i - n:i])
        lo = min(x["low"] for x in cs[i - n:i])
        mid = (hi + lo) / 2
        p = cs[i]["close"]
        if p > hi:
            state = 1
        elif p < lo:
            state = -1
        elif (state == 1 and p < mid) or (state == -1 and p > mid):
            state = 0
        out[i] = state
    return out


def boll_series(cs: List[dict], n: int = 20, k: float = 2.0) -> List[int]:
    c = [x["close"] for x in cs]
    out, state = [0] * len(c), 0
    for i in range(n, len(c)):
        w = c[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        if c[i] < m - k * sd:
            state = 1
        elif c[i] > m + k * sd:
            state = -1
        elif (state == 1 and c[i] > m) or (state == -1 and c[i] < m):
            state = 0
        out[i] = state
    return out


def supertrend_series(cs: List[dict], n: int = 10, mult: float = 3.0) -> List[int]:
    if len(cs) < n + 2:
        return [0] * len(cs)
    tr = [cs[0]["high"] - cs[0]["low"]] + [
        max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i - 1]["close"]), abs(cs[i]["low"] - cs[i - 1]["close"]))
        for i in range(1, len(cs))
    ]
    atr = [0.0] * len(cs)
    atr[n] = sum(tr[1:n + 1]) / n
    for i in range(n + 1, len(cs)):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    out = [0] * len(cs)
    f_up = f_dn = 0.0
    trend = 1
    for i in range(n, len(cs)):
        mid = (cs[i]["high"] + cs[i]["low"]) / 2
        up, dn = mid + mult * atr[i], mid - mult * atr[i]
        if i == n:
            f_up, f_dn = up, dn
        else:
            f_up = up if (up < f_up or cs[i - 1]["close"] > f_up) else f_up
            f_dn = dn if (dn > f_dn or cs[i - 1]["close"] < f_dn) else f_dn
            if trend == 1 and cs[i]["close"] < f_dn:
                trend = -1
            elif trend == -1 and cs[i]["close"] > f_up:
                trend = 1
        out[i] = trend
    return out


SERIES = {"ema": ema_series, "macd": macd_series, "rsi": rsi_series, "breakout": breakout_series,
          "boll": boll_series, "supertrend": supertrend_series}


def track_record(cs: List[dict], votes: List[int]) -> Dict:
    """How the strategy did each time it flipped to a new vote (entries only, no overlap counting)."""
    n = len(cs)
    lo = max(1, n - LOOKBACK)
    rets: List[float] = []
    i = lo
    while i < n - HORIZON:
        if votes[i] != 0 and votes[i] != votes[i - 1]:
            p0, p1 = cs[i]["close"], cs[i + HORIZON]["close"]
            rets.append(votes[i] * (p1 - p0) / p0 * 100 - FEE_PCT)
            i += HORIZON                       # one position at a time, like the SMC backtest
        else:
            i += 1
    k = len(rets)
    if not k:
        return {"n": 0, "win_rate": None, "avg_ret": None, "ok": False}
    return {"n": k, "win_rate": round(sum(1 for r in rets if r > 0) / k * 100, 1),
            "avg_ret": round(sum(rets) / k, 3), "ok": k >= MIN_SAMPLES}


def _detail(sid: str, v: float, tf: str) -> str:
    side = "bullish" if v > 0 else "bearish" if v < 0 else "neutral"
    t = tf.upper()
    return {
        "ema": f"price vs EMA20/50/200 on {t}: {side}",
        "supertrend": f"SuperTrend flipped {side} on {t}",
        "macd": f"MACD histogram {side} and {'growing' if v else 'flat'} on {t}",
        "rsi": f"RSI {side} on {t}",
        "breakout": f"20-bar channel: {side} on {t}",
        "boll": f"Bollinger band reversion: {side} on {t}",
    }[sid] if v else f"no clear {NAMES[sid].split(' (')[0]} signal on {t}"


def _closed(cs: List[dict], tf_ms: int, now_ms: Optional[float]) -> List[dict]:
    if now_ms is not None and cs and cs[-1]["time"] + tf_ms > now_ms:
        return cs[:-1]
    return cs


def compute(frames: Dict[str, List[dict]], entry_tf: str, setup_tf: str, decision: Dict,
            tf_ms: Dict[str, int], now_ms: Optional[float] = None, weighting: str = "equal") -> Dict:
    """Votes for one pair. `frames` must hold the entry and setup TF candles."""
    tfs = {"entry": entry_tf, "setup": setup_tf}
    series_cs = {role: _closed(frames.get(tf) or [], tf_ms.get(tf, 0), now_ms) for role, tf in tfs.items()}
    rows: List[Dict] = []
    # SMC
    d = decision.get("direction")
    sign = 1 if d == "bullish" else -1 if d == "bearish" else 0
    smc_v = {"SIGNAL": 1.0, "SETUP": 0.5}.get(decision.get("state"), 0.0) * sign
    rows.append({"id": "smc", "name": NAMES["smc"], "tf": f"{decision.get('bias_tf')}→{entry_tf}", "vote": smc_v,
                 "detail": decision.get("watch") or "", "edge": None})
    for sid, role in TF_ROLE.items():
        cs = series_cs[role]
        tf = tfs[role]
        if len(cs) < 40:
            rows.append({"id": sid, "name": NAMES[sid], "tf": tf, "vote": 0, "detail": f"not enough {tf.upper()} candles yet",
                         "edge": {"n": 0, "win_rate": None, "avg_ret": None, "ok": False}})
            continue
        ser = SERIES[sid](cs)
        rows.append({"id": sid, "name": NAMES[sid], "tf": tf, "vote": ser[-1], "detail": _detail(sid, ser[-1], tf),
                     "edge": track_record(cs, ser)})
    ranked = sorted((r for r in rows if r["edge"] and r["edge"]["ok"] and r["edge"]["avg_ret"] > 0),
                    key=lambda r: -r["edge"]["avg_ret"])
    best = ranked[0] if ranked else None
    wsum = tot = 0.0
    for r in rows:
        w = 2.0 if r["id"] == "smc" else 1.0
        if weighting == "best" and best and r["id"] == best["id"]:
            w = 3.0
        r["weight"] = w
        wsum += w
        tot += w * r["vote"]
    score = round(tot / wsum * 100, 1) if wsum else 0.0
    buy = sum(1 for r in rows if r["vote"] > 0)
    sell = sum(1 for r in rows if r["vote"] < 0)
    direction = "bullish" if score >= 20 else "bearish" if score <= -20 else "mixed"
    return {
        "votes": rows, "score": score, "direction": direction, "buy": buy, "sell": sell, "neutral": len(rows) - buy - sell,
        "best": {"id": best["id"], "name": best["name"], "avg_ret": best["edge"]["avg_ret"], "win_rate": best["edge"]["win_rate"],
                 "n": best["edge"]["n"], "vote": best["vote"]} if best else None,
        "weighting": weighting, "horizon": HORIZON,
        "note": "Track record = what price did over the next 12 bars after each flip, after 0.2% fees, on the stored candles. "
                "It describes the recent past, not the future.",
    }


def apply_consensus(dec: Dict, vote: Dict, threshold: float = 20.0) -> Dict:
    """If the SMC engine fired but the vote leans the other way, hold the signal back (never invent one)."""
    if dec.get("type") not in ("BUY", "SELL"):
        return dec
    want = 1 if dec["type"] == "BUY" else -1
    if vote["score"] * want >= threshold:
        return {**dec, "vote_ok": True}
    side = dec["type"]
    return {**dec, "type": "HOLD", "state": "SETUP", "confidence": 0, "vote_ok": False, "vote_blocked": True,
            "watch": f"{side} setup passed every SMC gate, but the strategy vote disagrees ({vote['score']:+.0f}) — waiting for the vote to agree",
            "reason": f"held back by the vote ({vote['score']:+.0f})"}


def quick_agrees(entry_candles: List[dict], side: str, threshold: float = 0.0) -> bool:
    """Backtest helper: do the entry-timeframe strategies lean the same way as `side` on the last bar?"""
    want = 1 if side == "BUY" else -1
    tot = n = 0
    for sid, role in TF_ROLE.items():
        if role != "entry":
            continue
        tot += SERIES[sid](entry_candles)[-1]
        n += 1
    return tot * want / max(1, n) * 100 >= threshold
