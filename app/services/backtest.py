"""
Real backtest engine.

The original endpoint returned `random.random()` dressed up as results.
This replays one of a handful of clearly-defined, deterministic strategies
against real historical candles and logs every simulated trade.

Presets are used (rather than parsing an arbitrary free-text "execution"
string) because that's a well-scoped, testable way to give non-technical
users real strategy logic without needing them to write rules themselves —
the `execution` field on a Strategy is a human-readable label of which of
these presets it maps to, and doubles as the description shown in the UI.
"""
import math
from typing import List, TypedDict

from .indicators import calc_bb, calc_ema, calc_macd, calc_rsi

PRESETS = ("RSI Scalper", "EMA Cross", "MACD Divergence", "Bollinger Squeeze", "Grid Trading")

# Tunable knobs per preset, seeded onto a new Strategy row at creation time
# and read back here on every run. Before this, every "RSI Scalper"
# strategy anyone created ran the exact same hardcoded 30/55/-3% rule no
# matter what name or description they gave it — the Parameters panel on
# the Strategy page existed and could edit numbers, but there was nothing
# for it to seed on a fresh strategy (parameters defaulted to `{}`), so it
# never actually appeared, and even if it had, the backtest/live engines
# below ignored it and used their own hardcoded constants regardless. Two
# "RSI Scalper" strategies with different names were never actually
# different strategies. Now they can be — e.g. a 25-oversold scalper vs a
# 35-oversold one are genuinely different rules with different backtest
# results, which is what "adding a NEW strategy, a real one" means.
DEFAULT_PARAMS = {
    "RSI Scalper": {"rsiOversold": 30, "rsiExit": 55, "stopPct": 3},
    "EMA Cross": {"emaFast": 12, "emaSlow": 26, "stopPct": 3},
    "MACD Divergence": {"stopPct": 3},
    "Bollinger Squeeze": {"bbPeriod": 20, "bbStdDev": 2, "stopPct": 3},
    "Grid Trading": {"gridPct": 1.5, "stopPct": 3},
}


class BacktestTrade(TypedDict):
    side: str
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    pnl: float
    pnl_pct: float
    result: str  # "WIN" | "LOSS"


def _close_position(candle, entry_price, entry_time, side) -> BacktestTrade:
    exit_price = candle["close"]
    pnl_pct = (exit_price - entry_price) / entry_price if side == "BUY" else (entry_price - exit_price) / entry_price
    return {
        "side": side, "entry_time": entry_time, "entry_price": entry_price,
        "exit_time": candle["time"], "exit_price": exit_price,
        "pnl": pnl_pct, "pnl_pct": pnl_pct * 100,
        "result": "WIN" if pnl_pct >= 0 else "LOSS",
    }


def _run_rsi_scalper(candles: List[dict], params: dict) -> List[BacktestTrade]:
    """Long-only mean reversion: enter when RSI dips under the oversold
    threshold AND is already turning back up, exit on RSI recovering above
    the exit threshold or the stop.

    BUG FIX: this used to enter the instant RSI dropped below the oversold
    line, with no confirmation the drop was actually done. On a genuine
    reversal that's fine, but a big chunk of the time an RSI that's just
    crossed under 30 is in the middle of a real downtrend and keeps making
    new lows — a classic "catch the falling knife" entry that mostly just
    walks straight into the stop-loss. Requiring RSI to have ticked up from
    the prior candle (rsi > prev_rsi) means it only buys once selling
    pressure has visibly started to ease, not on the way down.
    """
    oversold = params.get("rsiOversold", 30)
    exit_rsi = params.get("rsiExit", 55)
    stop_pct = params.get("stopPct", DEFAULT_PARAMS["RSI Scalper"]["stopPct"]) / 100
    trades: List[BacktestTrade] = []
    closes = [c["close"] for c in candles]
    position = None  # (entry_price, entry_time)
    prev_rsi = None
    for i in range(20, len(candles)):
        rsi = calc_rsi(closes[: i + 1])
        if position is None and rsi < oversold and prev_rsi is not None and rsi > prev_rsi:
            position = (candles[i]["close"], candles[i]["time"])
        elif position is not None:
            entry_price, entry_time = position
            change = (candles[i]["close"] - entry_price) / entry_price
            if rsi > exit_rsi or change <= -stop_pct:
                trades.append(_close_position(candles[i], entry_price, entry_time, "BUY"))
                position = None
        prev_rsi = rsi
    return trades


