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
    score = 0
    reasons = []

    if rsi < 30:
        score += 2; reasons.append("RSI oversold")
    elif rsi > 70:
        score -= 2; reasons.append("RSI overbought")
    elif rsi < 45:
        score += 1; reasons.append("RSI bullish momentum")
    elif rsi > 55:
        score -= 1; reasons.append("RSI bearish pressure")

    if macd["macd"] > macd["signal"] and macd["hist"] > 0:
        score += 2; reasons.append("MACD bullish crossover")
    elif macd["macd"] < macd["signal"]:
        score -= 2; reasons.append("MACD bearish crossover")

    if price < bb["lower"]:
        score += 2; reasons.append("Price below lower Bollinger band")
    elif price > bb["upper"]:
        score -= 2; reasons.append("Price above upper Bollinger band")

    if ema50 > ema200:
        score += 1; reasons.append("Uptrend confirmed (EMA50 > EMA200)")
    else:
        score -= 1; reasons.append("Downtrend (EMA50 < EMA200)")

    avg_vol = sum(volumes[-20:]) / max(1, len(volumes[-20:]))
    if volumes and volumes[-1] > avg_vol * 1.5:
        score += 1; reasons.append("Volume breakout")

    change_pct = ticker.get("changePct", 0)
    if change_pct > 3:
        score += 1; reasons.append("Strong bullish momentum")
    elif change_pct < -3:
        score -= 1; reasons.append("Strong bearish momentum")

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

    confidence = min(95, 50 + abs(score) * 10)

    if sig_type == "BUY":
        target, stop = price * 1.035, price * 0.975
    elif sig_type == "SELL":
        target, stop = price * 0.965, price * 1.025
    else:
        target, stop = price, price

    return {
        "type": sig_type,
        "strength": abs(score),
        "confidence": confidence,
        "targetPrice": target,
        "stopLoss": stop,
        "reason": " | ".join(reasons),
        "rsi": rsi,
        "macd": macd,
        "bb": bb,
    }
