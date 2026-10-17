"""
Approval — the rules for what is ALLOWED to become a signal.

A setup fires only if ALL of these hold (each is visible and switchable in the UI):

  1. its core strategy is approved           (settings.approved_styles: intraday / scalp)
  2. its pair is on the watchlist            (settings.watchlist)
  3. enough approved confirmation strategies agree (settings.approved_confirms / min_confirms)
  4. the BACKTEST for that pair + strategy + entry TF is APPROVED
     (settings.require_backtest): enough trades, profit factor above the bar, positive net R,
     and the newest third of the test not losing — verdicts are cached and re-run in the background.

The backtest and the live scanner call the very same engine with the very same settings.
"""
import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

from ..config import DEMO_MODE, log
from ..database import get_db
from . import mtf
from .market import fetch_history

VERDICT_TTL_S = 12 * 3600
TARGET_DAYS = 60

_pending: Dict[Tuple[str, str, str], float] = {}     # (symbol, style, entry_tf) -> queued_at
_running: set = set()


def engine_cfg(settings: dict) -> Dict:
    """Settings -> the `cfg` the engine and the backtest take."""
    return {
        "styles": list(settings.get("approved_styles") or []),
        "confirms": list(settings.get("approved_confirms") or []),
        "min_confirms": int(settings.get("min_confirms", 1)),
    }


def judge(res: Dict, settings: dict) -> Tuple[str, List[str]]:
    """APPROVED / REJECTED / INSUFFICIENT + the reasons, from a backtest result."""
    min_n = int(settings.get("bt_min_trades", 10))
    min_pf = float(settings.get("bt_min_pf", 1.2))
    n = res.get("trades", 0)
    pf = res.get("profit_factor") or 0.0
    why: List[str] = []
    if n < min_n:
        return "INSUFFICIENT", [f"only {n} trades in the test window (need {min_n}) — not enough evidence either way"]
    if pf < min_pf:
        why.append(f"profit factor {pf:.2f} is below {min_pf:.2f}")
    if res.get("net_r", 0) <= 0:
        why.append(f"net result {res.get('net_r', 0):+.1f}R is not positive")
    rec = res.get("recent")
    if rec and rec["trades"] >= 3 and rec["net_r"] < 0:
        why.append(f"the newest third of the test lost {rec['net_r']:+.1f}R — the edge may have faded")
    if why:
        return "REJECTED", why
    return "APPROVED", [f"{n} trades, {res['win_rate']}% wins, PF {pf:.2f}, {res['net_r']:+.1f}R, max drawdown {res['max_drawdown_r']}R"]


def summary(res: Dict) -> Dict:
    keys = ("trades", "wins", "losses", "scratches", "win_rate", "net_r", "avg_r", "profit_factor", "gross_win", "gross_loss",
            "max_drawdown_r", "worst_losing_streak", "recent", "from", "to", "assumptions")
    return {k: res.get(k) for k in keys}


def save_verdict(symbol: str, style: str, entry_tf: str, verdict: str, why: List[str], res: Dict) -> None:
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO backtest_verdicts (symbol, style, entry_tf, updated_at, verdict, result) VALUES (?,?,?,?,?,?)",
            (symbol, style, entry_tf, int(time.time()), verdict, json.dumps({**summary(res), "why": why})),
        )
        conn.commit()
    finally:
        conn.close()


def load_verdicts() -> Dict[Tuple[str, str, str], Dict]:
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM backtest_verdicts").fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        d = json.loads(r["result"])
        out[(r["symbol"], r["style"], r["entry_tf"])] = {"verdict": r["verdict"], "updated_at": r["updated_at"], **d}
    return out


def get_verdict(symbol: str, style: str, entry_tf: str) -> Optional[Dict]:
    return load_verdicts().get((symbol, style, entry_tf))


def queue(symbol: str, style: str, entry_tf: str) -> None:
    key = (symbol, style, entry_tf)
    if key not in _running:
        _pending.setdefault(key, time.time())


