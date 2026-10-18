import asyncio
import re
import time
from typing import Dict, List

import aiohttp
from fastapi import APIRouter, HTTPException

from ..config import DEMO_MODE, PAIRS
from ..services import candle_store, engine_config, mtf, scanner, smc, votes
from ..services.market import fetch_binance_tickers, fetch_history, fetch_klines, fetch_order_book, live_tickers

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/tickers")
async def get_tickers():
    async with aiohttp.ClientSession() as s:
        return await fetch_binance_tickers(s)


@router.get("/klines/{symbol}")
async def get_klines(symbol: str, interval: str = "1h", limit: int = 1000, before: int | None = None):
    # `before` (ms epoch) pages backwards for older history — the chart
    # calls this with the timestamp of its oldest loaded candle when the
    # user scrolls back near the edge of what's loaded, so history keeps
    # extending instead of hitting a hard wall at the first ~100 bars.
    async with aiohttp.ClientSession() as s:
        return await fetch_klines(s, symbol, interval, limit, end_time=before)


@router.get("/smc/{symbol}")
async def get_smc(symbol: str, interval: str = "1h", limit: int = smc.SMC_WINDOW):
    """Everything the chart draws for Smart Money Concepts — order blocks,
    fair value gaps, supply/demand zones, swing structure (HH/HL/LH/LL) and
    trend, liquidity sweeps, points of interest and retests — PLUS the
    strategy's own verdict on the newest bar (`decision`, with the step-by-
    step working the bot logs).

    All of it comes from `smc.analyze()` / `smc.evaluate_entry()`, the same
    two functions the Smart Money Concepts bot strategy and its backtest run,
    over the same `SMC_WINDOW` candles. So what's drawn is what the bot saw
    and `decision` is what it would do — not a lookalike computed elsewhere.
    """
    async with aiohttp.ClientSession() as s:
        candles = await fetch_klines(s, symbol, interval, limit)
    if not candles:
        return {
            "orderBlocks": [], "fvgs": [], "sdZones": [], "structure": {"swings": [], "trend": "ranging"},
            "sweeps": [], "poi": [], "retests": [], "decision": None,
        }

    snap = smc.analyze(candles)
    decision = smc.evaluate_entry(candles, snap)

    # smc.py's internal dicts use snake_case (fill_pct) like the rest of
    # this backend's Python; the frontend interfaces expect camelCase,
    # matching every other API response here.
    def _to_camel(g: dict) -> dict:
        out = dict(g)
        out["fillPct"] = out.pop("fill_pct")
        return out

    return {
        "orderBlocks": snap["order_blocks"],
        "fvgs": [_to_camel(g) for g in snap["fvgs"]],
        "sdZones": snap["sd_zones"],
        "structure": snap["structure"],
        "sweeps": snap["sweeps"],
        "poi": snap["poi"],
        "retests": snap["retests"],
        "decision": decision,
        "candles": len(candles),
        "lastTime": int(candles[-1]["time"] / 1000),
    }


@router.get("/orderbook/{symbol}")
async def get_order_book(symbol: str, limit: int = 20):
    async with aiohttp.ClientSession() as s:
        return await fetch_order_book(s, symbol, limit)


# ═══ Multi-timeframe top-down SMC ═══════════════════════════════════════════
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,15}$")


def _check_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if not _SYMBOL_RE.match(symbol):
        raise HTTPException(400, "Invalid symbol")
    return symbol


def _check_engine(sens: str, weighting: str):
    if sens not in mtf.SENSITIVITY:
        raise HTTPException(400, f"sens must be one of: {', '.join(mtf.SENSITIVITY)}")
    if weighting not in ("equal", "best"):
        raise HTTPException(400, "weighting must be equal or best")


