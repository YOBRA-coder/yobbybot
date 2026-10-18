"""
Multi-timeframe scanner: watches every pair, stores the candles it reads,
records the signals the top-down engine fires, and tracks how they end.

  * `scan_symbol` / `scan_all` — fetch the frames (this also writes them to the
    candle store, see market.fetch_klines), run mtf.evaluate_all, return one
    summary row per pair for the dashboard's market scanner.
  * `record_signal` — a fired BUY/SELL goes into `mtf_signals` once (de-duplicated
    per zone), so the history and win-rate survive restarts.
  * `resolve_signals` — walks later candles: stop first = LOSS, target first =
    WIN (both inside one bar = LOSS, the conservative reading), expired after a
    while. Recorded in R (win = +RR, loss = -1), before fees.
"""
import asyncio
import time
from typing import Dict, List, Optional

import aiohttp

from ..config import DEMO_MODE, PAIRS, log
from ..database import get_db
from . import candle_store, demo_feed, engine_config, mtf, votes
from .market import fetch_klines, live_tickers
from .telegram import format_mtf_result, format_mtf_signal, send_telegram

FRAME_LIMIT = 300
EXPIRE_HOURS = {"intraday": 72, "scalp": 12}
DEDUPE_HOURS = 12

# (mode, entry_tf, min_agree) -> {"ts": float, "rows": [...]}
_cache: Dict[tuple, Dict] = {}
DEFAULT_KEY = ("combined", "15m", 70)


def default_args() -> Dict:
    """Scanner defaults from the stored engine settings (what the background loop and alerts use)."""
    c = engine_config.get()
    return {"mode": c["mode"], "entry_tf": c["entry_tf"], "min_agree": c["min_agree"], "sens": c["sens"],
            "consensus": bool(c["consensus"]), "weighting": c["weighting"], "custom": engine_config.custom_opts(c)}


def finalize(result: Dict, frames: Dict[str, List[dict]], now_ms: float, consensus: bool, weighting: str) -> Dict:
    """Strategy vote for the chosen decision; with `consensus`, a SIGNAL the vote disagrees with is held back."""
    dec = result["decision"]
    vt = votes.compute(frames, dec["entry_tf"], dec["setup_tf"], dec, mtf.TF_MS, now_ms, weighting)
    if consensus:
        held = votes.apply_consensus(dec, vt)
        result["decision"] = held
        if dec.get("style") in result["styles"]:
            result["styles"][dec["style"]] = held
    return vt


async def load_frames(session: aiohttp.ClientSession, symbol: str, tfs: List[str], limit: int = FRAME_LIMIT) -> Dict[str, List[dict]]:
    res = await asyncio.gather(*(fetch_klines(session, symbol, tf, limit) for tf in tfs))
    return dict(zip(tfs, res))


def _row(symbol: str, result: Dict, frames: Dict[str, List[dict]], entry_tf: str, vt: Optional[Dict] = None) -> Dict:
    dec = result["decision"]
    tick = live_tickers.get(symbol) or {}
    price = tick.get("price")
    if price is None:
        ec = frames.get(entry_tf) or []
        price = ec[-1]["close"] if ec else None
    styles = {
        k: {"state": v["state"], "agreement": v["agreement"], "direction": v["direction"], "grade": v["grade"],
            "type": v["type"], "entry_tf": v["entry_tf"]}
        for k, v in result["styles"].items()
    }
    zone = dec.get("zone")
    return {
        "symbol": symbol, "price": price, "changePct": tick.get("changePct"),
        "type": dec["type"], "state": dec["state"], "direction": dec["direction"], "style": dec["style"],
        "agreement": dec["agreement"], "grade": dec["grade"], "watch": dec["watch"], "entry_tf": dec["entry_tf"],
        "bias_tf": dec["bias_tf"], "setup_tf": dec["setup_tf"],
        "zone": f"{zone['tf'].upper()} {zone['src']} {zone['bottom']:.6g}–{zone['top']:.6g}" if zone else None,
        "plan": dec.get("plan") if dec["type"] in ("BUY", "SELL") else None,
        "expect": dec.get("expect"), "styles": styles, "time": dec.get("time"),
        "demo": DEMO_MODE and not tick,
        "vote": {"score": vt["score"], "direction": vt["direction"]} if vt else None,
        "vote_blocked": bool(dec.get("vote_blocked")),
    }