def pooled(rows: List[Dict]) -> Dict:
    """Sum the stored backtests of one strategy across pairs (gross win/loss add, so PF is exact)."""
    n = sum(r.get("trades") or 0 for r in rows)
    gw = sum(r.get("gross_win") or 0 for r in rows)
    gl = sum(r.get("gross_loss") or 0 for r in rows)
    net = sum(r.get("net_r") or 0 for r in rows)
    wins = sum(r.get("wins") or 0 for r in rows)
    return {"trades": n, "net_r": round(net, 2), "wins": wins, "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "profit_factor": round(gw / gl, 2) if gl > 0 else (99.0 if n else None), "pairs": len(rows)}


def effective(symbol: str, style: str, entry_tf: str, settings: dict, verdicts: Optional[Dict] = None) -> Dict:
    """The verdict that actually applies to a pair: its OWN backtest when it has enough trades,
    otherwise the strategy's POOLED result across the watchlist (crypto setups are rare — one pair
    alone rarely produces enough trades to judge). A pair with plenty of trades and a clearly
    losing record is vetoed even if the pool is good."""
    verdicts = verdicts if verdicts is not None else load_verdicts()
    min_n = int(settings.get("bt_min_trades", 10))
    min_pf = float(settings.get("bt_min_pf", 1.2))
    own = verdicts.get((symbol, style, entry_tf))
    if own is None:
        return {"verdict": "PENDING", "basis": "none", "why": ["backtest not run yet for this pair"], "updated_at": 0}
    if own["trades"] >= min_n:
        return {**own, "basis": "pair"}
    rows = [v for (s_, st, tf), v in verdicts.items() if st == style and tf == entry_tf]
    pool = pooled(rows)
    if pool["trades"] < min_n:
        return {**own, "verdict": "INSUFFICIENT", "basis": "pooled", "pool": pool,
                "why": [f"only {pool['trades']} trades across {pool['pairs']} tested pair(s) — need {min_n} for a verdict"]}
    ok = (pool["profit_factor"] or 0) >= min_pf and pool["net_r"] > 0
    pair_bad = own["trades"] >= 4 and own["net_r"] < -1.5
    if ok and not pair_bad:
        return {**own, "verdict": "APPROVED", "basis": "pooled", "pool": pool,
                "why": [f"pooled over {pool['pairs']} pairs: {pool['trades']} trades, {pool['win_rate']}% wins, PF {pool['profit_factor']}, {pool['net_r']:+.1f}R"]}
    why = [f"pooled over {pool['pairs']} pairs: PF {pool['profit_factor']}, {pool['net_r']:+.1f}R — below the bar"] if not ok else \
          [f"this pair lost {own['net_r']:+.1f}R in the test — vetoed even though the strategy pool is fine"]
    return {**own, "verdict": "REJECTED", "basis": "pooled", "pool": pool, "why": why}


def gate(symbol: str, style: str, entry_tf: str, settings: dict, verdicts: Optional[Dict] = None) -> Tuple[bool, str, Optional[Dict]]:
    """May a fired setup become a signal? -> (allowed, reason, verdict row)."""
    if not settings.get("require_backtest", True):
        return True, "backtest gate is off", None
    verdicts = verdicts if verdicts is not None else load_verdicts()
    v = effective(symbol, style, entry_tf, settings, verdicts)
    own = verdicts.get((symbol, style, entry_tf))
    if own is None or time.time() - own["updated_at"] > VERDICT_TTL_S:
        queue(symbol, style, entry_tf)          # (re)run in the background; a stale verdict keeps applying meanwhile
    if v["verdict"] == "APPROVED":
        return True, "backtest approved", v
    if v["verdict"] == "PENDING":
        return False, "backtest is still running for this pair — no signal until it is approved", v
    return False, f"backtest {v['verdict'].lower()}: {'; '.join(v.get('why') or [])}", v


def history_plan(style: str, entry_tf: str, days: int = TARGET_DAYS) -> Dict[str, int]:
    """How many candles of each series a `days`-long backtest needs (incl. warm-up + level history)."""
    cfg = mtf.STYLES[style]
    et = mtf._entry_for(style, entry_tf)
    span_ms = days * 86_400_000
    counts = {
        et: min(6000, int(span_ms / mtf.TF_MS[et])),
        cfg["setup_tf"]: int(span_ms / mtf.TF_MS[cfg["setup_tf"]]) + 320,
        cfg["bias_tf"]: int(span_ms / mtf.TF_MS[cfg["bias_tf"]]) + 320,
    }
    counts["1h"] = max(counts.get("1h", 0), int(span_ms / 3_600_000) + 800)
    counts["1d"] = max(counts.get("1d", 0), days + 75)
    return {tf: min(n, 6000) for tf, n in counts.items()}


async def run_backtest(session: aiohttp.ClientSession, symbol: str, style: str, entry_tf: str, settings: dict,
                       days: int = TARGET_DAYS, min_agree: Optional[float] = None, keep_all: bool = False) -> Dict:
    counts = history_plan(style, entry_tf, days)
    got = await asyncio.gather(*(fetch_history(session, symbol, tf, n) for tf, n in counts.items()))
    frames = dict(zip(counts.keys(), got))
    need = [tf for tf in counts if tf not in ("1h", "1d")]
    if any(len(frames[tf]) < 100 for tf in need):
        raise RuntimeError("Not enough history available (exchange unreachable and the candle store is thin)")
    cfg = engine_cfg(settings)
    res = await asyncio.to_thread(
        mtf.backtest, frames, style, entry_tf, float(min_agree or settings.get("min_agree", 70)), 0.1,
        bool(settings.get("long_only")), 300, 60, keep_all, cfg, 0.03, True, float(settings.get("breakeven_r", 0) or 0),
    )
    res["symbol"] = symbol
    res["demo"] = DEMO_MODE
    return res


async def verdict_for(session: aiohttp.ClientSession, symbol: str, style: str, entry_tf: str, settings: dict) -> Dict:
    key = (symbol, style, entry_tf)
    _running.add(key)
    try:
        res = await run_backtest(session, symbol, style, entry_tf, settings)
        verdict, why = judge(res, settings)
        save_verdict(symbol, style, entry_tf, verdict, why, res)
        log.info("backtest %s %s %s -> %s (%s)", symbol, style, entry_tf, verdict, "; ".join(why))
        return {"symbol": symbol, "style": style, "entry_tf": entry_tf, "verdict": verdict, "why": why, **summary(res)}
    finally:
        _running.discard(key)
        _pending.pop(key, None)


def expected_keys(settings: dict) -> List[Tuple[str, str, str]]:
    """Every (symbol, style, entry TF) the scanner could fire from under these settings."""
    out = []
    for sym in settings.get("watchlist") or []:
        for style in settings.get("approved_styles") or []:
            if style in mtf.STYLES and (settings.get("mode") in ("combined", style)):
                out.append((sym, style, mtf._entry_for(style, settings.get("entry_tf", "15m"))))
    return out


async def verdict_loop(get_settings) -> None:
    """Background worker: runs queued backtests first, then refreshes stale verdicts, one at a time."""
    await asyncio.sleep(5)
    while True:
        try:
            settings = get_settings()
            if settings.get("require_backtest", True):
                keys = expected_keys(settings)
                have = load_verdicts()
                for k in keys:
                    v = have.get(k)
                    if _pending_retry.get(k, 0) > time.time():
                        continue
                    if v is None or time.time() - v["updated_at"] > VERDICT_TTL_S:
                        _pending.setdefault(k, time.time())
                todo = [k for k in sorted(_pending, key=_pending.get) if k in keys and _pending_retry.get(k, 0) <= time.time()]
                if todo:
                    sym, style, tf = todo[0]
                    async with aiohttp.ClientSession() as session:
                        try:
                            await verdict_for(session, sym, style, tf, settings)
                        except Exception as e:  # noqa: BLE001
                            log.warning("backtest %s %s %s failed: %s", sym, style, tf, e)
                            _pending.pop(todo[0], None)
                            _pending_retry[todo[0]] = time.time() + 900
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning("verdict loop error: %s", e)
        await asyncio.sleep(20)


_pending_retry: Dict[Tuple[str, str, str], float] = {}


def status() -> Dict:
    return {"queued": [list(k) for k in _pending], "running": [list(k) for k in _running]}