def _check_mode(mode: str, entry_tf: str, min_agree: float):
    if mode not in mtf.MODES:
        raise HTTPException(400, f"mode must be one of: {', '.join(mtf.MODES)}")
    if entry_tf not in mtf.ENTRY_TFS:
        raise HTTPException(400, f"entry_tf must be one of: {', '.join(mtf.ENTRY_TFS)}")
    if not 40 <= min_agree <= 100:
        raise HTTPException(400, "min_agree must be between 40 and 100")


@router.get("/mtf/{symbol}")
async def get_mtf(symbol: str, mode: str = "combined", entry_tf: str = "15m", min_agree: float = mtf.DEFAULT_MIN_AGREE, tfs: str = "", bars: int = 300,
                  sens: str = "balanced", consensus: bool = False, weighting: str = "equal", profile: bool = True):
    """The top-down read for one pair: bias (4H) -> setup (1H) -> trigger (entry TF), plus one chart
    panel per timeframe in `tfs` (default 4h,1h,<entry_tf>) holding the candles and every SMC concept
    detected on that timeframe — order blocks, FVGs, supply/demand, POI, BOS/CHoCH, HH/HL/LH/LL swings,
    liquidity sweeps, equal highs/lows, retests, trend lines, premium/discount.

    mode: intraday (4h/1h/entry) | scalp (1h/15m/entry) | combined (both, the stronger wins)."""
    symbol = _check_symbol(symbol)
    _check_mode(mode, entry_tf, min_agree)
    _check_engine(sens, weighting)
    panel_tfs = [t for t in (tfs.split(",") if tfs else ["4h", "1h", entry_tf]) if t in mtf.TF_MS][:6]
    if not panel_tfs:
        raise HTTPException(400, "tfs has no valid timeframe")
    need = list(dict.fromkeys(mtf.needed_timeframes(mode, entry_tf) + panel_tfs))
    async with aiohttp.ClientSession() as s:
        frames = await scanner.load_frames(s, symbol, need)
    missing = [t for t in need if not frames.get(t)]
    if missing:
        return {
            "symbol": symbol, "ok": False, "demo": DEMO_MODE,
            "error": f"No candles for {', '.join(missing)} — the exchange is unreachable and nothing is stored yet. "
                     f"Check GET /market/store, or start the backend with CRYPTO_DEMO=1 to test the UI offline.",
        }
    now_ms = time.time() * 1000
    sens, min_agree, custom, prof = scanner.apply_profile(symbol, sens, min_agree, engine_config.custom_opts(engine_config.get()), profile)
    opts = mtf.opts_for(sens, custom)

    def run_engine():
        res = mtf.evaluate_all(frames, mode, entry_tf, min_agree, now_ms, None, opts)
        return res, scanner.finalize(res, frames, now_ms, consensus, weighting)
    result, vote = await asyncio.to_thread(run_engine)
    bars = min(max(bars, 100), 1500)
    # the engine reads ~300 bars; the chart can show more history when asked (stored candles beyond that)
    long_frames = {}
    if bars > scanner.FRAME_LIMIT:
        for tf in panel_tfs:
            deep = await asyncio.to_thread(candle_store.load, symbol, tf, bars)
            if len(deep) > len(frames[tf]):
                long_frames[tf] = deep

    def build_panels():
        out = []
        for tf in panel_tfs:
            an = result["analysis"].get(tf)
            if an is None or an.get("empty"):
                an = smc.analyze_full(frames[tf])
            out.append(mtf.panel_payload(tf, long_frames.get(tf, frames[tf]), an, limit=bars))
        return out

    panels = await asyncio.to_thread(build_panels)
    ec = frames.get(entry_tf) or next(iter(frames.values()))
    lag_min = max(0.0, (now_ms - ec[-1]["time"]) / 60000 - mtf.TF_MS.get(entry_tf, 60000) / 60000)
    tick = live_tickers.get(symbol) or {}
    return {
        "ok": True, "symbol": symbol, "mode": mode, "entry_tf": entry_tf, "min_agree": min_agree,
        "demo": DEMO_MODE and not tick, "serverTime": int(now_ms / 1000),
        "price": tick.get("price") or ec[-1]["close"], "changePct": tick.get("changePct"),
        "staleMinutes": round(lag_min, 1),
        "decision": result["decision"], "styles": result["styles"], "panels": panels, "vote": vote,
        "counter": result.get("counter"), "profile": prof,
        "sens": sens, "consensus": consensus,
        "styleInfo": {k: {"bias_tf": v["bias_tf"], "setup_tf": v["setup_tf"], "entry_tfs": list(v["entry_tfs"]),
                           "label": v["label"]} for k, v in mtf.STYLES.items()},
    }


