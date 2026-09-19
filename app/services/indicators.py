import math
from typing import List


def calc_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    rs = gains / (losses or 0.001)
    return 100 - 100 / (1 + rs)


def calc_ema(data: List[float], period: int) -> List[float]:
    k = 2 / (period + 1)
    ema = [data[0]]
    for v in data[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes: List[float]) -> dict:
    if len(closes) < 26:
        return {"macd": 0, "signal": 0, "hist": 0}
    e12 = calc_ema(closes, 12)
    e26 = calc_ema(closes, 26)
    ml = [a - b for a, b in zip(e12, e26)]
    sl = calc_ema(ml, 9)
    return {"macd": ml[-1], "signal": sl[-1], "hist": ml[-1] - sl[-1]}


def calc_bb(closes: List[float], period: int = 20, std_dev: float = 2.0) -> dict:
    sl = closes[-period:]
    mean = sum(sl) / len(sl)
    std = math.sqrt(sum((x - mean) ** 2 for x in sl) / len(sl))
    return {"upper": mean + std_dev * std, "middle": mean, "lower": mean - std_dev * std}


def calc_atr(candles: List[dict], period: int = 14) -> float:
    """
    Average True Range — the average size of a candle's full move,
    including gaps. Used to size stops relative to what the market is
    ACTUALLY doing on this pair/timeframe right now.

    Why this exists: every preset used a flat percentage stop (default 3%).
    On a 15m chart 3% is an enormous distance — for most majors that's
    hours or days of range — so a bot would enter, then sit in the same
    position for days waiting for price to travel far enough to hit either
    the stop or the 4.5% target. With only one position open at a time,
    that is the direct cause of "each bot has placed 0, 1 or 2 trades in
    5 days": the bots weren't failing to find entries, they were parked in
    a single trade the whole time. An ATR-scaled stop is proportional to
    the timeframe being traded, so positions resolve on a timescale that
    matches the chart the bot is reading, and the bot is free to take the
    next setup.
    """
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"]),
        ))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def generate_signal_from_ta(candles: List[dict], ticker: dict) -> dict:
    price = ticker["price"]
    # No real candle data available right now (e.g. Binance is temporarily
    # unreachable and there's no last-known-real data cached yet for this
    # pair/interval — see fetch_klines). Nothing fabricated to analyze, so
    # return a neutral HOLD instead of crashing on empty indicator math.
    if not candles:
        return {
            "type": "HOLD", "strength": 0, "confidence": 0,
            "targetPrice": price, "stopLoss": price,
            "reason": "No market data available right now",
            "rsi": 50.0, "macd": {"macd": 0, "signal": 0, "hist": 0},
            "bb": {"upper": price, "middle": price, "lower": price},
        }
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]
    rsi = calc_rsi(closes)
    macd = calc_macd(closes)
    bb = calc_bb(closes)
    ema50 = calc_ema(closes, min(50, len(closes)))[-1]
    ema200 = calc_ema(closes, min(200, len(closes)))[-1]
    score = 0.0
    reasons = []

    # BUG FIX ("my signals all bring 70% confidence"): every factor below
    # used to add a FIXED integer (±1 or ±2) the instant its condition was
    # true at all, with no regard for how strongly true it was — RSI at
    # 44.9 scored identically to RSI at 30.1. Two of the most common
    # factors, RSI's 45-55 "momentum" zone and the EMA50/EMA200 trend
    # check, both fire in the overwhelming majority of real market states
    # (the trend check is UNCONDITIONAL — it always contributes ±1 to
    # EVERY evaluation, never zero), and both landed on exactly ±1 far
    # more often than not. ±1 + ±1 = the minimum ±2 needed to leave HOLD at
    # all, which floored confidence at exactly 50+2×10=70 for a huge share
    # of all signals — not a coincidence, a structural certainty of the
    # math. Every branch now adds its old base value PLUS a continuous
    # bonus sized to how far past its own threshold the reading actually
    # is, so two different market states essentially never produce the
    # identical score, and confidence spreads out to reflect that instead
    # of pooling at one number.
    if rsi < 30:
        score += 2 + min(2.5, (30 - rsi) / 10); reasons.append("RSI oversold")
    elif rsi > 70:
        score -= 2 + min(2.5, (rsi - 70) / 10); reasons.append("RSI overbought")
    elif rsi < 45:
        score += 1 + min(1.5, (45 - rsi) / 15); reasons.append("RSI bullish momentum")
    elif rsi > 55:
        score -= 1 + min(1.5, (rsi - 55) / 15); reasons.append("RSI bearish pressure")

    if macd["macd"] > macd["signal"] and macd["hist"] > 0:
        magnitude = min(2.0, abs(macd["hist"]) / price * 10000) if price else 0  # relative, not raw — scale-free across pairs
        score += 2 + magnitude; reasons.append("MACD bullish crossover")
    elif macd["macd"] < macd["signal"]:
        magnitude = min(2.0, abs(macd["hist"]) / price * 10000) if price else 0
        score -= 2 + magnitude; reasons.append("MACD bearish crossover")

    band_width = (bb["upper"] - bb["lower"]) or 1
    if price < bb["lower"]:
        score += 2 + min(2.0, (bb["lower"] - price) / band_width * 5); reasons.append("Price below lower Bollinger band")
    elif price > bb["upper"]:
        score -= 2 + min(2.0, (price - bb["upper"]) / band_width * 5); reasons.append("Price above upper Bollinger band")

    spread_pct = ((ema50 - ema200) / ema200 * 100) if ema200 else 0
    if ema50 > ema200:
        score += 1 + min(1.5, spread_pct * 2); reasons.append("Uptrend confirmed (EMA50 > EMA200)")
    else:
        score -= 1 + min(1.5, abs(spread_pct) * 2); reasons.append("Downtrend (EMA50 < EMA200)")

    avg_vol = sum(volumes[-20:]) / max(1, len(volumes[-20:]))
    if volumes and avg_vol and volumes[-1] > avg_vol * 1.5:
        score += 1 + min(1.5, volumes[-1] / avg_vol - 1.5); reasons.append("Volume breakout")

    change_pct = ticker.get("changePct", 0)
    if change_pct > 3:
        score += 1 + min(1.5, (change_pct - 3) / 3); reasons.append("Strong bullish momentum")
    elif change_pct < -3:
        score -= 1 + min(1.5, (abs(change_pct) - 3) / 3); reasons.append("Strong bearish momentum")

    # BUG FIX / tuning: this used to require a score of ±3 (out of a
    # realistic ±9 range) before ever calling BUY/SELL, which meant most
    # cycles landed in HOLD even when 2 of the ~6 signals below already
    # agreed — "bots ... always hold, want them active ... as long as we
    # get profit can be risked but only properly" means lower the bar to
    # "at least two independent technical factors agree" (±2) rather than
    # requiring three, while keeping the same multi-factor confluence model
    # (still never fires on a single noisy indicator) and the same
    # confidence scaling below, so weaker setups score lower confidence and
    # get filtered out by each bot's own confidence_threshold instead of
    # never being considered at all.
    if score >= 2:
        sig_type = "BUY"
    elif score <= -2:
        sig_type = "SELL"
    else:
        sig_type = "HOLD"

    confidence = round(min(95, 50 + abs(score) * 10))

    if sig_type == "BUY":
        target, stop = price * 1.035, price * 0.975
    elif sig_type == "SELL":
        target, stop = price * 0.965, price * 1.025
    else:
        target, stop = price, price

    return {
        "type": sig_type,
        # `score` is now a float (continuous bonuses above) — round for
        # display since "strength" reads as a small integer count/rating
        # elsewhere (e.g. star ratings), not a raw decimal.
        "strength": round(abs(score)),
        "confidence": confidence,
        "targetPrice": target,
        "stopLoss": stop,
        "reason": " | ".join(reasons),
        "rsi": rsi,
        "macd": macd,
        "bb": bb,
    }
