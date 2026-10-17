"""Key levels, AMD, confirmation strategies, approvals / backtest gate, breakeven, alerts.
Run:  python -m tests.test_signal_flow   (from backend/)"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CRYPTO_DEMO"] = "1"

aiohttp = types.ModuleType("aiohttp")


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    def get(self, *a, **k): raise ConnectionError("exchange unreachable (test)")


aiohttp.ClientSession = _Session
aiohttp.ClientTimeout = lambda **kw: None
sys.modules["aiohttp"] = aiohttp

from app import config  # noqa: E402

config.DB_PATH = Path(tempfile.mkdtemp()) / "flow.db"

from app import database  # noqa: E402
from app.services import amd, approvals, candle_store, confirmations, levels, mtf, scanner  # noqa: E402

database.init_db()

H = 3600
DAY0 = (1_700_000_000 // 86_400 + 1) * 86_400            # a UTC midnight


def hc(t_s, o, h, l, c, v=100.0):
    return {"time": int(t_s * 1000), "open": o, "high": max(h, o, c), "low": min(l, o, c), "close": c, "volume": v}


def day_h1(day, base, hi_hour=None, hi=None, lo_hour=None, lo=None, upto=24):
    """24 hourly bars flat around `base`, optionally with one spike high / one spike low."""
    out = []
    for h in range(upto):
        t = DAY0 + day * 86_400 + h * H
        high, low = base + 1, base - 1
        if hi_hour == h:
            high = hi
        if lo_hour == h:
            low = lo
        out.append(hc(t, base, high, low, base))
    return out


def daily(day, o, h, l, c):
    return hc(DAY0 + day * 86_400, o, h, l, c, 5000)


# ── key levels ──────────────────────────────────────────────────────────────
def _lvl_by_id(ls):
    return {l["id"]: l for l in ls}


def test_previous_day_week_month_levels_and_sweep_flags():
    d1 = [daily(-40 + k, 100, 105 + (k % 3), 95 - (k % 2), 100) for k in range(40)]    # 40 closed days incl. last month
    d1.append(daily(0, 100, 120, 90, 110))                                         # yesterday: H 120 L 90
    h1 = day_h1(0, 105, upto=24) + day_h1(1, 108, hi_hour=9, hi=119, lo_hour=5, lo=96, upto=15)   # today to 14:00, no sweep
    now = DAY0 + 86_400 + 15 * H
    L = _lvl_by_id(levels.build_levels(h1, d1, now))
    assert L["PDH"]["price"] == 120 and not L["PDH"]["swept"]
    assert L["PDL"]["price"] == 90 and not L["PDL"]["swept"]
    assert L["DO"]["price"] == 108 and L["DH"]["price"] == 119 and L["DL"]["price"] == 96
    assert {"PWH", "PWL", "WO", "MO", "VWAP"} <= set(L)
    # price then takes out yesterday's high -> PDH becomes spent liquidity
    h1b = h1[:-1] + [hc(DAY0 + 86_400 + 14 * H, 108, 121, 107, 109)]
    L2 = _lvl_by_id(levels.build_levels(h1b, d1, now))
    assert L2["PDH"]["swept"] and not L2["PDL"]["swept"]
    assert all(p != 120 for p, _ in levels.liquidity_above(list(L2.values()), 109))   # swept highs are not targets


def test_session_ranges_and_status():
    d1 = [daily(-30 + k, 100, 110, 90, 100) for k in range(31)]
    h1 = day_h1(0, 100) + day_h1(1, 100, hi_hour=3, hi=104, lo_hour=6, lo=97, upto=16)   # Asia 00-08 has the 104 / 97 extremes
    L = _lvl_by_id(levels.build_levels(h1, d1, DAY0 + 86_400 + 16 * H))
    assert L["ASIA_H"]["price"] == 104 and L["ASIA_L"]["price"] == 97
    assert not L["ASIA_H"]["active"]                         # Asia finished at 08:00
    assert L["NY_H"]["active"]                               # 16:00 is inside New York (12:00-21:00)
    st = {s["name"]: s for s in levels.session_status(DAY0 + 10 * H)}
    assert st["Asia"]["open"] is False and st["London"]["open"] is True and st["New York"]["open"] is False
    assert levels.session_status(DAY0 + 13 * H)[1]["open"] and levels.session_status(DAY0 + 13 * H)[2]["open"]   # overlap


def test_round_numbers_scale_with_price():
    btc = [l["price"] for l in levels.round_levels(67_420)]
    assert 67_000 in btc and 68_000 in btc
    xrp = [round(l["price"], 4) for l in levels.round_levels(0.5923)]
    assert 0.59 in xrp and 0.6 in xrp


def test_key_level_sweep_detects_a_stop_run_that_closed_back():
    lvls = [{"id": "PDL", "label": "PDL", "price": 100.0, "kind": "low", "group": "daily", "t": DAY0, "swept": False, "active": False}]
    cs = [hc(DAY0 + i * 900, 101, 102, 100.5, 101.5) for i in range(10)]
    cs.append(hc(DAY0 + 10 * 900, 101, 101.4, 99.2, 100.8))      # wick under 100, closes back above
    sw = levels.key_level_sweep(lvls, cs, "bullish")
    assert sw and sw["label"] == "PDL" and sw["wick"] == 99.2
    assert levels.key_level_sweep(lvls, cs, "bearish") is None


# ── AMD ─────────────────────────────────────────────────────────────────────
def _amd_series(kind="bullish", with_distribution=True):
    """40 bars trend-in, 16-bar tight range 100-102, a stop-run of one edge, then (optionally) expansion."""
    cs, t = [], DAY0
    p = 110.0
    for i in range(30):                                  # drift down into the range
        cs.append(hc(t, p, p + 0.4, p - 0.6, p - 0.3)); p -= 0.3; t += 900
    for i in range(16):                                  # accumulation 100-102 (touches both edges)
        lo, hi = 100.0, 102.0
        o = 101.0 + (0.6 if i % 2 else -0.6)
        cs.append(hc(t, o, hi if i % 4 == 1 else o + 0.3, lo if i % 4 == 3 else o - 0.3, 101.0)); t += 900
    if kind == "bullish":
        cs.append(hc(t, 101.0, 101.3, 98.9, 100.9)); t += 900            # sweeps the LOW and closes back inside
        if with_distribution:
            for k in range(4):
                cs.append(hc(t, 101 + k * 1.6, 103 + k * 1.6, 100.9 + k * 1.6, 102.8 + k * 1.6)); t += 900
    else:
        cs.append(hc(t, 101.0, 103.1, 100.8, 101.1)); t += 900           # sweeps the HIGH and closes back inside
        if with_distribution:
            for k in range(4):
                cs.append(hc(t, 101 - k * 1.6, 101.2 - k * 1.6, 99 - k * 1.6, 99.2 - k * 1.6)); t += 900
    return cs


def test_amd_bullish_manipulation_then_distribution():
    m = amd.detect_amd(_amd_series("bullish", with_distribution=False))
    assert m["phase"] == "MANIPULATION" and m["direction"] == "bullish", m
    assert m["manipulation"]["side"] == "low" and m["manipulation"]["wick"] < 100.0
    d = amd.detect_amd(_amd_series("bullish"))
    assert d["phase"] == "DISTRIBUTION" and d["direction"] == "bullish" and d["distribution"]["target"] > 102


def test_amd_bearish_mirror_and_accumulation_only():
    d = amd.detect_amd(_amd_series("bearish"))
    assert d["phase"] == "DISTRIBUTION" and d["direction"] == "bearish" and d["manipulation"]["side"] == "high"
    acc = _amd_series("bullish", with_distribution=False)[:-1]
    a = amd.detect_amd(acc)
    assert a["phase"] == "ACCUMULATION" and a["direction"] is None and a["range"]["bars"] >= 8


def test_amd_ignores_a_plain_breakout_and_trends():
    cs = _amd_series("bullish", with_distribution=False)[:-1]
    t = cs[-1]["time"] / 1000 + 900
    for k in range(5):                                    # closes straight out of the range with no stop-run
        cs.append(hc(t + k * 900, 102 + k, 103.5 + k, 101.8 + k, 103.2 + k))
    assert amd.detect_amd(cs)["phase"] in ("NONE",), amd.detect_amd(cs)
    trend = [hc(DAY0 + i * 900, 100 + i, 100.8 + i, 99.6 + i, 100.5 + i) for i in range(80)]
    assert amd.detect_amd(trend)["phase"] == "NONE"


def test_session_amd_asia_range_london_sweep():
    cs = []
    for i in range(32):                                   # Asia 00:00-08:00 on 15m bars, range 100-102
        o = 101 + (0.5 if i % 2 else -0.5)
        cs.append(hc(DAY0 + i * 900, o, 102 if i % 6 == 1 else o + 0.2, 100 if i % 6 == 4 else o - 0.2, 101))
    for k in range(6):                                    # London: runs the Asia low, closes back, then expands up
        t = DAY0 + 8 * H + k * 900
        if k == 0:
            cs.append(hc(t, 101, 101.2, 99.3, 100.8))
        else:
            cs.append(hc(t, 101 + k * 1.2, 102.4 + k * 1.2, 100.9 + k * 1.2, 102.2 + k * 1.2))
    out = amd.session_amd(cs, DAY0 + 10 * H)
    assert out["phase"] == "DISTRIBUTION" and out["direction"] == "bullish", out
    only_asia = amd.session_amd(cs[:32], DAY0 + 6 * H + 3600)
    assert only_asia["phase"] == "ACCUMULATION"


# ── confirmation strategies ─────────────────────────────────────────────────
def _trend(n=120, slope=0.2, wob=0.4):
    return [hc(DAY0 + i * 900, 100 + i * slope + (wob if i % 2 else -wob), 100.5 + i * slope + wob, 99.5 + i * slope - wob,
               100 + i * slope + (0.1 if i % 2 else -0.1), 100 + (i % 5)) for i in range(n)]


def test_confirmations_support_with_trend_and_oppose_against():
    up = _trend(slope=0.2)
    assert confirmations.evaluate(["EMA Trend"], up, "bullish")["votes"][0]["vote"] == "support"
    assert confirmations.evaluate(["EMA Trend"], up, "bearish")["votes"][0]["vote"] == "oppose"
    dn = _trend(slope=-0.2)
    assert confirmations.evaluate(["EMA Trend"], dn, "bearish")["votes"][0]["vote"] == "support"
    out = confirmations.evaluate(list(confirmations.NAMES), up, "bullish")
    assert len(out["votes"]) == 5 and out["supports"] - out["opposes"] == out["net"]
    json.dumps(out)
    assert confirmations.evaluate(["Nope"], up, "bullish")["votes"] == []


# ── engine with the approved-strategy gate + key-level confluence ──────────
from tests.test_mtf import _entry, _long_setup, ZONE_FORMED  # noqa: E402


def test_confirmation_gate_blocks_when_strategies_disagree():
    cb, cs = _long_setup()
    ent = _entry()
    base = mtf.evaluate_style("intraday", cb, cs, ent, "15m")
    assert base["type"] == "BUY"
    # an extra with a strategy list that cannot be satisfied (min 5 net votes) must stop the signal
    extra = {"levels": [], "confirms": ["EMA Trend", "MACD Momentum"], "min_confirms": 5}
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m", extra=extra)
    chk = next(c for c in r["checks"] if c["id"] == "confirm")
    assert chk["gate"] and chk["ok"] is False
    assert r["type"] == "HOLD" and r["confirmations"]["votes"]
    # with the bar at 0 the same setup fires again and the votes are reported
    ok = mtf.evaluate_style("intraday", cb, cs, ent, "15m", extra={"levels": [], "confirms": ["EMA Trend"], "min_confirms": -5})
    assert ok["type"] == "BUY"


def test_unswept_key_level_in_the_path_blocks_a_long():
    cb, cs = _long_setup()
    ent = _entry()
    price = ent[-1]["close"]
    wall = {"id": "PDH", "label": "PDH", "price": price + 0.6, "kind": "high", "group": "daily", "t": ZONE_FORMED, "swept": False, "active": False}
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m", extra={"levels": [wall], "confirms": [], "min_confirms": 0})
    plan_chk = next(c for c in r["checks"] if c["id"] == "plan")
    assert plan_chk["ok"] is False and "PDH" in plan_chk["detail"] and r["type"] == "HOLD"
    far = dict(wall, price=price + 30)
    r2 = mtf.evaluate_style("intraday", cb, cs, ent, "15m", extra={"levels": [far], "confirms": [], "min_confirms": 0})
    assert r2["type"] == "BUY" and r2["plan"]["ladder"][0]["label"] == "PDH"


def test_zone_on_a_key_level_scores_confluence():
    cb, cs = _long_setup()
    ent = _entry()
    pdl = {"id": "PDL", "label": "PDL", "price": 100.5, "kind": "low", "group": "daily", "t": ZONE_FORMED, "swept": False, "active": False}
    r = mtf.evaluate_style("intraday", cb, cs, ent, "15m", extra={"levels": [pdl], "confirms": [], "min_confirms": 0})
    chk = next(c for c in r["checks"] if c["id"] == "levels")
    assert chk["ok"] is True and "PDL" in chk["detail"]


# ── approvals ───────────────────────────────────────────────────────────────
S = dict(database.DEFAULT_SETTINGS)


def _res(trades, pf, net, recent_net=1.0, wr=55.0, dd=3.0):
    return {"trades": trades, "profit_factor": pf, "net_r": net, "win_rate": wr, "max_drawdown_r": dd,
            "recent": {"trades": max(3, trades // 3), "net_r": recent_net}, "wins": int(trades * wr / 100), "losses": 1,
            "scratches": 0, "avg_r": net / max(1, trades), "gross_win": pf * 10 if pf else 0, "gross_loss": 10, "worst_losing_streak": 2}


def test_judge_rules():
    assert approvals.judge(_res(25, 1.6, 9.0), S)[0] == "APPROVED"
    assert approvals.judge(_res(25, 0.9, -3.0), S)[0] == "REJECTED"
    assert approvals.judge(_res(25, 1.5, 6.0, recent_net=-2.0), S)[0] == "REJECTED"      # edge faded recently
    v, why = approvals.judge(_res(4, 3.0, 5.0), S)
    assert v == "INSUFFICIENT" and "4 trades" in why[0]


def test_pooled_verdict_lets_thin_pairs_borrow_the_strategy_result():
    s = {**S, "bt_min_trades": 10, "bt_min_pf": 1.2}
    def row(trades, net, gw, gl, verdict="INSUFFICIENT"):
        return {"verdict": verdict, "updated_at": 9e9, "trades": trades, "net_r": net, "gross_win": gw, "gross_loss": gl, "wins": trades // 2, "why": []}
    v = {("AAAUSDT", "intraday", "15m"): row(6, 4.0, 8, 4), ("BBBUSDT", "intraday", "15m"): row(7, 3.0, 9, 6),
         ("CCCUSDT", "intraday", "15m"): row(5, 2.0, 7, 5)}
    e = approvals.effective("AAAUSDT", "intraday", "15m", s, v)
    assert e["verdict"] == "APPROVED" and e["basis"] == "pooled" and e["pool"]["trades"] == 18
    # a pair with a clearly losing record is vetoed even though the pool is fine
    v[("AAAUSDT", "intraday", "15m")] = row(6, -3.0, 2, 5)
    v[("BBBUSDT", "intraday", "15m")] = row(9, 6.0, 12, 5)
    assert approvals.effective("AAAUSDT", "intraday", "15m", s, v)["verdict"] == "REJECTED"
    # not enough evidence anywhere -> blocked
    assert approvals.effective("AAAUSDT", "scalp", "5m", s, {("AAAUSDT", "scalp", "5m"): row(3, 1, 2, 1)})["verdict"] == "INSUFFICIENT"
    assert approvals.effective("ZZZUSDT", "intraday", "15m", s, v)["verdict"] == "PENDING"


def test_scanner_gate_blocks_unapproved_and_allows_approved():
    cb, cs = _long_setup()
    base = mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")
    assert base["type"] == "BUY"
    s = {**S, "require_backtest": True}
    ok = {("GATEUSDT", "intraday", "15m"): {"verdict": "APPROVED", "updated_at": 9e9, "trades": 30, "net_r": 8, "gross_win": 20, "gross_loss": 12, "wins": 17, "why": []}}
    r = scanner.gate_result("GATEUSDT", json.loads(json.dumps(base)), s, ok)
    assert r["type"] == "BUY" and r["approval"]["allowed"]
    bad = {("GATEUSDT", "intraday", "15m"): {"verdict": "REJECTED", "updated_at": 9e9, "trades": 30, "net_r": -4, "gross_win": 9, "gross_loss": 13, "wins": 10, "why": ["PF low"]}}
    r2 = scanner.gate_result("GATEUSDT", json.loads(json.dumps(base)), s, bad)
    assert r2["type"] == "HOLD" and r2["state"] == "SETUP" and "rejected" in r2["blocked"] and r2["targetPrice"] is None
    r3 = scanner.gate_result("NEWUSDT", json.loads(json.dumps(base)), s, {})                 # no backtest yet
    assert r3["type"] == "HOLD" and "backtest" in r3["blocked"]
    assert ("NEWUSDT", "intraday", "15m") in approvals._pending                              # queued to run
    off = scanner.gate_result("GATEUSDT", json.loads(json.dumps(base)), {**s, "require_backtest": False}, {})
    assert off["type"] == "BUY"
    short = json.loads(json.dumps(base))
    short["type"] = "SELL"
    assert scanner.gate_result("GATEUSDT", short, {**s, "long_only": True, "require_backtest": False}, {})["type"] == "HOLD"


def test_unapproved_strategy_never_fires():
    cb, cs = _long_setup()
    frames = {"4h": [], "1h": [], "15m": []}
    # a style that is not approved is stood aside by evaluate_all itself
    import time
    fr = {tf: __import__("app.services.demo_feed", fromlist=["x"]).klines("BTCUSDT", tf, n) for tf, n in (("4h", 400), ("1h", 900), ("15m", 400), ("1d", 120), ("5m", 400))}
    res = mtf.evaluate_all(fr, "combined", "15m", 70, time.time() * 1000, None, {"styles": ["intraday"], "confirms": [], "min_confirms": 0})
    assert res["styles"]["scalp"]["state"] == "STAND_ASIDE" and "not approved" in res["styles"]["scalp"]["watch"]


# ── breakeven + alerts ──────────────────────────────────────────────────────
def test_breakeven_turns_a_would_be_loss_into_a_scratch():
    cb, cs = _long_setup()
    dec = json.loads(json.dumps(mtf.evaluate_style("intraday", cb, cs, _entry(), "15m")))
    dec["time"] = int(dec["time"]) + 777_000
    sid = scanner.record_signal("BEUSDT", dec, {**S, "breakeven_r": 1.0})
    p = dec["plan"]
    risk = p["entry"] - p["sl"]
    after = int(dec["time"])
    bars = [hc(after + 900, p["entry"], p["entry"] + risk * 1.2, p["entry"] - 0.01, p["entry"] + risk),            # runs +1.2R first
            hc(after + 1800, p["entry"] + risk, p["entry"] + risk, p["sl"] - 1, p["sl"])]                         # then falls through everything
    candle_store.upsert("BEUSDT", "15m", bars)
    closed = scanner.resolve_signals()
    row = [s for s in closed if s["id"] == sid][0]
    assert row["status"] == "BE" and row["r_result"] == 0.0 and row["exit_price"] == p["entry"]


def test_alerts_feed_and_unread_counter():
    a = database.add_alert("SIGNAL", "BUY BTCUSDT", "body", "BTCUSDT", 1)
    b = database.add_alert("WIN", "Target hit", "", "BTCUSDT", 1)
    assert b > a
    conn = database.get_db()
    n = conn.execute("SELECT COUNT(*) FROM alerts WHERE id>?", (a - 1,)).fetchone()[0]
    conn.close()
    assert n >= 2


def test_settings_roundtrip_and_password_hash():
    out = database.set_settings({"min_agree": 80, "watchlist": ["BTCUSDT"], "bogus": 1})
    assert out["min_agree"] == 80 and out["watchlist"] == ["BTCUSDT"] and "bogus" not in out
    database.set_settings({"min_agree": 70, "watchlist": config.PAIRS[:8]})
    from app import security
    h = security.hash_password("secret1")
    assert security.verify_password("secret1", h) and not security.verify_password("nope", h) and "$" in h


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)
             and f.__module__ == __name__]
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