def _run_ema_cross(candles: List[dict], params: dict) -> List[BacktestTrade]:
    """Trend-following: long on the fast EMA crossing above the slow EMA
    while the slow EMA is itself already rising, flat on the reverse cross
    or the protective stop.

    BUG FIX #1 (quality filter): a raw fast/slow cross fires just as often
    in a flat, choppy market as in a real trend — the two lines are
    constantly grazing each other with no directional conviction behind
    it, and each one of those is a trade that's likely to reverse again
    within a few bars. Requiring the SLOW EMA to have net-risen over the
    last few candles (es[i] > es[i-3]) filters out crosses that happen
    while the underlying trend is still flat or falling, keeping only
    crosses that occur inside an actual uptrend.

    BUG FIX #2 (missing stop): this never modeled a stop-loss at all — it
    only exited on the bearish cross, which lags price by definition (the
    slow EMA has to catch up first). That let backtested trades ride out
    drawdowns no live bot actually would, since position_monitor_loop
    enforces a real stop-loss on every live trade regardless of what the
    strategy's own exit condition says. Without this, a strategy's
    backtest could show a healthy win rate while its live version — hit by
    stops the backtest never modeled — did noticeably worse. Adding the
    same stopPct exit here makes the backtest an honest preview of live
    behavior instead of an optimistic one.
    """
    fast, slow = int(params.get("emaFast", 12)), int(params.get("emaSlow", 26))
    stop_pct = params.get("stopPct", DEFAULT_PARAMS["EMA Cross"]["stopPct"]) / 100
    trades: List[BacktestTrade] = []
    closes = [c["close"] for c in candles]
    ef, es = calc_ema(closes, fast), calc_ema(closes, slow)
    position = None
    for i in range(slow + 3, len(candles)):
        bullish = ef[i] > es[i]
        prev_bullish = ef[i - 1] > es[i - 1]
        slow_rising = es[i] > es[i - 3]
        if position is None and bullish and not prev_bullish and slow_rising:
            position = (candles[i]["close"], candles[i]["time"])
        elif position is not None:
            entry_price, entry_time = position
            change = (candles[i]["close"] - entry_price) / entry_price
            if (not bullish and prev_bullish) or change <= -stop_pct:
                trades.append(_close_position(candles[i], entry_price, entry_time, "BUY"))
                position = None
    return trades


def _run_macd_divergence(candles: List[dict], params: dict) -> List[BacktestTrade]:
    """Enter long on MACD line crossing above signal line WHILE the MACD
    line itself is above zero (a cross that confirms an already-bullish
    regime), exit on cross below (or the stop).

    BUG FIX: a histogram flipping positive with the MACD line still deep
    below zero is usually just a dead-cat bounce inside an ongoing
    downtrend, not a real reversal — same "catch the falling knife"
    problem fixed on the other mean-reversion-ish presets. Requiring
    macd_line > 0 at the cross means it only fires once the shorter EMA has
    actually recovered past the longer one in absolute terms, not merely
    stopped falling as fast.
    """
    stop_pct = params.get("stopPct", 3) / 100
    trades: List[BacktestTrade] = []
    closes = [c["close"] for c in candles]
    position = None
    prev_hist = None
    for i in range(27, len(candles)):
        m = calc_macd(closes[: i + 1])
        hist = m["hist"]
        if prev_hist is not None:
            if position is None and prev_hist <= 0 < hist and m["macd"] > 0:
                position = (candles[i]["close"], candles[i]["time"])
            elif position is not None:
                entry_price, entry_time = position
                change = (candles[i]["close"] - entry_price) / entry_price
                if (prev_hist >= 0 > hist) or change <= -stop_pct:
                    trades.append(_close_position(candles[i], entry_price, entry_time, "BUY"))
                    position = None
        prev_hist = hist
    return trades


