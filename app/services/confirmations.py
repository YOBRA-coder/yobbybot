"""
Confirmation strategies — the classic indicator strategies from the old bot, turned
into directional VOTES on a trade the smart-money engine already wants to take.

They do not generate trades on their own. For a candidate BUY/SELL each approved
strategy answers: does the indicator SUPPORT it, OPPOSE it, or stay NEUTRAL?
The engine requires `supports - opposes >= min_confirms` before a signal can fire.
Enable / disable them (approve them) on the Strategies page; the backtest uses
exactly the same functions, so the live signal and its backtest cannot drift apart.

All of them read only closed candles of the entry timeframe.
"""
from typing import Callable, Dict, List, Optional

from .indicators import calc_bb, calc_ema, calc_macd, calc_rsi

CATALOG: Dict[str, Dict] = {
    "EMA Trend": {
        "desc": "Price above a rising EMA 9 / 21 pair supports longs; below a falling pair supports shorts.",
        "params": {"fast": 9, "slow": 21},
    },
    "RSI Reversal": {
        "desc": "RSI turning up out of weakness (or down out of strength) supports the trade; chasing an overbought/oversold extreme opposes it.",
        "params": {"period": 14, "low": 45, "high": 55, "extreme": 72},
    },
    "MACD Momentum": {
        "desc": "MACD histogram building in the trade's direction supports it; fading against it opposes.",
        "params": {},
    },
    "Bollinger Reaction": {
        "desc": "A rejection back inside the Bollinger band supports a reversal trade; a close stretched beyond the opposite band opposes it.",
        "params": {"period": 20, "std": 2.0},
    },
    "VWAP Bias": {
        "desc": "Price on the right side of the day's anchored VWAP supports the trade.",
        "params": {},
    },
}
NAMES = tuple(CATALOG)


def _v(vote: str, detail: str) -> Dict:
    return {"vote": vote, "detail": detail}


def _ema_trend(cs: List[dict], d: str, p: Dict) -> Dict:
    closes = [c["close"] for c in cs]
    if len(closes) < p["slow"] + 6:
        return _v("neutral", "not enough candles")
    ef, es = calc_ema(closes, p["fast"]), calc_ema(closes, p["slow"])
    px = closes[-1]
    rising = ef[-1] > ef[-4]
    if d == "bullish":
        if px > es[-1] and (ef[-1] >= es[-1] or rising):
            return _v("support", f"price above EMA{p['slow']}, EMA{p['fast']} {'rising' if rising else 'above slow'}")
        if px < es[-1] and ef[-1] < es[-1] and not rising:
            return _v("oppose", f"price below a falling EMA{p['fast']}/{p['slow']} pair")
    else:
        if px < es[-1] and (ef[-1] <= es[-1] or not rising):
            return _v("support", f"price below EMA{p['slow']}, EMA{p['fast']} {'falling' if not rising else 'below slow'}")
        if px > es[-1] and ef[-1] > es[-1] and rising:
            return _v("oppose", f"price above a rising EMA{p['fast']}/{p['slow']} pair")
    return _v("neutral", "EMAs give no clear read")


def _rsi(cs: List[dict], d: str, p: Dict) -> Dict:
    closes = [c["close"] for c in cs]
    per = p["period"]
    if len(closes) < per + 8:
        return _v("neutral", "not enough candles")
    now, prev = calc_rsi(closes, per), calc_rsi(closes[:-1], per)
    window = [calc_rsi(closes[:len(closes) - k], per) for k in range(0, 7)]
    if d == "bullish":
        if now >= p["extreme"]:
            return _v("oppose", f"RSI {now:.0f} already overbought — chasing")
        if min(window) <= p["low"] and now > prev:
            return _v("support", f"RSI {now:.0f} turning up (low {min(window):.0f} within 6 bars)")
    else:
        if now <= 100 - p["extreme"]:
            return _v("oppose", f"RSI {now:.0f} already oversold — chasing")
        if max(window) >= p["high"] and now < prev:
            return _v("support", f"RSI {now:.0f} turning down (high {max(window):.0f} within 6 bars)")
    return _v("neutral", f"RSI {now:.0f}")