async def scan_symbol(session: aiohttp.ClientSession, symbol: str, mode: str, entry_tf: str, min_agree: float,
                      sens: str = "balanced", consensus: bool = False, weighting: str = "equal",
                      custom: Optional[Dict] = None) -> Optional[Dict]:
    tfs = mtf.needed_timeframes(mode, entry_tf)
    frames = await load_frames(session, symbol, tfs)
    if not all(frames.get(tf) for tf in tfs):
        return None
    now_ms = time.time() * 1000
    opts = mtf.opts_for(sens, custom if custom is not None else engine_config.custom_opts(engine_config.get()))

    def work():
        res = mtf.evaluate_all(frames, mode, entry_tf, min_agree, now_ms, None, opts)
        return res, finalize(res, frames, now_ms, consensus, weighting)
    result, vt = await asyncio.to_thread(work)
    try:
        record_signal(symbol, result["decision"])
    except Exception as e:  # noqa: BLE001
        log.warning("mtf signal record failed for %s: %s", symbol, e)
    return _row(symbol, result, frames, entry_tf, vt)


async def scan_all(mode: str = "combined", entry_tf: str = "15m", min_agree: float = 70, symbols: Optional[List[str]] = None,
                   sens: str = "balanced", consensus: bool = False, weighting: str = "equal",
                   custom: Optional[Dict] = None) -> List[Dict]:
    syms = symbols or PAIRS
    sem = asyncio.Semaphore(4)
    rows: List[Dict] = []
    async with aiohttp.ClientSession() as session:
        async def one(sym: str):
            async with sem:
                try:
                    r = await scan_symbol(session, sym, mode, entry_tf, min_agree, sens, consensus, weighting, custom)
                except Exception as e:  # noqa: BLE001
                    log.warning("scan %s failed: %s", sym, e)
                    r = None
                if r:
                    rows.append(r)
        await asyncio.gather(*(one(s) for s in syms))
    order = {s: i for i, s in enumerate(syms)}
    rows.sort(key=lambda r: (-mtf._STATE_RANK[r["state"]], -r["agreement"], order.get(r["symbol"], 99)))
    _cache[(mode, entry_tf, int(min_agree), sens, consensus, weighting)] = {"ts": time.time(), "rows": rows}
    return rows


async def overview(mode: str, entry_tf: str, min_agree: float, max_age: float = 45, sens: str = "balanced",
                   consensus: bool = False, weighting: str = "equal") -> Dict:
    key = (mode, entry_tf, int(min_agree), sens, consensus, weighting)
    hit = _cache.get(key)
    if hit and time.time() - hit["ts"] < max_age:
        return {"rows": hit["rows"], "ts": hit["ts"]}
    rows = await scan_all(mode, entry_tf, min_agree, None, sens, consensus, weighting)
    return {"rows": rows, "ts": _cache[key]["ts"]}