def _run_bollinger_squeeze(candles: List[dict], params: dict) -> List[BacktestTrade]:
    """Long when price closes below the lower band AND that candle is
    already green (a bullish reaction off the band, not still falling
    through it), exit at the middle band or the protective stop.

    BUG FIX (entry): entering the instant price touched the lower band,
    with no check on which way that candle actually closed, bought
    straight into ongoing breakdowns just as often as genuine oversold
    bounces — same "falling knife" problem as the RSI Scalper fix above.
    Requiring the entry candle to close green (close > open) means there
    was at least some buying pressure right at the point of entry.

    BUG FIX (missing stop): this had no stop-loss at all — only the
    middle-band exit — so backtested trades could ride out a much bigger
    drawdown than any live trade ever would, since position_monitor_loop
    always enforces a real stop. Adding the same stopPct exit here keeps
    the backtest honest about what live trading will actually look like.
    """
    period = int(params.get("bbPeriod", 20))
    std_dev = params.get("bbStdDev", 2.0)
    stop_pct = params.get("stopPct", DEFAULT_PARAMS["Bollinger Squeeze"]["stopPct"]) / 100
    trades: List[BacktestTrade] = []
    closes = [c["close"] for c in candles]
    position = None
    for i in range(period, len(candles)):
        bb = calc_bb(closes[: i + 1], period, std_dev)
        price = candles[i]["close"]
        if position is None and price < bb["lower"] and price > candles[i]["open"]:
            position = (price, candles[i]["time"])
        elif position is not None:
            entry_price, entry_time = position
            change = (price - entry_price) / entry_price
            if price >= bb["middle"] or change <= -stop_pct:
                trades.append(_close_position(candles[i], entry_price, entry_time, "BUY"))
                position = None
    return trades


def _run_grid_trading(candles: List[dict], params: dict) -> List[BacktestTrade]:
    """Simplified grid: buy when price drops gridPct% below a 20-period SMA
    and that candle closed green, sell when it recovers gridPct% above
    that same reference, or the protective stop is hit. See the Bollinger
    Squeeze fix above for why both the green-candle confirmation and the
    stop-loss matter here too."""
    grid_pct = params.get("gridPct", 1.5) / 100
    stop_pct = params.get("stopPct", DEFAULT_PARAMS["Grid Trading"]["stopPct"]) / 100
    trades: List[BacktestTrade] = []
    closes = [c["close"] for c in candles]
    position = None
    for i in range(20, len(candles)):
        sma = sum(closes[i - 20:i]) / 20
        price = candles[i]["close"]
        if position is None and price <= sma * (1 - grid_pct) and price > candles[i]["open"]:
            position = (price, candles[i]["time"])
        elif position is not None:
            entry_price, entry_time = position
            change = (price - entry_price) / entry_price
            if price >= sma * (1 + grid_pct) or change <= -stop_pct:
                trades.append(_close_position(candles[i], entry_price, entry_time, "BUY"))
                position = None
    return trades


RUNNERS = {
    "RSI Scalper": _run_rsi_scalper,
    "EMA Cross": _run_ema_cross,
    "MACD Divergence": _run_macd_divergence,
    "Bollinger Squeeze": _run_bollinger_squeeze,
    "Grid Trading": _run_grid_trading,
}


def run_backtest(strategy_name: str, candles: List[dict], starting_capital: float = 1000.0, parameters: dict = None) -> dict:
    runner = RUNNERS.get(strategy_name, _run_ema_cross)
    params = {**DEFAULT_PARAMS.get(strategy_name, {}), **(parameters or {})}
    trades = runner(candles, params) if len(candles) > 30 else []

    equity = starting_capital
    equity_curve = [equity]
    for t in trades:
        equity *= (1 + t["pnl"])
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100 if peak else 0)

    wins = [t for t in trades if t["result"] == "WIN"]
    returns = [t["pnl"] for t in trades]
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
        sharpe = (mean / std) * math.sqrt(len(returns)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "profit": equity - starting_capital,
        "trades": len(trades),
        "winRate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "sharpeRatio": sharpe,
        "maxDrawdown": max_dd,
        "tradeLog": trades,
    }