@router.get("/overview")
async def get_overview(mode: str = "combined", entry_tf: str = "15m", min_agree: float = mtf.DEFAULT_MIN_AGREE,
                       sens: str = "balanced", consensus: bool = False, weighting: str = "equal", profile: bool = True):
    """One row per pair: bias, setup state (SIGNAL / SETUP / WATCH / STAND_ASIDE), agreement, what to wait for.
    Cached ~45 s; the background scanner keeps the default settings warm."""
    _check_mode(mode, entry_tf, min_agree)
    _check_engine(sens, weighting)
    ov = await scanner.overview(mode, entry_tf, min_agree, 45, sens, consensus, weighting, profile)
    return {"rows": ov["rows"], "updated": int(ov["ts"]), "demo": DEMO_MODE and not live_tickers}


@router.get("/entry-scan/{symbol}")
async def entry_scan(symbol: str, mode: str = "combined", min_agree: float = mtf.DEFAULT_MIN_AGREE, sens: str = "balanced",
                     consensus: bool = False, weighting: str = "equal", profile: bool = True):
    """Which entry timeframe (1m…30m) is ready right now for this pair — 'find one faster'."""
    symbol = _check_symbol(symbol)
    _check_mode(mode, "15m", min_agree)
    _check_engine(sens, weighting)
    return await scanner.entry_scan(symbol, mode, min_agree, sens, consensus, weighting, use_profile=profile)


@router.get("/engine")
async def get_engine():
    """Stored engine settings + the sensitivity presets (labels) for the settings screen."""
    return {"config": await asyncio.to_thread(engine_config.get),
            "presets": {k: {"min_agree": v["min_agree"], "label": v["label"]} for k, v in mtf.SENSITIVITY.items()}}


@router.get("/mtf-signals")
async def get_mtf_signals(limit: int = 80):
    """Signals the scanner has fired, with outcome (OPEN / WIN / LOSS / EXPIRED) and overall stats."""
    await asyncio.to_thread(scanner.resolve_signals)
    return await asyncio.to_thread(scanner.list_signals, min(max(limit, 1), 300))


@router.get("/mtf-signals/{signal_id}/preview")
async def signal_preview(signal_id: int):
    """Candles around one stored signal (entry timeframe) so the Signals page can draw it:
    ~60 bars of context, the entry bar, and everything after up to the exit (or now)."""
    data = await asyncio.to_thread(scanner.signal_preview, signal_id)
    if not data:
        raise HTTPException(404, "signal not found")
    return data


@router.get("/news")
async def get_news(symbol: str = "", limit: int = 30):
    """Latest crypto headlines (public RSS feeds, cached 5 min). `symbol` (e.g. BTCUSDT) filters to that coin."""
    from ..services import news
    return await news.get_news(symbol.upper(), min(max(limit, 1), 60))


