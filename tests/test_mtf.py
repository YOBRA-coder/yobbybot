"""Top-down multi-timeframe SMC engine, candle store, scanner and API handlers.
Run:  python -m tests.test_mtf   (from backend/)   or   pytest tests/

No network and no web framework needed: fastapi/aiohttp are stubbed, Binance is
"unreachable", and the router handlers run on the opt-in synthetic feed
(CRYPTO_DEMO=1). Everything uses a throw-away SQLite file.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CRYPTO_DEMO"] = "1"

# ── stubs for third-party packages ──────────────────────────────────────────
aiohttp = types.ModuleType("aiohttp")


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, *a, **k): raise ConnectionError("exchange unreachable (test)")


aiohttp.ClientSession = _Session
aiohttp.ClientTimeout = lambda **kw: None
sys.modules["aiohttp"] = aiohttp

fastapi = types.ModuleType("fastapi")


class _HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        self.status_code, self.detail = status_code, detail


class _Router:
    def __init__(self, *a, **k): pass
    def get(self, *a, **k): return lambda f: f
    def post(self, *a, **k): return lambda f: f


fastapi.APIRouter, fastapi.HTTPException, fastapi.WebSocket = _Router, _HTTPException, object
sys.modules["fastapi"] = fastapi

from app import config  # noqa: E402

_tmp = tempfile.mkdtemp()
config.DB_PATH = Path(_tmp) / "test.db"

from app import database  # noqa: E402
from app.services import candle_store, demo_feed, mtf, scanner, smc  # noqa: E402
from tests.synthetic import STEP, T0, candle, from_waypoints  # noqa: E402

database.init_db()


def run(coro):
    return asyncio.run(coro)


# ── detectors ──────────────────────────────────────────────────────────────
def test_bos_then_choch():
    pts = [(0, 100), (10, 110), (16, 104), (26, 118), (32, 109), (42, 124), (48, 112), (56, 98), (66, 88)]
    br = smc.find_structure_breaks(from_waypoints(pts, noise=0.05))
    kinds = [(e["kind"], e["dir"]) for e in br["events"]]
    assert ("BOS", "bullish") in kinds, kinds
    assert ("CHoCH", "bearish") in kinds, kinds
    assert kinds.index(("CHoCH", "bearish")) > kinds.index(("BOS", "bullish"))
    assert br["trend"] == "bearish"


def test_break_needs_a_close_not_a_wick():
    cs = from_waypoints([(0, 100), (10, 110), (18, 104), (24, 108)], noise=0.02)
    hi = max(c["high"] for c in cs[:12])
    n = len(cs)
    # a wick through the swing high that closes back inside is NOT a break
    cs.append(candle(n, 108, hi + 1.0, 107.5, 108.2))
    cs += [candle(n + 1 + k, 108.2, 108.4, 107.9, 108.1) for k in range(5)]
    assert not [e for e in smc.find_structure_breaks(cs)["events"] if e["time"] == int((T0 + n * STEP) / 1000)]


def test_equal_highs_found_and_swept_flag():
    cs = from_waypoints([(0, 100), (8, 110), (14, 104), (22, 110.0), (30, 100), (40, 103)])
    sw = smc.find_swings(cs)
    eq = smc.find_equal_levels(cs, sw)
    eqh = [e for e in eq if e["kind"] == "EQH"]
    assert eqh and not eqh[0]["swept"]
    cs.append(candle(len(cs), 103, 112, 102, 103.2))     # runs the highs
    eq2 = smc.find_equal_levels(cs, smc.find_swings(cs))
    assert [e for e in eq2 if e["kind"] == "EQH" and e["swept"]]


def test_premium_discount_zones():
    cs = from_waypoints([(0, 100), (40, 140), (80, 104)], noise=0.02)
    pd = smc.premium_discount(cs)
    assert pd["zone"] == "discount" and pd["pct"] < 15
    cs2 = from_waypoints([(0, 100), (40, 140), (80, 136)], noise=0.02)
    assert smc.premium_discount(cs2)["zone"] == "premium"


def test_trendline_support_and_break():
    cs = from_waypoints([(0, 100), (10, 90), (20, 108), (30, 95), (40, 114), (50, 112)])
    tl = smc.find_trendlines(cs, smc.find_swings(cs))
    sup = [t for t in tl if t["kind"] == "support"]
    assert sup and not sup[0]["broken"] and sup[0]["p2"] > sup[0]["p1"]


# ── the engine, on hand-built contexts ─────────────────────────────────────
ZONE_FORMED = int(T0 / 1000) - 200_000


def _ctx(bias, zones=(), atr=1.0, pd_pct=30.0, last_break=True):
    brk = {"kind": "BOS", "dir": bias, "level": 100.0, "time": ZONE_FORMED, "from_time": ZONE_FORMED - 100}
    ctx = {
        "order_blocks": [z for z in zones if z[0] == "ob" and True] and [
            {"top": z[1], "bottom": z[2], "kind": bias, "time": ZONE_FORMED, "formed_at": ZONE_FORMED,
             "mitigated": False, "strength": 2.0} for z in zones if z[0] == "ob"] or [],
        "fvgs": [{"top": z[1], "bottom": z[2], "kind": bias, "time": ZONE_FORMED, "filled": False, "fill_pct": 0.0}
                 for z in zones if z[0] == "fvg"],
        "sd_zones": [], "structure": {"swings": [], "trend": bias}, "sweeps": [], "eq": [], "poi": [], "retests": [],
        "breaks": {"events": [brk] if last_break else [], "trend": bias if last_break else None},
        "pd": {"pct": pd_pct, "zone": "discount" if pd_pct < 45 else "premium", "high": 120, "low": 90, "eq": 105},
        "atr": atr,
    }
    ctx["bias"] = smc.bias_state(ctx)
    return ctx


def _entry(last="engulf", vol_spike=True):
    cs = []
    for i in range(50):
        p = 104.0 + (0.1 if i % 2 else -0.1)
        cs.append(candle(i, p, p + 0.25, p - 0.25, p + (0.05 if i % 2 else -0.05), 100.0))
    cs.append(candle(50, 104.0, 104.1, 102.6, 102.8, 100.0))      # drop
    cs.append(candle(51, 102.8, 102.9, 100.8, 101.2, 100.0))      # taps the 100-101 zone, closes above 100
    if last == "engulf":
        cs.append(candle(52, 101.1, 103.0, 100.9, 102.9, 300.0 if vol_spike else 100.0))   # bullish engulfing
    elif last == "doji":
        cs.append(candle(52, 101.2, 101.35, 101.1, 101.25, 100.0))
    return cs


def _long_setup():
    cb = _ctx("bullish", zones=[("fvg", 101.2, 100.4)], atr=2.6)
    cs = _ctx("bullish", zones=[("ob", 101.0, 100.0)], atr=1.3)
    return cb, cs


def test_complete_long_setup_fires_with_a_sane_plan():
    cb, cs = _long_setup()
    r = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert r["type"] == "BUY" and r["state"] == "SIGNAL", (r["state"], r["agreement"], [(c["id"], c["ok"]) for c in r["checks"]])
    p = r["plan"]
    assert p["sl"] < p["entry"] < p["tp"]
    assert abs((p["tp"] - p["entry"]) / (p["entry"] - p["sl"]) - 2.0) < 1e-9
    assert r["agreement"] >= 70 and r["gates_ok"] and r["targetPrice"] == p["tp"] and r["stopLoss"] == p["sl"]
    assert r["steps"][-1]["step"] == "DECISION" and r["steps"][-1]["status"] == "pass"


def test_no_trigger_candle_means_setup_not_signal():
    cb, cs = _long_setup()
    r = mtf.evaluate_style("intraday", cb, cs, _entry(last="doji"), "15m")
    assert r["type"] == "HOLD" and r["state"] == "SETUP"
    assert "confirmation candle" in r["watch"]


def test_neutral_bias_stands_aside():
    cb = _ctx("bullish", last_break=False)
    cb["structure"]["trend"] = "ranging"
    cb["bias"] = smc.bias_state(cb)
    r = mtf.evaluate_style("intraday", cb, _long_setup()[1], _entry(), "15m")
    assert r["type"] == "HOLD" and r["state"] == "STAND_ASIDE" and r["direction"] == "neutral"


def test_setup_tf_disagreeing_with_bias_blocks_the_trade():
    cb, cs = _long_setup()
    cs = _ctx("bearish", zones=[("ob", 101.0, 100.0)])
    r = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert r["type"] == "HOLD"
    assert next(c for c in r["checks"] if c["id"] == "align")["ok"] is False


def test_forming_candle_is_ignored_for_the_decision():
    cb, cs = _long_setup()
    ent = _entry()
    forming_now = ent[-1]["time"] + 5 * 60 * 1000          # the trigger bar is still open
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m", now_ms=forming_now)
    assert r["type"] == "HOLD"                              # without the trigger bar there is no confirmation
    r2 = mtf.evaluate_style("intraday", cb, cs, ent, "15m", now_ms=ent[-1]["time"] + 16 * 60 * 1000)
    assert r2["type"] == "BUY"


def test_price_that_ran_away_from_the_zone_is_not_chased():
    cb, cs = _long_setup()
    ent = _entry()
    ent.append(candle(53, 102.9, 108.0, 102.8, 107.9, 100.0))   # +5 ATR away from the zone top
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m")
    assert r["type"] == "HOLD"


def test_short_is_the_mirror_image():
    cb = _ctx("bearish", zones=[("fvg", 99.6, 98.8)], atr=2.6, pd_pct=70)
    cs = _ctx("bearish", zones=[("ob", 100.0, 99.0)], atr=1.3)
    ent = []
    for i in range(50):
        p = 96.0 + (0.1 if i % 2 else -0.1)
        ent.append(candle(i, p, p + 0.25, p - 0.25, p + (0.05 if i % 2 else -0.05), 100.0))
    ent.append(candle(50, 96.0, 97.4, 95.9, 97.2, 100.0))
    ent.append(candle(51, 97.2, 99.2, 97.1, 98.8, 100.0))          # taps the 99-100 zone from below
    ent.append(candle(52, 98.9, 99.1, 97.0, 97.1, 300.0))          # bearish engulfing
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m")
    assert r["type"] == "SELL", (r["state"], [(c["id"], c["ok"], c["detail"]) for c in r["checks"] if c["ok"] is False])
    p = r["plan"]
    assert p["tp"] < p["entry"] < p["sl"]


def _short_entry():
    ent = []
    for i in range(50):
        p = 96.0 + (0.1 if i % 2 else -0.1)
        ent.append(candle(i, p, p + 0.25, p - 0.25, p + (0.05 if i % 2 else -0.05), 100.0))
    ent.append(candle(50, 96.0, 97.4, 95.9, 97.2, 100.0))
    ent.append(candle(51, 97.2, 99.2, 97.1, 98.8, 100.0))
    ent.append(candle(52, 98.9, 99.1, 97.0, 97.1, 300.0))
    return ent


def test_counter_trend_watch_and_outlook_timing():
    # 4H still bullish, but the 1H has already turned bearish and price tapped a 1H supply zone
    cb = _ctx("bullish", zones=[], atr=2.6, pd_pct=70)
    cs = _ctx("bearish", zones=[("ob", 100.0, 99.0)], atr=1.3)
    ent = _short_entry()
    trend = mtf.evaluate_style("intraday", cb, cs, ent, "15m")
    assert trend["type"] == "HOLD" and trend["kind"] == "trend"           # with-trend: 1H disagrees -> no trade
    c = mtf.evaluate_style("intraday", cb, cs, ent, "15m", against=True)
    assert c["kind"] == "counter" and c["direction"] == "bearish" and c["side"] == "SELL"
    assert c["state"] in ("SIGNAL", "SETUP"), (c["state"], [(k["id"], k["ok"]) for k in c["checks"]])
    assert c["plan"]["rr"] <= 1.5 and c["plan"]["tp"] < c["plan"]["entry"] < c["plan"]["sl"]
    o = c["outlook"]
    assert o["kind"] == "counter" and o["risk_note"] and o["to"] >= o["from"] > 0 and o["window"]
    # with-trend read also carries what-to-expect + a time window
    cb2, cs2 = _long_setup()
    t2 = mtf.evaluate_style("intraday", cb2, cs2, _entry(), "15m")
    assert t2["outlook"]["kind"] == "trend" and t2["outlook"]["to"] > t2["outlook"]["from"] > 0
    # a neutral higher timeframe has no trend to fade
    n = mtf.evaluate_style("intraday", _ctx("neutral", last_break=False), cs, ent, "15m", against=True)
    assert n["state"] == "STAND_ASIDE" and "no trend to fade" in n["watch"]


def test_evaluate_all_returns_counter_watch():
    out = mtf.evaluate_all(_frames("BTCUSDT"), "intraday", "15m") if "_frames" in globals() else None
    if out:
        assert out["counter"]["kind"] == "counter" and out["decision"]["outlook"] is not None
        json.dumps(out["counter"])


def test_styles_and_entry_tf_selection():
    assert mtf.needed_timeframes("intraday", "15m") == ["4h", "1h", "15m"]
    assert mtf.needed_timeframes("scalp", "1m") == ["1h", "15m", "1m"]
    comb = mtf.needed_timeframes("combined", "30m")
    assert {"4h", "1h", "15m", "30m", "5m"} <= set(comb)           # scalp falls back to its own 5m entry
    assert mtf._entry_for("scalp", "30m") == "5m" and mtf._entry_for("intraday", "5m") == "5m"


# ── end-to-end on the synthetic market ─────────────────────────────────────
def _frames(sym="BTCUSDT", n=1000, tfs=("4h", "1h", "15m", "5m", "1m")):
    return {tf: demo_feed.klines(sym, tf, n) for tf in tfs}


def test_evaluate_all_runs_on_every_pair_and_serializes():
    import time
    for sym in config.PAIRS[:4]:
        res = mtf.evaluate_all(_frames(sym, 400), "combined", "15m", 70, now_ms=time.time() * 1000)
        d = res["decision"]
        assert d["state"] in mtf._STATE_RANK and set(res["styles"]) == {"intraday", "scalp"}
        json.dumps(d)
        json.dumps(mtf.panel_payload("1h", _frames(sym, 400)["1h"], res["analysis"]["1h"]))


def test_backtest_has_no_lookahead():
    fr = _frames("ETHUSDT", 3000, ("4h", "1h", "15m"))
    full = mtf.backtest(fr, "intraday", "15m", 65, keep_all=True)
    cut_i = len(fr["15m"]) - 900
    cut_t = fr["15m"][cut_i]["time"]
    part = {tf: [c for c in cs if c["time"] < cut_t] for tf, cs in fr.items()}
    cutrun = mtf.backtest(part, "intraday", "15m", 65, keep_all=True)
    a = [(t["entry_time"], t["exit_time"], t["side"], round(t["r"], 6)) for t in cutrun["trade_log"][:-1]]
    b = [(t["entry_time"], t["exit_time"], t["side"], round(t["r"], 6)) for t in full["trade_log"]]
    assert a, "need at least one settled trade to compare"
    assert a == b[:len(a)], "trades before the cut changed when future candles were added"


def test_backtest_costs_and_sl_first_rule():
    fr = _frames("SOLUSDT", 2500, ("4h", "1h", "15m"))
    bt = mtf.backtest(fr, "intraday", "15m", 60, keep_all=True)
    for t in bt["trade_log"]:
        assert t["result"] in ("WIN", "LOSS")
        risk = abs(t["entry"] - t["sl"])
        if t["result"] == "WIN" and t["exit"] == t["tp"]:
            assert t["r"] < 2.0 + 1e-9                 # fees always reduce a full winner
        if t["result"] == "LOSS" and t["exit"] == t["sl"]:
            assert t["r"] <= -1.0 + 1e-9
        assert risk > 0


# ── candle store + signal history ──────────────────────────────────────────
def test_candle_store_roundtrip_and_forming_bar_overwrite():
    cs = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(10)]
    assert candle_store.upsert("TESTUSDT", "15m", cs) == 10
    forming = dict(cs[-1], close=999.0)
    candle_store.upsert("TESTUSDT", "15m", [forming])
    got = candle_store.load("TESTUSDT", "15m", 5)
    assert len(got) == 5 and got[-1]["close"] == 999.0 and got[0]["time"] < got[-1]["time"]
    older = candle_store.load("TESTUSDT", "15m", 3, before=cs[4]["time"])
    assert [c["time"] for c in older] == [cs[2]["time"], cs[3]["time"], cs[4]["time"]]
    st = [r for r in candle_store.stats() if r["symbol"] == "TESTUSDT"][0]
    assert st["count"] == 10 and st["interval"] == "15m"


def test_signal_recorded_once_and_settled():
    cb, cs = _long_setup()
    dec = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert dec["type"] == "BUY"
    sid = scanner.record_signal("RECUSDT", dec)
    assert sid and scanner.record_signal("RECUSDT", dec) is None          # de-duplicated
    p = dec["plan"]
    after = int(dec["time"])
    # price first reaches the target -> WIN at +RR
    bars = [candle(100 + k, p["entry"], p["entry"] + 0.1 * k, p["entry"] - 0.05, p["entry"] + 0.1 * k) for k in range(1, 4)]
    bars.append(candle(110, p["entry"], p["tp"] + 0.1, p["entry"], p["tp"]))
    for b in bars:
        b["time"] = (after + 900 * (bars.index(b) + 1)) * 1000
    candle_store.upsert("RECUSDT", "15m", bars)
    assert scanner.resolve_signals() >= 1
    row = [s for s in scanner.list_signals()["signals"] if s["symbol"] == "RECUSDT"][0]
    assert row["status"] == "WIN" and abs(row["r_result"] - 2.0) < 1e-9


def test_stop_and_target_in_one_bar_counts_as_loss():
    cb, cs = _long_setup()
    dec = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    dec = json.loads(json.dumps(dec))
    dec["time"] = int(dec["time"]) + 99999                                # distinct key/bar
    sid = scanner.record_signal("BOTHUSDT", dec)
    p = dec["plan"]
    bar = candle(1, p["entry"], p["tp"] + 1, p["sl"] - 1, p["entry"])
    bar["time"] = (dec["time"] + 900) * 1000
    candle_store.upsert("BOTHUSDT", "15m", [bar])
    scanner.resolve_signals()
    row = [s for s in scanner.list_signals()["signals"] if s["id"] == sid][0]
    assert row["status"] == "LOSS" and row["r_result"] == -1.0


# ── API handlers (stubbed framework, synthetic market) ─────────────────────
def test_api_mtf_endpoint_payload():
    from app.routers import market as mr
    out = run(mr.get_mtf("BTCUSDT", "combined", "15m", 70, "4h,1h,15m"))
    assert out["ok"] and out["demo"] is True
    assert [p["tf"] for p in out["panels"]] == ["4h", "1h", "15m"]
    for p in out["panels"]:
        assert len(p["candles"]) >= 250
        for key in ("orderBlocks", "fvgs", "sdZones", "structure", "breaks", "sweeps", "poi", "retests", "eq", "pd", "trendlines"):
            assert key in p, key
    assert out["decision"]["checks"] and out["styles"]["scalp"]["bias_tf"] == "1h"
    json.dumps(out)
    for bad in (dict(mode="x"), dict(entry_tf="4h"), dict(min_agree=10)):
        kw = dict(mode="combined", entry_tf="15m", min_agree=70)
        kw.update(bad)
        try:
            run(mr.get_mtf("BTCUSDT", **kw))
            assert False, "should reject"
        except Exception as e:  # noqa: BLE001
            assert getattr(e, "status_code", None) == 400
    try:
        run(mr.get_mtf("../etc", "combined", "15m", 70, ""))
        assert False
    except Exception as e:  # noqa: BLE001
        assert e.status_code == 400


def test_api_overview_backtest_store():
    from app.routers import market as mr
    ov = run(mr.get_overview("combined", "15m", 70))
    assert len(ov["rows"]) == len(config.PAIRS)
    assert all(r["state"] in mtf._STATE_RANK for r in ov["rows"])
    json.dumps(ov)
    bt = run(mr.mtf_backtest("BTCUSDT", "scalp", "5m", 600, 70, False))
    assert bt["style"] == "scalp" and "net_r" in bt and bt["demo"] is True
    st = run(mr.store_status())
    assert "series" in st


def test_mtf_bot_branch_returns_a_usable_ta_dict():
    from app import tasks
    bot = {"id": "b1", "pair": "BTCUSDT", "user_id": "u", "strategy": "SMC Multi-Timeframe"}
    for tf in ("5m", "15m"):
        ta = run(tasks.evaluate_mtf_live(None, bot, tf))
        assert ta["type"] in ("BUY", "HOLD") and "steps" in ta and "reason" in ta
        if ta["type"] == "BUY":
            assert ta["stopLoss"] < ta["targetPrice"]


def test_mtf_strategy_backtests_through_the_existing_runner():
    from app.services.backtest import MTF_PRESET, run_backtest
    out = run_backtest(MTF_PRESET, demo_feed.klines("BTCUSDT", "15m", 1000))
    assert {"profit", "trades", "winRate", "maxDrawdown"} <= set(out)
    # an entry timeframe above 30m is refused politely instead of crashing
    assert run_backtest(MTF_PRESET, demo_feed.klines("BTCUSDT", "4h", 300))["trades"] == 0


def test_send_alerts_pushes_each_stage_once():
    from app.database import get_db
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO users (id,name,email,password_hash,telegram_token,telegram_chat_id) VALUES ('u1','T','t@x.com','x','tok','chat')")
    conn.commit(); conn.close()
    cb, cs = _long_setup()
    dec = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert scanner.record_signal("ALERTUSDT", dec)      # self-contained: own fresh signal
    sent = []

    async def fake(session, token, chat, msg):
        sent.append(msg)
        return True
    orig = scanner.send_telegram
    scanner.send_telegram = fake
    try:
        first = asyncio.run(scanner.send_alerts())
        second = asyncio.run(scanner.send_alerts())
    finally:
        scanner.send_telegram = orig
    assert first >= 1 and len(sent) == first
    assert second == 0                      # nothing is sent twice


def test_news_parse_and_filter():
    from app.services import news
    xml = ("<rss><channel><item><title>Bitcoin breaks 100k</title><link>https://x.io/a</link>"
           "<pubDate>Sat, 17 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;BTC rallies&lt;/p&gt;</description></item>"
           "<item><title>Solana upgrade</title><link>https://x.io/b</link></item>"
           "<item><title>no link</title><link>javascript:x</link></item></channel></rss>")
    items = news.parse_rss(xml, "T")
    assert [i["title"] for i in items] == ["Bitcoin breaks 100k", "Solana upgrade"]
    assert items[0]["summary"] == "BTC rallies" and items[0]["time"] > 0
    assert [i["title"] for i in news.filter_for(items, "BTCUSDT")] == ["Bitcoin breaks 100k"]
    assert news.parse_rss("<not xml", "T") == []


def test_signal_preview_has_context_and_exit():
    cb, cs = _long_setup()
    dec = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    dec["plan"] = dict(dec["plan"])
    sid = scanner.record_signal("PREVUSDT", dec)
    assert sid
    t0 = int(dec["time"])
    bars = [candle(i, 100 + i * 0.01, 100.5 + i * 0.01, 99.8, 100.2 + i * 0.01) for i in range(80)]
    for i, b in enumerate(bars):
        b["time"] = (t0 + 900 * (i - 40)) * 1000
    candle_store.upsert("PREVUSDT", "15m", bars)
    pv = scanner.signal_preview(sid)
    assert pv["signal"]["symbol"] == "PREVUSDT" and pv["bar_ms"] == 900_000
    assert 40 <= len(pv["candles"]) <= 400 and pv["candles"][0]["time"] < t0
    assert scanner.signal_preview(999999) is None


def test_sensitivity_presets_change_what_fires():
    o_strict, o_fast = mtf.opts_for("strict"), mtf.opts_for("fast")
    assert o_strict["gate_sweep"] is True and o_fast["trigger_bars"] > o_strict["trigger_bars"]
    assert mtf.SENSITIVITY["strict"]["min_agree"] > mtf.SENSITIVITY["balanced"]["min_agree"] > mtf.SENSITIVITY["fast"]["min_agree"]
    cb, cs = _long_setup()
    base = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert base["type"] == "BUY"
    # a bigger target changes the plan; a wider stop buffer moves the stop away
    wide = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m", opts=mtf.opts_for("balanced", {"rr_intraday": 3.0, "sl_buffer": 0.6}))
    assert wide["plan"]["rr"] == 3.0 and wide["plan"]["sl"] < base["plan"]["sl"]
    # overrides ignore unknown keys
    assert "evil" not in mtf.opts_for("balanced", {"evil": 1})


def test_trade_stats_and_streaks():
    t0 = 1_700_000_000
    rs = [2, -1, -1, -1, 2, 2, -1]
    trades = [{"r": r, "result": "WIN" if r > 0 else "LOSS", "side": "BUY" if i % 2 else "SELL", "grade": "A", "entry_time": t0 + i * 3600,
               "exit_time": t0 + i * 3600 + 900, "bars_held": 5} for i, r in enumerate(rs)]
    st = mtf.trade_stats(trades)
    assert st["max_loss_streak"] == 3 and st["max_win_streak"] == 2
    assert st["expectancy_r"] == round(sum(rs) / len(rs), 3) and st["best_r"] == 2 and st["worst_r"] == -1
    assert st["by_side"]["BUY"]["trades"] + st["by_side"]["SELL"]["trades"] == 7
    assert sum(b["n"] for b in st["dist"]) == 7 and st["drawdown"][-1]["dd"] <= 0
    assert mtf.trade_stats([])["expectancy_r"] == 0


def test_backtest_advanced_options_run():
    frames = {tf: demo_feed.klines("BTCUSDT", tf, n) for tf, n in (("4h", 400), ("1h", 700), ("15m", 1500))}
    a = mtf.backtest(frames, "intraday", "15m", 60)
    b = mtf.backtest(frames, "intraday", "15m", 60, slippage_pct=0.1, min_grade="B", breakeven_at_r=1.0,
                     opts=mtf.opts_for("strict"))
    assert "stats" in a and b["trades"] <= a["trades"]
    assert all(t["grade"] in ("B", "A", "A+") for t in b["trade_log"])


def test_votes_and_consensus_gate():
    from app.services import votes
    cs, su = demo_feed.klines("BTCUSDT", "15m", 600), demo_feed.klines("BTCUSDT", "1h", 600)
    dec = {"direction": "bullish", "state": "SIGNAL", "type": "BUY", "bias_tf": "4h", "watch": "w"}
    v = votes.compute({"15m": cs, "1h": su}, "15m", "1h", dec, mtf.TF_MS, None, "best")
    assert [r["id"] for r in v["votes"]][0] == "smc" and len(v["votes"]) == 7
    assert -100 <= v["score"] <= 100 and v["buy"] + v["sell"] + v["neutral"] == 7
    if v["best"]:
        assert next(r for r in v["votes"] if r["id"] == v["best"]["id"])["weight"] == 3.0
    # series are aligned with the candles and only ever -1/0/1
    for fn in votes.SERIES.values():
        out = fn(cs)
        assert len(out) == len(cs) and set(out) <= {-1, 0, 1}
    held = votes.apply_consensus(dec, {"score": -50})
    assert held["type"] == "HOLD" and held["state"] == "SETUP" and held["vote_blocked"]
    assert votes.apply_consensus(dec, {"score": 50})["type"] == "BUY"
    assert votes.apply_consensus({"type": "HOLD"}, {"score": -90})["type"] == "HOLD"


def test_engine_config_roundtrip_and_validation():
    from app.services import engine_config as ec
    assert ec.get()["sens"] == "balanced"
    out = ec.set_({"sens": "fast", "min_agree": 62, "rr_intraday": 2.5, "consensus": True, "junk": 1})
    assert out["sens"] == "fast" and out["min_agree"] == 62 and out["consensus"] is True and "junk" not in out
    assert ec.custom_opts(ec.get()) == {"rr_intraday": 2.5}
    for bad in ({"sens": "wild"}, {"min_agree": 10}, {"rr_scalp": 99}, {"mode": "x"}):
        try:
            ec.set_(bad)
            assert False, bad
        except ValueError:
            pass
    ec.set_({"sens": "balanced", "min_agree": 70, "rr_intraday": None, "consensus": False})


def test_secret_key_is_not_the_public_default():
    from app import config
    assert config.SECRET_KEY and config.SECRET_KEY != "nexusai-super-secret-key-change-in-production" and len(config.SECRET_KEY) >= 32


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS ", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print("FAIL ", name, "->", repr(e))
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