def run_confluence_backtest(strategy_names: List[str], candles: List[dict], starting_capital: float = 1000.0, min_agree: int | None = None) -> dict:
    """
    Historical counterpart to evaluate_confluence_live — same "N of these
    must agree" rule, but walked bar-by-bar over real history so a
    Confluence combo can actually be backtested before running real (or
    even demo) capital through it, same as any single preset can. Lets
    "which strategy is best" be answered with real numbers instead of a
    guess, for combos too.

    Entry: at each bar (after the first 30, same warm-up as every single-
    preset runner), evaluate_preset_live is run for every strategy in
    `strategy_names` against only the candles up to and including that bar
    (no lookahead) — enters once `min_agree` of them signal BUY in the
    same bar, combining their target/stop exactly like the live engine
    does (avg target, tightest/max stop).

    Exit: target/stop hit — deliberately NOT each preset's own reversal
    rule, because that's also how the live system actually manages an
    open confluence trade (position_monitor_loop is target/stop based,
    not preset-specific), so the backtest reflects the same exit logic
    the live bot would actually use.
    """
    required = min_agree if min_agree and min_agree > 0 else len(strategy_names)
    required = min(required, len(strategy_names))

    trades: List[BacktestTrade] = []
    if len(candles) > 30:
        position = None  # {"entry_price", "entry_time", "target", "stop"}
        for i in range(30, len(candles)):
            window = candles[: i + 1]
            candle = candles[i]
            if position is None:
                agreed = []
                for name in strategy_names:
                    result = evaluate_preset_live(name, window)
                    if result and result["type"] == "BUY":
                        agreed.append(result)
                if len(agreed) >= required:
                    position = {
                        "entry_price": candle["close"],
                        "entry_time": candle["time"],
                        "target": sum(r["targetPrice"] for r in agreed) / len(agreed),
                        "stop": max(r["stopLoss"] for r in agreed),
                    }
            else:
                hit_target = candle["high"] >= position["target"]
                hit_stop = candle["low"] <= position["stop"]
                if hit_target or hit_stop:
                    # Conservative when a single volatile bar clears both
                    # levels in the same candle: assume the stop was hit
                    # first, same worst-case assumption a real position
                    # monitor has to make from OHLC data alone (it doesn't
                    # know the intra-candle order either).
                    exit_price = position["stop"] if hit_stop else position["target"]
                    entry_price = position["entry_price"]
                    pnl_pct = (exit_price - entry_price) / entry_price
                    trades.append({
                        "side": "BUY", "entry_time": position["entry_time"], "entry_price": entry_price,
                        "exit_time": candle["time"], "exit_price": exit_price,
                        "pnl": pnl_pct, "pnl_pct": pnl_pct * 100,
                        "result": "WIN" if pnl_pct >= 0 else "LOSS",
                    })
                    position = None

    equity = starting_capital
    equity_curve = [equity]
    for t in trades:
        equity *= (1 + t["pnl"])
        equity_curve.append(equity)

    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100 if peak else 0)

    wins = [t for t in trades if t["result"] == "WIN"]
    returns = [t["pnl"] for t in trades]
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        std = math.sqrt(sum((r - mean) ** 2 for r in returns) / len(returns))
        sharpe = (mean / std) * math.sqrt(len(returns)) if std > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "profit": equity - starting_capital,
        "trades": len(trades),
        "winRate": (len(wins) / len(trades) * 100) if trades else 0.0,
        "sharpeRatio": sharpe,
        "maxDrawdown": max_dd,
        "tradeLog": trades,
    }