@router.get("/mtf/optimize/{symbol}")
async def mtf_optimize(symbol: str, style: str = "intraday", entry_tf: str = "15m", bars: int = 3000,
                       fee_pct: float = 0.1, slippage_pct: float = 0.0):
    """Search the best engine settings for ONE pair on its stored history. The history is split: settings are
    chosen on the first ~65% and re-tested on the untouched last ~35%; only settings positive on both are
    recommended. Heavy (tens of seconds) — runs in a thread. Apply the result with PUT /settings/engine/profile/{symbol}."""
    from ..services import optimizer
    symbol = _check_symbol(symbol)
    if style not in mtf.STYLES:
        raise HTTPException(400, f"style must be one of: {', '.join(mtf.STYLES)}")
    _check_mode(style, entry_tf, mtf.DEFAULT_MIN_AGREE)
    if not (0 <= fee_pct <= 1 and 0 <= slippage_pct <= 1):
        raise HTTPException(400, "fee_pct/slippage_pct must be 0-1")
    bars = min(max(bars, 800), 8000)
    et = mtf._entry_for(style, entry_tf)
    frames = await _bt_frames(symbol, style, et, bars)
    custom = engine_config.custom_opts(engine_config.get())
    try:
        res = await asyncio.to_thread(optimizer.optimize, frames, style, et, fee_pct, slippage_pct, custom)
    except ValueError as e:
        raise HTTPException(400, str(e))
    res["symbol"] = symbol
    res["demo"] = DEMO_MODE and not live_tickers
    return res


async def _bt_frames(symbol: str, style: str, et: str, bars: int) -> Dict[str, List[dict]]:
    cfg = mtf.STYLES[style]
    span_ms = bars * mtf.TF_MS[et]
    counts = {
        et: bars,
        cfg["setup_tf"]: int(span_ms / mtf.TF_MS[cfg["setup_tf"]]) + 320,
        cfg["bias_tf"]: int(span_ms / mtf.TF_MS[cfg["bias_tf"]]) + 320,
    }
    async with aiohttp.ClientSession() as s:
        got = await asyncio.gather(*(fetch_history(s, symbol, tf, min(n, 40000)) for tf, n in counts.items()))
    frames = dict(zip(counts.keys(), got))
    if any(len(v) < 100 for v in frames.values()):
        raise HTTPException(503, "Not enough history available (exchange unreachable and the candle store is thin)")
    return frames


