"""Run with:  python -m tests.test_smc   (from backend/)   or   pytest tests/

Pure-Python, no network. Each test builds a tiny hand-made market so the
expected answer is unambiguous."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import smc  # noqa: E402
from tests.synthetic import candle, from_waypoints  # noqa: E402


def flat(n: int, start: int = 0, base: float = 100.0):
    """Quiet, tiny-bodied candles so 'impulse' is unambiguous later."""
    out = []
    for k in range(n):
        i = start + k
        o = base + (0.03 if k % 2 else -0.03)
        c = base + (-0.03 if k % 2 else 0.03)
        out.append(candle(i, o, max(o, c) + 0.05, min(o, c) - 0.05, c))
    return out


def test_bullish_fvg_detected_and_unfilled():
    cs = flat(30)
    n = len(cs)
    cs += [
        candle(n, 100.0, 100.2, 99.9, 100.1),      # a
        candle(n + 1, 100.1, 101.6, 100.0, 101.5), # b (impulse)
        candle(n + 2, 101.5, 101.9, 101.0, 101.8), # c: low 101.0 > a.high 100.2
    ]
    gaps = smc.find_fair_value_gaps(cs)
    g = next(x for x in gaps if x["kind"] == "bullish" and abs(x["bottom"] - 100.2) < 1e-9)
    assert abs(g["top"] - 101.0) < 1e-9
    assert g["filled"] is False


def test_bullish_order_block_uses_last_down_candle_before_impulse():
    cs = flat(30)
    n = len(cs)
    cs += [
        candle(n, 100.1, 100.15, 99.85, 99.9),     # last DOWN candle = origin
        candle(n + 1, 99.9, 101.7, 99.85, 101.5),  # big bullish impulse
        candle(n + 2, 101.5, 102.0, 101.4, 101.9),
    ]
    obs = smc.find_order_blocks(cs)
    ob = next(b for b in obs if b["kind"] == "bullish" and not b["mitigated"])
    assert abs(ob["top"] - 100.1) < 1e-9 and abs(ob["bottom"] - 99.9) < 1e-9
    assert ob["formed_at"] == int(cs[n + 1]["time"] / 1000)


def _ob_scenario(retest_low: float, retest_close: float):
    cs = flat(30)
    n = len(cs)
    cs += [
        candle(n, 100.1, 100.15, 99.85, 99.9),
        candle(n + 1, 99.9, 101.7, 99.85, 101.5),
        candle(n + 2, 101.5, 102.0, 101.4, 101.9),
        candle(n + 3, 101.9, 102.3, 101.8, 102.2),
        candle(n + 4, 102.2, 102.4, 101.9, 102.0),
        candle(n + 5, 102.0, 102.1, 101.5, 101.6),
        candle(n + 6, 101.6, 101.7, 100.9, 101.0),
        candle(n + 7, 101.0, 101.1, retest_low, retest_close),  # comes back into the OB
    ]
    return cs


def test_retest_of_order_block_is_found_and_marked_active():
    cs = _ob_scenario(100.0, 100.08)
    snap = smc.analyze(cs)
    rts = [r for r in snap["retests"] if r["source"] == "OB" and r["kind"] == "bullish"]
    assert rts, "expected a retest of the bullish order block"
    assert rts[-1]["active"] is True
    assert rts[-1]["time"] == int(cs[-1]["time"] / 1000)


def test_bot_buys_the_retest_and_says_why():
    cs = _ob_scenario(100.0, 100.08)
    d = smc.evaluate_entry(cs)
    assert d["type"] == "BUY", d
    assert d["trigger"].startswith("retest")
    assert "retest" in d["reason"].lower()
    kinds = {s["step"] for s in d["steps"]}
    assert {"STRUCTURE", "SWEEP", "ZONES", "RETEST", "DECISION"} <= kinds
    assert d["invalidation"] is not None and d["invalidation"] < cs[-1]["close"]


def test_no_retest_no_trade():
    cs = _ob_scenario(101.0, 101.05)  # stays well above the block
    d = smc.evaluate_entry(cs)
    assert not any(s["step"] == "RETEST" and s["status"] == "pass"
                   and "OB" in s["message"] for s in d["steps"])


def test_close_through_block_kills_it_and_its_retest():
    cs = _ob_scenario(100.0, 100.08)
    n = len(cs)
    # Next bar closes clean through the bottom of the block -> mitigated.
    cs.append(candle(n, 100.08, 100.1, 99.4, 99.5))
    obs = smc.find_order_blocks(cs)
    assert all(b["mitigated"] for b in obs if b["kind"] == "bullish" and abs(b["top"] - 100.1) < 1e-9)
    assert not any(r["source"] == "OB" and abs(r["top"] - 100.1) < 1e-9 for r in smc.analyze(cs)["retests"])


def test_swings_need_lookback_bars_after_them_so_no_lookahead():
    # A peak on the second-to-last bar cannot be a confirmed swing yet.
    cs = flat(20)
    n = len(cs)
    cs.append(candle(n, 100.0, 105.0, 99.95, 100.0))   # spike high...
    cs.append(candle(n + 1, 100.0, 100.1, 99.95, 100.0))  # ...only 1 bar after it
    highs = [s for s in smc.find_swings(cs) if s["kind"] == "high"]
    assert not any(abs(s["price"] - 105.0) < 1e-9 for s in highs)


def test_structure_labels_and_trend():
    pts = [(0, 100), (15, 103), (25, 101), (40, 106), (50, 103.5), (65, 110), (75, 107), (90, 113), (100, 110.5)]
    cs = from_waypoints(pts)
    ms = smc.find_market_structure(cs)
    labels = [s["label"] for s in ms["swings"] if s["label"]]
    assert "HH" in labels and "HL" in labels
    assert ms["trend"] == "bullish"

    down = from_waypoints([(0, 113), (15, 110), (25, 112), (40, 107), (50, 109.5), (65, 103), (75, 106), (90, 100), (100, 102.5)])
    ms = smc.find_market_structure(down)
    assert "LL" in [s["label"] for s in ms["swings"]]
    assert ms["trend"] == "bearish"


def test_bearish_structure_blocks_long_entries_even_on_a_retest():
    down = from_waypoints([(0, 113), (15, 110), (25, 112), (40, 107), (50, 109.5), (65, 103), (75, 106), (90, 100), (100, 102.5)])
    d = smc.evaluate_entry(down)
    assert d["type"] == "HOLD"
    st = next(s for s in d["steps"] if s["step"] == "STRUCTURE")
    assert st["status"] == "block"


def test_bullish_liquidity_sweep_of_swing_low():
    cs = flat(30, base=101.0)
    n = len(cs)
    # a clean swing low at 99.0 (3 higher lows each side)
    seq = [100.6, 100.3, 100.0, 99.0, 100.0, 100.3, 100.6]
    for k, low in enumerate(seq):
        cs.append(candle(n + k, low + 0.3, low + 0.5, low, low + 0.35))
    m = len(cs)
    for k in range(4):
        cs.append(candle(m + k, 101.0, 101.3, 100.8, 101.1))
    m = len(cs)
    # the sweep: wick under 99.0, close back above it
    cs.append(candle(m, 100.9, 101.0, 98.6, 100.4))
    snap = smc.analyze(cs)
    sw = [s for s in snap["sweeps"] if s["kind"] == "bullish" and s["time"] == int(cs[-1]["time"] / 1000)]
    assert sw, snap["sweeps"]
    assert abs(sw[0]["level"] - 99.0) < 1e-9 and sw[0]["wick"] < 99.0
    d = smc.evaluate_entry(cs, snap)
    assert d["type"] == "BUY" and d["trigger"] in ("sweep", "sweep+retest")


def test_short_history_holds_instead_of_crashing():
    d = smc.evaluate_entry(flat(10))
    assert d["type"] == "HOLD" and d["steps"]


def test_every_step_has_a_stable_signature_for_log_dedupe():
    d = smc.evaluate_entry(_ob_scenario(100.0, 100.08))
    assert all(isinstance(s["sig"], str) and s["sig"] for s in d["steps"])


def test_the_bar_that_creates_a_zone_is_not_its_own_retest():
    # Order block: the impulse candle opens at the block, so it "touches" it.
    cs = flat(30)
    n = len(cs)
    cs += [candle(n, 100.1, 100.15, 99.85, 99.9), candle(n + 1, 99.9, 101.7, 99.85, 101.5)]
    d = smc.evaluate_entry(cs)
    assert d["type"] == "HOLD", d["reason"]
    assert not any(r["source"] == "OB" for r in smc.analyze(cs)["retests"])

    # Fair value gap: its top IS the third candle's low, so it touches itself.
    cs = flat(30)
    n = len(cs)
    cs += [candle(n, 100.0, 100.2, 99.9, 100.1), candle(n + 1, 100.1, 101.6, 100.0, 101.5),
           candle(n + 2, 101.5, 101.9, 101.0, 101.8)]
    d = smc.evaluate_entry(cs)
    assert not any(s["step"] == "RETEST" and s["status"] == "pass" for s in d["steps"]), d["steps"]


def test_one_origin_candle_makes_one_order_block():
    cs = flat(30)
    n = len(cs)
    cs += [
        candle(n, 100.1, 100.15, 99.85, 99.9),       # origin
        candle(n + 1, 99.9, 101.7, 99.85, 101.5),    # impulse 1
        candle(n + 2, 101.5, 103.4, 101.4, 103.3),   # impulse 2 -> same origin
        candle(n + 3, 103.3, 104.6, 103.2, 104.5),   # impulse 3 -> same origin
    ]
    origin_t = int(cs[n]["time"] / 1000)
    same = [b for b in smc.find_order_blocks(cs) if b["time"] == origin_t]
    assert len(same) == 1, same
    assert same[0]["formed_at"] == int(cs[n + 1]["time"] / 1000)  # earliest formation kept


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