def evaluate_confluence_live(strategy_names: List[str], candles: List[dict], min_agree: int | None = None):
    """
    "can i create a bot that confirms from all or selected strategies" —
    runs evaluate_preset_live for every strategy in `strategy_names`
    against the SAME candle set and only signals BUY once at least
    `min_agree` of them independently agree in the same cycle (default:
    ALL of them, i.e. the strictest/highest-conviction setting — pass a
    lower number, e.g. 1, for an "ANY of these" mode that trades more
    often since it doesn't wait for every strategy to line up).

    All 5 presets are long-only (see evaluate_preset_live), so "agree"
    here only ever means "independently signaled BUY" — there's no SELL
    case to reconcile.
    """
    required = min_agree if min_agree and min_agree > 0 else len(strategy_names)
    required = min(required, len(strategy_names))

    agreed, held = [], []
    for name in strategy_names:
        result = evaluate_preset_live(name, candles)
        if result and result["type"] == "BUY":
            agreed.append((name, result))
        else:
            held.append(name)

    if len(agreed) < required:
        names_str = ", ".join(strategy_names)
        return {
            "type": "HOLD", "strength": 0, "confidence": 0,
            "targetPrice": candles[-1]["close"] if candles else 0,
            "stopLoss": candles[-1]["close"] if candles else 0,
            "reason": f"Confluence ({names_str}): {len(agreed)}/{required} required agree — " +
                      (f"waiting on {', '.join(held)}" if held else "no agreement yet"),
        }

    # Combine the agreeing strategies' own targets/stops rather than
    # picking just one — average target (a representative expected move),
    # and the TIGHTEST stop among them (max, since all stops sit below a
    # long entry — the highest such value is the nearest one) so the
    # combined trade is never riskier than any single strategy that fired
    # would have been on its own.
    avg_target = sum(r["targetPrice"] for _, r in agreed) / len(agreed)
    tightest_stop = max(r["stopLoss"] for _, r in agreed)
    # Confidence scales with how much of the panel agrees, not just a flat
    # 75 like a single preset — 2/2 agreeing reads more confident than a
    # bare majority does, capped the same as every other engine's output.
    confidence = min(95, 60 + round(70 * len(agreed) / len(strategy_names)))
    agreed_names = ", ".join(n for n, _ in agreed)
    return {
        "type": "BUY", "strength": len(agreed), "confidence": confidence,
        "targetPrice": avg_target, "stopLoss": tightest_stop,
        "reason": f"Confluence: {len(agreed)}/{len(strategy_names)} strategies agree ({agreed_names})",
    }