@router.get("/mtf/backtest/{symbol}")
async def mtf_backtest(symbol: str, style: str = "intraday", entry_tf: str = "15m", bars: int = 1500,
                       min_agree: float = mtf.DEFAULT_MIN_AGREE, long_only: bool = False,
                       sens: str = "balanced", rr: float = 0, sl_buffer: float = -1, fee_pct: float = 0.1,
                       slippage_pct: float = 0.0, min_grade: str = "D", breakeven_at_r: float = 0.0,
                       max_hold_bars: int = 300, killzone_only: bool = False, gate_sweep: bool = False,
                       consensus: bool = False, risk_guard: int = -1, max_chase_atr: float = 0, min_room_rr: float = 0,
                       max_risk_pct: float = 0):
    """Replay the top-down engine on history (no look-ahead; fees + slippage included). Heavy: runs in a thread.
    Advanced knobs: rr (target in R, 0 = style default), sl_buffer (x ATR, -1 = default), min_grade, breakeven_at_r
    (move stop to entry after +N R), killzone_only, gate_sweep (require a liquidity sweep), consensus (the entry-TF
    strategies must lean the same way)."""
    symbol = _check_symbol(symbol)
    if style not in mtf.STYLES:
        raise HTTPException(400, f"style must be one of: {', '.join(mtf.STYLES)}")
    _check_mode(style, entry_tf, min_agree)
    _check_engine(sens, "equal")
    if min_grade not in mtf.GRADE_RANK:
        raise HTTPException(400, "min_grade must be one of: " + ", ".join(mtf.GRADE_RANK))
    if not (0 <= fee_pct <= 1 and 0 <= slippage_pct <= 1 and 0 <= breakeven_at_r <= 5 and 10 <= max_hold_bars <= 2000):
        raise HTTPException(400, "fee_pct/slippage_pct must be 0-1, breakeven_at_r 0-5, max_hold_bars 10-2000")
    bars = min(max(bars, 300), 20000)
    cfg = mtf.STYLES[style]
    et = mtf._entry_for(style, entry_tf)
    span_ms = bars * mtf.TF_MS[et]
    counts = {
        et: bars,
        cfg["setup_tf"]: int(span_ms / mtf.TF_MS[cfg["setup_tf"]]) + 320,
        cfg["bias_tf"]: int(span_ms / mtf.TF_MS[cfg["bias_tf"]]) + 320,
    }
    async with aiohttp.ClientSession() as s:
        got = await asyncio.gather(*(fetch_history(s, symbol, tf, min(n, 40000)) for tf, n in counts.items()))
    frames = dict(zip(counts.keys(), got))
    if any(len(v) < 100 for v in frames.values()):
        raise HTTPException(503, "Not enough history available (exchange unreachable and the candle store is thin)")
    custom = engine_config.custom_opts(engine_config.get())
    if rr > 0:
        custom[f"rr_{style}"] = min(max(rr, 1.0), 6.0)
    if sl_buffer >= 0:
        custom["sl_buffer"] = min(sl_buffer, 2.0)
    if gate_sweep:
        custom["gate_sweep"] = True
    if 0 <= risk_guard <= 2:
        custom["risk_guard"] = risk_guard
    if 0.3 <= max_chase_atr <= 4:
        custom["max_chase_atr"] = max_chase_atr
    if 0.8 <= min_room_rr <= 4:
        custom["min_room_rr"] = min_room_rr
    if 0.3 <= max_risk_pct <= 10:
        custom["max_risk_pct"] = max_risk_pct
    opts = mtf.opts_for(sens, custom)
    vfn = votes.quick_agrees if consensus else None
    res = await asyncio.to_thread(
        lambda: mtf.backtest(frames, style, et, min_agree, fee_pct, long_only, max_hold_bars, 60, False, opts,
                             slippage_pct, min_grade, breakeven_at_r, killzone_only, vfn))
    res["symbol"] = symbol
    res["demo"] = DEMO_MODE and not live_tickers
    res["params"] = {"style": style, "sens": sens, "min_agree": min_agree, "rr": opts.get(f"rr_{style}") or cfg["rr"],
                     "sl_buffer": opts.get("sl_buffer", 0.25), "fee_pct": fee_pct, "slippage_pct": slippage_pct,
                     "min_grade": min_grade, "breakeven_at_r": breakeven_at_r, "killzone_only": killzone_only,
                     "gate_sweep": bool(opts.get("gate_sweep")), "consensus": consensus, "long_only": long_only,
                     "risk_guard": opts.get("risk_guard", 0)}
    return res


@router.get("/store")
async def store_status():
    """What the candle database currently holds (per symbol and timeframe)."""
    rows = await asyncio.to_thread(candle_store.stats)
    return {"series": rows, "total_candles": sum(r["count"] for r in rows), "pairs": PAIRS}


@router.post("/store/sync")
async def store_sync(symbols: str = "", tfs: str = "4h,1h,15m,5m", bars: int = 1500):
    """Back-fill the candle store: pages history for each symbol/timeframe (up to `bars` each)."""
    syms: List[str] = [x for x in (symbols.upper().split(",") if symbols else PAIRS) if _SYMBOL_RE.match(x)][:20]
    tf_list = [t for t in tfs.split(",") if t in mtf.TF_MS][:9]
    bars = min(max(bars, 100), 50000)
    done: Dict[str, int] = {}
    sem = asyncio.Semaphore(3)
    async with aiohttp.ClientSession() as s:
        async def one(sym: str, tf: str):
            async with sem:
                got = await fetch_history(s, sym, tf, bars)
                done[f"{sym}:{tf}"] = len(got)
        await asyncio.gather(*(one(a, b) for a in syms for b in tf_list))
    return {"synced": done}