def _macd(cs: List[dict], d: str, p: Dict) -> Dict:
    closes = [c["close"] for c in cs]
    if len(closes) < 40:
        return _v("neutral", "not enough candles")
    h0, h1 = calc_macd(closes)["hist"], calc_macd(closes[:-1])["hist"]
    h2 = calc_macd(closes[:-2])["hist"]
    if d == "bullish":
        if h0 > h1 > h2 or (h0 > h1 and h0 > 0):
            return _v("support", "MACD histogram building up")
        if h0 < h1 and h0 < 0:
            return _v("oppose", "MACD histogram fading down below zero")
    else:
        if h0 < h1 < h2 or (h0 < h1 and h0 < 0):
            return _v("support", "MACD histogram building down")
        if h0 > h1 and h0 > 0:
            return _v("oppose", "MACD histogram fading up above zero")
    return _v("neutral", "MACD flat")


def _bollinger(cs: List[dict], d: str, p: Dict) -> Dict:
    closes = [c["close"] for c in cs]
    per = p["period"]
    if len(closes) < per + 4:
        return _v("neutral", "not enough candles")
    bb = calc_bb(closes, per, p["std"])
    c = cs[-1]
    recent = cs[-3:]
    if d == "bullish":
        if c["close"] > bb["upper"]:
            return _v("oppose", "closed above the upper band — stretched")
        prior_lower = calc_bb(closes[:-1], per, p["std"])["lower"]
        if any(x["low"] < calc_bb(closes[:len(closes) - k], per, p["std"])["lower"] for k, x in zip((2, 1, 0), recent)) and c["close"] > prior_lower:
            return _v("support", "rejected back inside the lower band")
    else:
        if c["close"] < bb["lower"]:
            return _v("oppose", "closed below the lower band — stretched")
        if any(x["high"] > calc_bb(closes[:len(closes) - k], per, p["std"])["upper"] for k, x in zip((2, 1, 0), recent)) and c["close"] < calc_bb(closes[:-1], per, p["std"])["upper"]:
            return _v("support", "rejected back inside the upper band")
    return _v("neutral", "inside the bands")


def _vwap(cs: List[dict], d: str, p: Dict) -> Dict:
    if not cs:
        return _v("neutral", "no candles")
    day = int(cs[-1]["time"] / 1000 // 86_400 * 86_400)
    seg = [c for c in cs if c["time"] / 1000 >= day]
    vv = sum(c["volume"] for c in seg)
    if len(seg) < 4 or vv <= 0:
        return _v("neutral", "VWAP not formed yet")
    vw = sum((c["high"] + c["low"] + c["close"]) / 3 * c["volume"] for c in seg) / vv
    px = cs[-1]["close"]
    if (px > vw) == (d == "bullish"):
        return _v("support", f"price {'above' if d == 'bullish' else 'below'} day VWAP")
    return _v("oppose", f"price {'below' if d == 'bullish' else 'above'} day VWAP")


_FN: Dict[str, Callable] = {
    "EMA Trend": _ema_trend, "RSI Reversal": _rsi, "MACD Momentum": _macd,
    "Bollinger Reaction": _bollinger, "VWAP Bias": _vwap,
}


def evaluate(names: List[str], candles: List[dict], direction: str, params: Optional[Dict[str, Dict]] = None) -> Dict:
    """Vote of every named strategy on a `direction` ('bullish' | 'bearish') trade."""
    votes = []
    for n in names:
        fn = _FN.get(n)
        if not fn:
            continue
        p = {**CATALOG[n]["params"], **((params or {}).get(n) or {})}
        try:
            r = fn(candles[-150:], direction, p)
        except Exception as e:  # noqa: BLE001
            r = _v("neutral", f"error: {e}")
        votes.append({"name": n, **r})
    sup = sum(1 for v in votes if v["vote"] == "support")
    opp = sum(1 for v in votes if v["vote"] == "oppose")
    return {"votes": votes, "supports": sup, "opposes": opp, "net": sup - opp}