def evaluate_preset_live(strategy_name: str, candles: List[dict], parameters: dict = None):
    """
    Live, point-in-time version of a preset's ENTRY rule — the exact same
    condition its backtest runner enters on above (now honoring the same
    tunable `parameters`, so a bot running a strategy actually reflects
    whatever thresholds that strategy was tuned/backtested with, not the
    preset's raw defaults), evaluated against only the candles available
    "now" (no lookahead).

    This exists because bot_simulation_loop previously ignored a bot's
    chosen strategy entirely and always ran the generic TA signal engine
    (generate_signal_from_ta) instead — so backtesting "RSI Scalper" and
    then running a bot on "RSI Scalper" had zero connection to each other;
    the bot's actual behavior was identical no matter which strategy (or
    which of your own custom ones) you picked. This function is what lets
    the bot loop actually run the selected preset.

    Exit is intentionally NOT handled here — that stays with
    position_monitor_loop's stop-loss/take-profit (and now optional
    max-hold-time) checks, same as every other trade in the system,
    manual or bot-owned.

    Returns None for anything that isn't one of the five built-in presets
    — i.e. a user's own custom strategy — so the caller falls back to the
    general TA engine, since a free-text custom strategy has no
    structured, directly-runnable rule.
    """
    if strategy_name not in PRESETS or len(candles) < 30:
        return None

    params = {**DEFAULT_PARAMS.get(strategy_name, {}), **(parameters or {})}
    closes = [c["close"] for c in candles]
    price = closes[-1]
    entered, reason = False, ""

    if strategy_name == "RSI Scalper":
        rsi = calc_rsi(closes)
        prev_rsi = calc_rsi(closes[:-1])
        oversold = params.get("rsiOversold", 30)
        # BUG FIX: matches the same "must be turning back up, not just
        # below the line" confirmation as the backtest runner above — see
        # its docstring. Without this, the live bot entered on every single
        # cycle RSI was oversold, including deep into an ongoing drop,
        # which is why an RSI Scalper bot could rack up loss after loss:
        # it kept buying dips in a falling market with nothing checking
        # whether the fall had actually paused.
        entered = rsi < oversold and rsi > prev_rsi
        reason = (
            f"RSI {rsi:.1f} — below oversold threshold {oversold} and turning up"
            if entered else f"RSI {rsi:.1f} — below oversold threshold {oversold} but still falling"
        )
    elif strategy_name == "EMA Cross":
        fast, slow = int(params.get("emaFast", 12)), int(params.get("emaSlow", 26))
        ef, es = calc_ema(closes, fast), calc_ema(closes, slow)
        # BUG FIX: matches the backtest runner's "slow EMA must itself be
        # rising" filter — see its docstring. Without it, the live bot
        # entered on every marginal cross even inside a flat, choppy
        # market, which is the main way an EMA Cross bot whipsaws into
        # repeated small losses.
        slow_rising = len(es) > 3 and es[-1] > es[-4]
        entered = ef[-1] > es[-1] and ef[-2] <= es[-2] and slow_rising
        reason = (
            f"EMA{fast} crossed above EMA{slow} in a rising trend" if entered
            else f"EMA{fast}/EMA{slow} cross condition not confirmed"
        )
    elif strategy_name == "MACD Divergence":
        m_now = calc_macd(closes)
        hist_now, hist_prev = m_now["hist"], calc_macd(closes[:-1])["hist"]
        # BUG FIX: matches the backtest runner's "MACD line must already be
        # above zero" filter — see its docstring. Filters out crosses that
        # are really just a bounce inside a deeper downtrend.
        entered = hist_prev <= 0 < hist_now and m_now["macd"] > 0
        reason = "MACD line crossed above signal in bullish territory" if entered else "MACD cross condition not confirmed"
    elif strategy_name == "Bollinger Squeeze":
        period, std_dev = int(params.get("bbPeriod", 20)), params.get("bbStdDev", 2.0)
        bb = calc_bb(closes, period, std_dev)
        entered = price < bb["lower"] and price > candles[-1]["open"]
        reason = f"Price closed below lower Bollinger band ({period}, {std_dev}σ) on a bullish candle"
    elif strategy_name == "Grid Trading":
        grid_pct = params.get("gridPct", 1.5)
        sma = sum(closes[-20:]) / 20
        entered = price <= sma * (1 - grid_pct / 100) and price > candles[-1]["open"]
        reason = f"Price {grid_pct}% below 20-period SMA on a bullish candle"

    if not entered:
        return {
            "type": "HOLD", "strength": 0, "confidence": 0,
            "targetPrice": price, "stopLoss": price,
            "reason": f'"{strategy_name}" — entry condition not met',
        }

    # All presets are long-only (matches the backtest runners above). Stop
    # uses the strategy's own tuned stopPct where it has one, so tuning
    # that parameter changes live risk sizing too.
    # BUG FIX (defaults mismatch): this fallback (2%) didn't match
    # DEFAULT_PARAMS' fallback (3%) for the same "stopPct" key — a strategy
    # row with no explicit stopPct saved would backtest with a 3% stop but
    # trade live with a 2% one, a tighter stop than it was ever actually
    # tuned/tested against.
    stop_pct = params.get("stopPct", DEFAULT_PARAMS.get(strategy_name, {}).get("stopPct", 3)) / 100
    # BUG FIX (reward:risk): target used to be a flat 3% no matter how wide
    # or tight the stop was — pair that with a 3% stop and every trade
    # risks exactly as much as it can win, meaning even a strategy that's
    # right MORE than it's wrong can still lose money once fees are
    # included, since a full win only just covers a full loss. Scaling the
    # target to 1.5x the stop distance means winners are worth more than
    # losers by design, so a strategy doesn't need a coin-flip-beating win
    # rate just to break even. This does NOT make any strategy "guaranteed"
    # profitable — no rule can promise that, since it still depends on
    # price actually reaching target more than 40% of the time — but it
    # removes a stacked-against-you payoff structure that was independent
    # of how good the entries were.
    return {
        "type": "BUY", "strength": 2, "confidence": 75,
        "targetPrice": price * (1 + stop_pct * 1.5), "stopLoss": price * (1 - stop_pct),
        "reason": f'"{strategy_name}": {reason}',
    }