# ── signal history ──────────────────────────────────────────────────────────
def record_signal(symbol: str, dec: Dict) -> Optional[int]:
    if dec.get("type") not in ("BUY", "SELL") or not dec.get("plan"):
        return None
    z = dec.get("zone") or {}
    zone_label = f"{z.get('tf', '').upper()} {z.get('src', '')} {z.get('bottom', 0):.6g}–{z.get('top', 0):.6g}"
    key = f"{symbol}|{dec['type']}|{dec['style']}|{z.get('top', 0):.6g}|{z.get('bottom', 0):.6g}"
    now = int(time.time())
    p = dec["plan"]
    conn = get_db()
    try:
        dup = conn.execute(
            "SELECT id FROM mtf_signals WHERE zone_key=? AND created_at>?", (key, now - DEDUPE_HOURS * 3600)
        ).fetchone()
        if dup:
            return None
        cur = conn.execute(
            """INSERT INTO mtf_signals (symbol,style,entry_tf,side,entry,sl,tp,rr,agreement,grade,zone,zone_key,reason,bar_time,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, dec["style"], dec["entry_tf"], dec["type"], p["entry"], p["sl"], p["tp"], p["rr"], dec["agreement"],
             dec["grade"], zone_label, key, dec.get("reason", ""), int(dec.get("time") or now), now),
        )
        conn.commit()
        log.info("MTF signal %s %s %s @ %.6g (agreement %.0f%%)", symbol, dec["type"], dec["style"], p["entry"], dec["agreement"])
        return cur.lastrowid
    finally:
        conn.close()


def _candles_after(symbol: str, tf: str, bar_time_s: int) -> List[dict]:
    cs = candle_store.load(symbol, tf, 1500)
    if not cs and DEMO_MODE:
        cs = demo_feed.klines(symbol, tf, 1500)
    return [c for c in cs if c["time"] / 1000 > bar_time_s]


def resolve_signals() -> int:
    conn = get_db()
    changed = 0
    try:
        rows = conn.execute("SELECT * FROM mtf_signals WHERE status='OPEN'").fetchall()
        now = int(time.time())
        for r in rows:
            long = r["side"] == "BUY"
            status, exit_px, closed, r_res = None, None, None, None
            for c in _candles_after(r["symbol"], r["entry_tf"], r["bar_time"]):
                hit_sl = c["low"] <= r["sl"] if long else c["high"] >= r["sl"]
                hit_tp = c["high"] >= r["tp"] if long else c["low"] <= r["tp"]
                if hit_sl:
                    status, exit_px, closed, r_res = "LOSS", r["sl"], int(c["time"] / 1000), -1.0
                    break
                if hit_tp:
                    status, exit_px, closed, r_res = "WIN", r["tp"], int(c["time"] / 1000), float(r["rr"])
                    break
            if status is None and now - r["created_at"] > EXPIRE_HOURS.get(r["style"], 48) * 3600:
                status, closed, r_res = "EXPIRED", now, 0.0
            if status:
                conn.execute("UPDATE mtf_signals SET status=?, closed_at=?, exit_price=?, r_result=? WHERE id=?",
                             (status, closed, exit_px, r_res, r["id"]))
                changed += 1
        if changed:
            conn.commit()
    finally:
        conn.close()
    return changed


def list_signals(limit: int = 80) -> Dict:
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM mtf_signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        agg = conn.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN status='WIN' THEN 1 ELSE 0 END) AS wins,
                      SUM(CASE WHEN status='LOSS' THEN 1 ELSE 0 END) AS losses,
                      SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) AS open,
                      COALESCE(SUM(r_result), 0) AS net_r
               FROM mtf_signals"""
        ).fetchone()
    finally:
        conn.close()
    closed = (agg["wins"] or 0) + (agg["losses"] or 0)
    return {
        "signals": [dict(r) for r in rows],
        "stats": {
            "total": agg["n"], "wins": agg["wins"] or 0, "losses": agg["losses"] or 0, "open": agg["open"] or 0,
            "win_rate": round((agg["wins"] or 0) / closed * 100, 1) if closed else None,
            "net_r": round(agg["net_r"] or 0, 2),
        },
    }


async def send_alerts() -> int:
    """Push new signals (and later their WIN/LOSS result) to every user who has
    connected Telegram. Each row is marked so it is sent exactly once per stage."""
    def pending():
        conn = get_db()
        try:
            users = conn.execute(
                "SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token != '' AND telegram_chat_id != ''"
            ).fetchall()
            rows = conn.execute(
                "SELECT * FROM mtf_signals WHERE (status='OPEN' AND notified=0) OR (status IN ('WIN','LOSS') AND notified<2) ORDER BY id"
            ).fetchall()
            return [dict(u) for u in users], [dict(r) for r in rows]
        finally:
            conn.close()

    users, rows = await asyncio.to_thread(pending)
    if not rows:
        return 0
    sent = 0
    if users:
        async with aiohttp.ClientSession() as session:
            for r in rows:
                msg = format_mtf_signal(r) if r["status"] == "OPEN" else format_mtf_result(r)
                for u in users:
                    if await send_telegram(session, u["telegram_token"], u["telegram_chat_id"], msg):
                        sent += 1

    def mark():
        conn = get_db()
        try:
            for r in rows:
                conn.execute("UPDATE mtf_signals SET notified=? WHERE id=?", (1 if r["status"] == "OPEN" else 2, r["id"]))
            conn.commit()
        finally:
            conn.close()
    await asyncio.to_thread(mark)
    return sent


def signal_preview(signal_id: int) -> Optional[Dict]:
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM mtf_signals WHERE id=?", (signal_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    row = dict(r)
    cs = candle_store.load(row["symbol"], row["entry_tf"], 4000)
    if not cs and DEMO_MODE:
        cs = demo_feed.klines(row["symbol"], row["entry_tf"], 1500)
    bar_ms = mtf.TF_MS.get(row["entry_tf"], 60_000)
    t0 = row["bar_time"] * 1000
    end_ms = (row["closed_at"] * 1000 + bar_ms * 12) if row.get("closed_at") else None
    idx = next((i for i, c in enumerate(cs) if c["time"] > t0), len(cs))
    lo = max(0, idx - 60)
    sel = cs[lo:]
    if end_ms is not None:
        sel = [c for c in sel if c["time"] <= end_ms]
    sel = sel[:400]
    return {
        "signal": row, "bar_ms": bar_ms,
        "candles": [{"time": int(c["time"] / 1000), "open": c["open"], "high": c["high"], "low": c["low"],
                     "close": c["close"], "volume": c["volume"]} for c in sel],
    }


# ── which entry timeframe is ready first? ────────────────────────────────────
_entry_cache: Dict[tuple, Dict] = {}


async def entry_scan(symbol: str, mode: str, min_agree: float, sens: str = "balanced", consensus: bool = False,
                     weighting: str = "equal", max_age: float = 20) -> Dict:
    """Run the engine once per entry timeframe (1m…30m) so the UI can show which one is ready right now
    and offer to switch to it. Ranked by state, then agreement, then speed (smaller timeframe first)."""
    key = (symbol, mode, int(min_agree), sens, consensus, weighting)
    hit = _entry_cache.get(key)
    if hit and time.time() - hit["ts"] < max_age:
        return hit["data"]
    need: List[str] = []
    for etf in mtf.ENTRY_TFS:
        for tf in mtf.needed_timeframes(mode, etf):
            if tf not in need:
                need.append(tf)
    async with aiohttp.ClientSession() as s:
        frames = await load_frames(s, symbol, need)
    now_ms = time.time() * 1000
    opts = mtf.opts_for(sens, engine_config.custom_opts(engine_config.get()))

    def work():
        cache: Dict = {}
        out = []
        for etf in mtf.ENTRY_TFS:
            if not all(frames.get(t) for t in mtf.needed_timeframes(mode, etf)):
                continue
            res = mtf.evaluate_all(frames, mode, etf, min_agree, now_ms, cache, opts)
            if consensus:
                finalize(res, frames, now_ms, True, weighting)
            d = res["decision"]
            out.append({"tf": etf, "state": d["state"], "type": d["type"], "direction": d["direction"],
                        "agreement": d["agreement"], "grade": d["grade"], "style": d["style"], "watch": d["watch"]})
        return out
    rows = await asyncio.to_thread(work)
    order = {t: i for i, t in enumerate(mtf.ENTRY_TFS)}
    ranked = sorted(rows, key=lambda r: (-mtf._STATE_RANK[r["state"]], -r["agreement"], order[r["tf"]]))
    best = ranked[0] if ranked and ranked[0]["state"] in ("SIGNAL", "SETUP") else None
    data = {"symbol": symbol, "rows": rows, "best": best["tf"] if best else None}
    _entry_cache[key] = {"ts": time.time(), "data": data}
    return data
