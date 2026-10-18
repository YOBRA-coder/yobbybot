"""
Multi-timeframe top-down SMC engine — ported from the GoldSignal strategy
(4H direction -> 1H setup -> lower-timeframe trigger) and extended with a
SCALP style, so one engine covers both.

    STEP 1  Direction   BIAS TF   BOS/CHoCH structure + swing labels -> bullish / bearish / neutral
    STEP 2  Map         BIAS TF   unmitigated zones, premium/discount, opposing liquidity (room to target)
    STEP 3  Setup       SETUP TF  structure must AGREE with the bias, price must be at an aligned
                                  order block / FVG / supply-demand zone; sweeps and zone confluence add weight
    STEP 4  Trigger     ENTRY TF  engulfing / rejection wick / displacement candle after the zone touch,
                                  plus mini BOS-CHoCH, volume and kill-zone confirmation

    style      bias   setup   entry (you choose)
    intraday   4h     1h      5m / 15m / 30m   (default 15m)
    scalp      1h     15m     1m / 3m / 5m     (default 5m)
    combined   runs both and reports the stronger one

Each rule is a weighted *check*. GATES are mandatory (bias, setup agrees,
price at a zone, trigger candle, sane stop + room to target). The agreement %
is the weighted share of all applicable checks. A signal fires only when every
gate passes AND agreement >= `min_agree` (default 70).

Everything reads only the candles it is given (no look-ahead), and decisions
use CLOSED entry candles — so `backtest()` below replays exactly what the live
scanner would have done.
"""
import bisect
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import smc
from .indicators import calc_atr

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}

STYLES: Dict[str, Dict] = {
    "intraday": {
        "label": "Intraday", "bias_tf": "4h", "setup_tf": "1h", "entry_tfs": ("5m", "15m", "30m"),
        "default_entry": "15m", "touch_bars": 6, "trigger_bars": 3, "rr": 2.0, "min_rr": 1.5,
    },
    "scalp": {
        "label": "Scalp", "bias_tf": "1h", "setup_tf": "15m", "entry_tfs": ("1m", "3m", "5m"),
        "default_entry": "5m", "touch_bars": 8, "trigger_bars": 3, "rr": 1.5, "min_rr": 1.2,
    },
}
MODES = ("intraday", "scalp", "combined")
DEFAULT_MIN_AGREE = 70
ENTRY_TFS = ("1m", "3m", "5m", "15m", "30m")
# Sensitivity presets — how picky the engine is. `min_agree` is applied by the caller (the UI sets it
# from the preset); the rest is read inside evaluate_style via `opts`.
SENSITIVITY: Dict[str, Dict] = {
    "strict":   {"min_agree": 80, "gate_sweep": True, "gate_killzone": False, "trigger_bars": 2, "touch_mult": 0.75,
                 "label": "Strict · quality over quantity: needs a liquidity sweep, a fresh trigger and 80% agreement"},
    "balanced": {"min_agree": 70, "gate_sweep": False, "gate_killzone": False, "trigger_bars": None, "touch_mult": 1.0,
                 "label": "Balanced · the default GoldSignal rules"},
    "fast":     {"min_agree": 60, "gate_sweep": False, "gate_killzone": False, "trigger_bars": 5, "touch_mult": 1.5,
                 "label": "Fast · earlier, more signals: wider touch window, older triggers still count, 60% agreement"},
}
OPT_KEYS = ("gate_sweep", "gate_killzone", "trigger_bars", "touch_mult", "rr_intraday", "rr_scalp", "sl_buffer")


def opts_for(sens: str = "balanced", custom: Optional[Dict] = None) -> Dict:
    """Engine options for a sensitivity preset, with optional overrides (rr_*, sl_buffer, gates...)."""
    o = {k: v for k, v in SENSITIVITY.get(sens, SENSITIVITY["balanced"]).items() if k in OPT_KEYS and v is not None}
    for k, v in (custom or {}).items():
        if k in OPT_KEYS and v is not None:
            o[k] = v
    return o


MIN_RISK_PCT = 0.0015      # stops tighter than 0.15% are eaten by fees
MAX_RISK_PCT = 0.06


def needed_timeframes(mode: str, entry_tf: str) -> List[str]:
    """Which candle series the engine needs for this mode/entry TF."""
    tfs: List[str] = []
    for name in _styles_for(mode):
        st = STYLES[name]
        for tf in (st["bias_tf"], st["setup_tf"], _entry_for(name, entry_tf)):
            if tf not in tfs:
                tfs.append(tf)
    return tfs


def _styles_for(mode: str) -> List[str]:
    return ["intraday", "scalp"] if mode == "combined" else [mode if mode in STYLES else "intraday"]


def _entry_for(style: str, entry_tf: str) -> str:
    """The entry TF the user picked, kept inside what that style can sensibly use."""
    st = STYLES[style]
    if entry_tf in st["entry_tfs"]:
        return entry_tf
    # Combined mode: a 15m/30m pick belongs to intraday; scalp falls back to its own default.
    return st["default_entry"] if style == "scalp" else (entry_tf if entry_tf in ENTRY_TFS else st["default_entry"])


# ── small helpers ───────────────────────────────────────────────────────────
def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"


def killzone(ts_s: float) -> Optional[str]:
    """Liquidity windows that matter for crypto too (UTC): London open, NY open/overlap."""
    d = datetime.fromtimestamp(ts_s, tz=timezone.utc)
    h = d.hour + d.minute / 60
    if d.weekday() >= 5:
        return None
    if 7 <= h < 10.5:
        return "London"
    if 12.5 <= h < 16:
        return "New York"
    return None


def _check(cid: str, step: int, label: str, ok: Optional[bool], weight: int, detail: str, gate: bool = False) -> Dict:
    """ok=None means 'not applicable' (skipped from the score)."""
    return {"id": cid, "step": step, "label": label, "ok": ok, "weight": weight, "gate": gate, "detail": detail}


def _zone_dir(kind: str) -> str:
    return "bullish" if kind in ("bullish", "demand") else "bearish"


def _aligned_zones(ctx: Dict, tf: str, direction: str) -> List[Dict]:
    """Live zones of the wanted direction from one timeframe's analysis."""
    out: List[Dict] = []
    for b in ctx.get("order_blocks", []):
        if not b["mitigated"] and b["kind"] == direction:
            out.append({"src": "OB", "tf": tf, "top": b["top"], "bottom": b["bottom"], "time": b["time"],
                        "formed_at": b.get("formed_at", b["time"]), "fresh": True, "strength": b.get("strength", 1.0)})
    for g in ctx.get("fvgs", []):
        if not g["filled"] and g["kind"] == direction:
            out.append({"src": "FVG", "tf": tf, "top": g["top"], "bottom": g["bottom"], "time": g["time"],
                        "formed_at": g["time"], "fresh": g["fill_pct"] < 50, "strength": 1.0})
    for z in ctx.get("sd_zones", []):
        if _zone_dir(z["kind"]) == direction and z["touches"] <= 1:
            out.append({"src": "S/D", "tf": tf, "top": z["top"], "bottom": z["bottom"], "time": z["time"],
                        "formed_at": z.get("formed_at", z["time"]), "fresh": z["fresh"], "strength": 1.0})
    return out


def _touch(c: dict, z: Dict, direction: str) -> bool:
    if direction == "bullish":
        return c["low"] <= z["top"] and c["close"] > z["bottom"]
    return c["high"] >= z["bottom"] and c["close"] < z["top"]


def _is_trigger(prev: dict, c: dict, direction: str, avg_body: float) -> Optional[str]:
    rng = c["high"] - c["low"]
    if rng <= 0:
        return None
    body = abs(c["close"] - c["open"])
    if direction == "bullish":
        if c["close"] > c["open"] and prev["close"] < prev["open"] and c["close"] >= prev["open"] and c["open"] <= prev["close"]:
            return "bullish engulfing"
        lower = min(c["open"], c["close"]) - c["low"]
        if lower >= 0.55 * rng and c["close"] >= c["low"] + 0.6 * rng:
            return "bullish rejection wick"
        if c["close"] > c["open"] and body >= 1.5 * avg_body and c["close"] > prev["high"]:
            return "bullish displacement candle"
    else:
        if c["close"] < c["open"] and prev["close"] > prev["open"] and c["close"] <= prev["open"] and c["open"] >= prev["close"]:
            return "bearish engulfing"
        upper = c["high"] - max(c["open"], c["close"])
        if upper >= 0.55 * rng and c["close"] <= c["high"] - 0.6 * rng:
            return "bearish rejection wick"
        if c["close"] < c["open"] and body >= 1.5 * avg_body and c["close"] < prev["low"]:
            return "bearish displacement candle"
    return None


def _opposing_levels(ctxs: List[Tuple[str, Dict]], direction: str, price: float) -> List[Tuple[float, str]]:
    """Nearest liquidity / opposing supply-demand in the trade's path, nearest first."""
    out: List[Tuple[float, str]] = []
    for tf, ctx in ctxs:
        opp = "bearish" if direction == "bullish" else "bullish"
        for z in _aligned_zones(ctx, tf, opp):
            lvl = z["bottom"] if direction == "bullish" else z["top"]
            out.append((lvl, f"{tf} {z['src']}"))
        for e in ctx.get("eq", []):
            if e["swept"]:
                continue
            if direction == "bullish" and e["kind"] == "EQH":
                out.append((e["price"], f"{tf} equal highs"))
            if direction == "bearish" and e["kind"] == "EQL":
                out.append((e["price"], f"{tf} equal lows"))
        for s in ctx.get("structure", {}).get("swings", []):
            if direction == "bullish" and s["kind"] == "high":
                out.append((s["price"], f"{tf} swing high"))
            if direction == "bearish" and s["kind"] == "low":
                out.append((s["price"], f"{tf} swing low"))
    if direction == "bullish":
        out = sorted((o for o in out if o[0] > price), key=lambda o: o[0])
    else:
        out = sorted((o for o in out if o[0] < price), key=lambda o: -o[0])
    return out


def _grade(agree: float) -> str:
    return "A+" if agree >= 90 else "A" if agree >= 80 else "B" if agree >= 70 else "C" if agree >= 55 else "D"


def _empty_result(style: str, entry_tf: str, reason: str) -> Dict:
    st = STYLES[style]
    return {
        "style": style, "label": st["label"], "bias_tf": st["bias_tf"], "setup_tf": st["setup_tf"], "entry_tf": entry_tf,
        "type": "HOLD", "state": "STAND_ASIDE", "direction": "neutral", "confidence": 0, "grade": "D",
        "agreement": 0, "gates_ok": False, "checks": [], "plan": None, "zone": None, "watch": reason,
        "reason": reason, "steps": [smc._step("DATA", "wait", reason, "data")],
        "targetPrice": None, "stopLoss": None,
    }


# ── the engine ──────────────────────────────────────────────────────────────
def evaluate_style(
    style: str, ctx_bias: Dict, ctx_setup: Dict, entry: List[dict], entry_tf: str,
    min_agree: float = DEFAULT_MIN_AGREE, now_ms: Optional[float] = None, opts: Optional[Dict] = None,
) -> Dict:
    """Evaluate ONE style on pre-computed bias/setup analyses (`smc.analyze_full`)
    and the entry-TF candles. If `now_ms` is given and the newest entry candle is
    still forming, it is ignored — decisions are on closed candles."""
    cfg = STYLES[style]
    opts = opts or {}
    rr_use = float(opts.get(f"rr_{style}") or cfg["rr"])
    sl_buf = float(opts.get("sl_buffer") if opts.get("sl_buffer") is not None else 0.25)
    trig_bars = int(opts.get("trigger_bars") or cfg["trigger_bars"])
    btf, stf = cfg["bias_tf"], cfg["setup_tf"]
    emsg = TF_MS.get(entry_tf, 900_000)
    if now_ms is not None and entry and entry[-1]["time"] + emsg > now_ms:
        entry = entry[:-1]
    if len(entry) < 30 or ctx_bias.get("empty") or ctx_setup.get("empty"):
        return _empty_result(style, entry_tf, f"Not enough candles yet (need {btf}, {stf} and {entry_tf} history)")

    price = entry[-1]["close"]
    last_t = int(entry[-1]["time"] / 1000)
    atr_e = calc_atr(entry) or price * 0.001
    atr_s = ctx_setup.get("atr") or atr_e * 3
    bias = ctx_bias.get("bias") or smc.bias_state(ctx_bias)
    setup_b = ctx_setup.get("bias") or smc.bias_state(ctx_setup)
    d = bias["bias"]
    checks: List[Dict] = []
    BT, ST, ET = btf.upper(), stf.upper(), entry_tf.upper()

    # STEP 1 — direction ----------------------------------------------------
    lb = bias.get("last_break")
    detail = (f"{BT} {d}: last {lb['kind']} {lb['dir']} through {_fmt(lb['level'])}; swings {bias['swing_trend']}"
              if lb else f"{BT} has no confirmed BOS/CHoCH yet")
    checks.append(_check("bias", 1, f"{BT} structure bias", d != "neutral", 25, detail, gate=True))

    if d == "neutral":
        res = _empty_result(style, entry_tf, f"{BT} bias is neutral (structure and swings disagree) — stand aside")
        res["checks"] = checks + [_check("rest", 2, "Remaining checks", None, 0, "skipped — no direction")]
        res["bias"] = {"bias": d, "bias_tf": btf, "setup_tf": stf}
        res["steps"] = [smc._step("STRUCTURE", "block", f"{BT} bias neutral — no trade", "bias-neutral")]
        res["agreement"] = 0
        return res

    sign = 1 if d == "bullish" else -1
    side = "BUY" if d == "bullish" else "SELL"

    pd = ctx_bias.get("pd")
    if pd:
        good = pd["pct"] <= 50 if d == "bullish" else pd["pct"] >= 50
        checks.append(_check("pd", 1, f"{BT} premium / discount", good, 8,
                             f"price at {pd['pct']:.0f}% of the {BT} range ({pd['zone']}) — "
                             f"{'longs belong in discount' if d == 'bullish' else 'shorts belong in premium'}"))
    else:
        checks.append(_check("pd", 1, f"{BT} premium / discount", None, 8, "range not available"))

    # STEP 3 — setup-TF agreement + zone ------------------------------------
    agree_ok = setup_b["bias"] == d or setup_b["swing_trend"] == d
    checks.append(_check("align", 3, f"{ST} structure agrees with {BT}", agree_ok, 15,
                         f"{ST} bias {setup_b['bias']} (breaks {setup_b['break_trend']}, swings {setup_b['swing_trend']}) vs {BT} {d}",
                         gate=True))

    zones = _aligned_zones(ctx_setup, stf, d) + _aligned_zones(ctx_bias, btf, d)
    touch_n = max(2, round(cfg["touch_bars"] * float(opts.get("touch_mult", 1.0))))
    recent = entry[-touch_n:]
    base_idx = len(entry) - len(recent)
    best: Optional[Dict] = None
    for z in zones:
        # A zone must already exist before the bar that touches it.
        touch_i = None
        for k, c in enumerate(recent):
            if int(c["time"] / 1000) <= z["formed_at"]:
                continue
            if _touch(c, z, d):
                touch_i = base_idx + k
                break
        if touch_i is None:
            continue
        far_side_broken = price <= z["bottom"] if d == "bullish" else price >= z["top"]
        if far_side_broken:
            continue
        away = (price - z["top"]) if d == "bullish" else (z["bottom"] - price)
        if away > 1.5 * atr_s:
            continue  # price already ran away from the zone — chasing
        rank = (1 if z["tf"] == btf else 0, 1 if z["src"] == "OB" else 0, z["strength"])
        if best is None or rank > best["_rank"]:
            best = {**z, "touch_i": touch_i, "_rank": rank}
    if best:
        inside = best["bottom"] <= price <= best["top"]
        checks.append(_check("zone", 3, f"Price at an aligned {ST}/{BT} zone", True, 20,
                             f"{best['tf'].upper()} {best['src']} {_fmt(best['bottom'])}–{_fmt(best['top'])} touched "
                             f"{len(entry) - 1 - best['touch_i']} bar(s) ago" + (", price inside it" if inside else ""), gate=True))
        overlap = sum(
            1 for z in zones
            if z is not best and z["bottom"] <= best["top"] and z["top"] >= best["bottom"]
        )
        checks.append(_check("confluence", 3, "Zone confluence (OB + FVG / HTF overlap)", overlap >= 1 or best["tf"] == btf, 8,
                             f"{overlap} more aligned zone(s) stacked on it" + (f"; it is a {BT} zone" if best["tf"] == btf else "")))
        checks.append(_check("fresh", 3, "Zone is fresh", bool(best["fresh"]), 5,
                             "first / early return to the zone" if best["fresh"] else "zone already partly used"))
    else:
        checks.append(_check("zone", 3, f"Price at an aligned {ST}/{BT} zone", False, 20,
                             f"no {d} OB / FVG / supply-demand zone touched in the last {touch_n} {entry_tf} bars", gate=True))
        checks.append(_check("confluence", 3, "Zone confluence (OB + FVG / HTF overlap)", None, 8, "no active zone"))
        checks.append(_check("fresh", 3, "Zone is fresh", None, 5, "no active zone"))

    # liquidity sweep (setup-TF or entry-TF), with the right direction
    sweep_kind = "bullish" if d == "bullish" else "bearish"
    ent_swings = smc.find_swings(entry[-140:], 2)
    ent_sweeps = smc.find_liquidity_sweeps(entry[-140:], ent_swings, max_sweeps=10)
    recent_sweep = next((s for s in reversed(ent_sweeps) if s["kind"] == sweep_kind and s["time"] >= last_t - 20 * emsg / 1000), None)
    src = f"{ET} sweep of {_fmt(recent_sweep['level'])}" if recent_sweep else ""
    if recent_sweep is None:
        cutoff = last_t - 8 * TF_MS.get(stf, 3_600_000) / 1000
        s2 = next((s for s in reversed(ctx_setup.get("sweeps", [])) if s["kind"] == sweep_kind and s["time"] >= cutoff), None)
        if s2:
            recent_sweep = s2
            src = f"{ST} sweep of {_fmt(s2['level'])}"
    if recent_sweep is None:
        want = "EQL" if d == "bullish" else "EQH"
        eqs = [e for e in ctx_setup.get("eq", []) if e["kind"] == want and e["swept"]]
        if eqs:
            recent_sweep, src = {"wick": None, "level": eqs[0]["price"]}, f"{ST} {want} already swept"
    checks.append(_check("sweep", 3, "Liquidity swept before the entry", recent_sweep is not None, 10,
                         src or f"no {'sell' if d == 'bullish' else 'buy'}-side liquidity sweep nearby",
                         gate=bool(opts.get("gate_sweep"))))

    # STEP 4 — trigger on the entry TF --------------------------------------
    avg_body = (sum(abs(c["close"] - c["open"]) for c in entry[-21:-1]) / 20) or atr_e * 0.3
    trig = None
    trig_i = None
    if best:
        for k in range(len(entry) - 1, max(len(entry) - 1 - trig_bars, best["touch_i"] - 1), -1):
            if k < 1 or k < best["touch_i"]:
                break
            name = _is_trigger(entry[k - 1], entry[k], d, avg_body)
            if name:
                trig, trig_i = name, k
                break
    checks.append(_check("trigger", 4, f"{ET} confirmation candle", trig is not None, 12,
                         f"{trig} {len(entry) - 1 - trig_i} bar(s) ago" if trig else
                         f"no engulfing / rejection / displacement candle after the zone touch ({ET})", gate=True))

    mini = smc.find_structure_breaks(entry[-90:], 2)
    mini_ev = next((e for e in reversed(mini["events"]) if e["dir"] == d), None)
    touch_t = int(entry[best["touch_i"]]["time"] / 1000) if best else last_t
    mini_ok = bool(mini_ev and mini_ev["time"] >= touch_t and mini_ev["time"] >= last_t - 8 * emsg / 1000)
    checks.append(_check("mini", 4, f"{ET} BOS / CHoCH in trade direction", mini_ok, 10,
                         f"{mini_ev['kind']} {mini_ev['dir']} through {_fmt(mini_ev['level'])}" if mini_ok else
                         f"no {ET} break of structure {d} after the zone touch yet"))

    vols = [c["volume"] for c in entry[-21:-1]]
    vol_avg = sum(vols) / len(vols) if vols else 0
    if vol_avg > 0:
        k = trig_i if trig_i is not None else len(entry) - 1
        spike = entry[k]["volume"] >= 1.3 * vol_avg
        checks.append(_check("volume", 4, "Volume spike on the trigger bar", spike, 5,
                             f"{entry[k]['volume'] / vol_avg:.1f}x the 20-bar average"))
    else:
        checks.append(_check("volume", 4, "Volume spike on the trigger bar", None, 5, "no volume data"))

    kz = killzone(last_t)
    checks.append(_check("session", 4, "Kill-zone (London / New York)", kz is not None, 4 if style == "intraday" else 6,
                         f"{kz} session active" if kz else "outside London / New York kill zones",
                         gate=bool(opts.get("gate_killzone"))))

    # trade plan -------------------------------------------------------------
    plan = None
    plan_ok = False
    plan_detail = "needs a zone and a trigger first"
    if best:
        if d == "bullish":
            anchor = min([best["bottom"], min(c["low"] for c in entry[best["touch_i"]:])] +
                         ([recent_sweep["wick"]] if recent_sweep and recent_sweep.get("wick") else []))
            sl = anchor - sl_buf * atr_s
            risk = price - sl
        else:
            anchor = max([best["top"], max(c["high"] for c in entry[best["touch_i"]:])] +
                         ([recent_sweep["wick"]] if recent_sweep and recent_sweep.get("wick") else []))
            sl = anchor + sl_buf * atr_s
            risk = sl - price
        if risk > 0:
            tp = price + sign * risk * rr_use
            opp = _opposing_levels([(stf, ctx_setup), (btf, ctx_bias)], d, price)
            nearest = opp[0] if opp else None
            room_rr = abs(nearest[0] - price) / risk if nearest else None
            risk_pct = risk / price
            sane = MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT and 0.15 * atr_s <= risk <= 3.0 * atr_s
            room_ok = room_rr is None or room_rr >= cfg["min_rr"]
            plan_ok = sane and room_ok
            plan = {
                "side": side, "entry": price, "sl": sl, "tp": tp, "rr": rr_use, "risk_pct": risk_pct * 100,
                "ideal_entry": (best["top"] + best["bottom"]) / 2,
                "target_liquidity": {"price": nearest[0], "label": nearest[1], "rr": room_rr} if nearest else None,
            }
            parts = [f"stop {_fmt(sl)} ({risk_pct * 100:.2f}% / {risk / atr_s:.1f}x {ST} ATR)"]
            if not sane:
                parts.append("stop is too tight/wide for this zone")
            parts.append(f"next liquidity {nearest[1]} {_fmt(nearest[0])} = {room_rr:.1f}R" if nearest else "no opposing level in the way")
            if not room_ok:
                parts.append(f"room {room_rr:.1f}R < required {cfg['min_rr']}R")
            plan_detail = "; ".join(parts)
    checks.append(_check("plan", 4, "Sane stop and room to target", plan_ok if best else False, 6, plan_detail, gate=True))

    # score ------------------------------------------------------------------
    applicable = [c for c in checks if c["ok"] is not None and c["weight"] > 0]
    total = sum(c["weight"] for c in applicable) or 1
    agree = round(sum(c["weight"] for c in applicable if c["ok"]) / total * 100, 1)
    gates_ok = all(c["ok"] for c in checks if c["gate"])
    fired = gates_ok and agree >= min_agree

    # state + "what to expect" text -----------------------------------------
    expect = None
    if fired:
        state = "SIGNAL"
        watch = f"{side} signal live — {trig} at the {best['tf'].upper()} {best['src']}"
    elif best and agree_ok:
        state = "SETUP"
        if trig is None:
            watch = f"Price is at the {best['tf'].upper()} {best['src']} — waiting for a {ET} confirmation candle ({d})"
        else:
            watch = f"Setup is there but not complete ({'agreement ' + str(agree) + '% < ' + str(int(min_agree)) + '%' if gates_ok else 'a gate is failing — see checklist'})"
    elif agree_ok:
        state = "WATCH"
        cands = []
        for z in zones:
            if d == "bullish" and z["top"] < price:
                cands.append((price - z["top"], z))
            elif d == "bearish" and z["bottom"] > price:
                cands.append((z["bottom"] - price, z))
        if cands:
            dist, z = min(cands, key=lambda x: x[0])
            expect = {"tf": z["tf"], "src": z["src"], "top": z["top"], "bottom": z["bottom"], "distance_pct": dist / price * 100}
            watch = (f"Expect a {side} if price pulls {'back down' if d == 'bullish' else 'back up'} into the {z['tf'].upper()} {z['src']} "
                     f"{_fmt(z['bottom'])}–{_fmt(z['top'])} ({dist / price * 100:.2f}% away), then a {ET} trigger")
        else:
            watch = f"{BT}/{ST} are {d} but there is no fresh {'demand' if d == 'bullish' else 'supply'} zone on the path — wait for new structure"
    else:
        state = "STAND_ASIDE"
        watch = f"{ST} structure disagrees with {BT} {d} — stand aside until they align"

    # trace for the bot log (same shape as smc.evaluate_entry's steps) -------
    def st(cid):
        return next((c for c in checks if c["id"] == cid), None)

    def stat(c):
        return "info" if c is None or c["ok"] is None else ("pass" if c["ok"] else "wait")

    steps = [
        smc._step("DATA", "info", f"{style}: {btf}/{stf}/{entry_tf} read, last close {_fmt(price)}", "data"),
        smc._step("STRUCTURE", "pass" if st("bias")["ok"] else "block", st("bias")["detail"] + f" | {ST}: {st('align')['detail']}",
                  f"{d}:{int(agree_ok)}"),
        smc._step("ZONES", stat(st("zone")), st("zone")["detail"], f"zone:{int(bool(best))}:{best['src'] if best else ''}"),
        smc._step("SWEEP", stat(st("sweep")), st("sweep")["detail"], f"sweep:{int(recent_sweep is not None)}"),
        smc._step("RETEST", stat(st("trigger")), st("trigger")["detail"] + " | " + st("mini")["detail"], f"trig:{int(trig is not None)}"),
    ]
    if fired:
        steps.append(smc._step("DECISION", "pass",
                               f"{side} ({style}) — agreement {agree}% grade {_grade(agree)}; entry {_fmt(price)} stop {_fmt(plan['sl'])} "
                               f"target {_fmt(plan['tp'])} ({plan['rr']}R)", f"{side.lower()}-{style}"))
    else:
        steps.append(smc._step("DECISION", "wait", f"HOLD ({style}) — {watch}", f"hold-{state}"))

    return {
        "style": style, "label": cfg["label"], "bias_tf": btf, "setup_tf": stf, "entry_tf": entry_tf,
        "type": side if fired else "HOLD", "state": state, "direction": d, "side": side,
        "confidence": agree if fired else 0, "agreement": agree, "grade": _grade(agree), "gates_ok": gates_ok,
        "checks": checks, "plan": plan if (fired or best) else None,
        "zone": ({k: v for k, v in best.items() if not k.startswith("_")} if best else None),
        "trigger": trig, "watch": watch, "expect": expect,
        "reason": f"{cfg['label']} {side}: {trig} at {best['tf'].upper()} {best['src']} (agreement {agree}%)" if fired else watch,
        "steps": steps,
        "targetPrice": plan["tp"] if fired else None, "stopLoss": plan["sl"] if fired else None,
        "bias": {"bias": d, "bias_tf": btf, "setup_tf": stf, "last_break": lb},
        "time": last_t,
    }


_STATE_RANK = {"SIGNAL": 3, "SETUP": 2, "WATCH": 1, "STAND_ASIDE": 0}


def evaluate_all(
    frames: Dict[str, List[dict]], mode: str = "combined", entry_tf: str = "15m",
    min_agree: float = DEFAULT_MIN_AGREE, now_ms: Optional[float] = None, ctx_cache: Optional[Dict] = None,
    opts: Optional[Dict] = None,
) -> Dict:
    """Run the requested style(s) over `frames` ({tf: candles}); returns
    {"decision": best, "styles": {...}, "analysis": {tf: analyze_full}}."""
    ctxs: Dict[str, Dict] = ctx_cache if ctx_cache is not None else {}

    def ctx(tf: str) -> Dict:
        if tf not in ctxs:
            cs = frames.get(tf) or []
            if now_ms is not None and cs and cs[-1]["time"] + TF_MS.get(tf, 0) > now_ms:
                cs_closed = cs[:-1]          # analysis on closed candles only
            else:
                cs_closed = cs
            ctxs[tf] = smc.analyze_full(cs_closed)
        return ctxs[tf]

    results: Dict[str, Dict] = {}
    for name in _styles_for(mode):
        et = _entry_for(name, entry_tf)
        st = STYLES[name]
        results[name] = evaluate_style(name, ctx(st["bias_tf"]), ctx(st["setup_tf"]), frames.get(et) or [], et,
                                       min_agree, now_ms, opts)
    best = max(results.values(), key=lambda r: (_STATE_RANK[r["state"]], r["agreement"]))
    return {"decision": best, "styles": results, "analysis": ctxs}


# ── payloads for the chart ──────────────────────────────────────────────────
def _camel_fvg(g: dict) -> dict:
    out = dict(g)
    out["fillPct"] = out.pop("fill_pct", 0)
    return out


def panel_payload(tf: str, candles: List[dict], analysis: Optional[Dict] = None, limit: int = 300) -> Dict:
    """Candles (seconds) + every concept detected on this timeframe — one chart panel."""
    candles = candles[-limit:]
    an = analysis if analysis is not None else smc.analyze_full(candles)
    ms = TF_MS.get(tf, 0)
    return {
        "tf": tf,
        "candles": [{"time": int(c["time"] / 1000), "open": c["open"], "high": c["high"], "low": c["low"],
                     "close": c["close"], "volume": c["volume"]} for c in candles],
        "orderBlocks": an.get("order_blocks", []),
        "fvgs": [_camel_fvg(g) for g in an.get("fvgs", [])],
        "sdZones": an.get("sd_zones", []),
        "structure": an.get("structure", {"swings": [], "trend": "ranging"}),
        "breaks": an.get("breaks", {"events": [], "trend": None}),
        "sweeps": an.get("sweeps", []),
        "poi": an.get("poi", []),
        "retests": an.get("retests", []),
        "eq": an.get("eq", []),
        "pd": an.get("pd"),
        "trendlines": an.get("trendlines", []),
        "bias": (an.get("bias") or {}).get("bias", "neutral"),
        "atr": an.get("atr", 0),
        "barMs": ms,
    }


# ── backtest ────────────────────────────────────────────────────────────────
def backtest(
    frames: Dict[str, List[dict]], style: str = "intraday", entry_tf: str = "15m",
    min_agree: float = DEFAULT_MIN_AGREE, fee_pct: float = 0.1, long_only: bool = False,
    max_hold_bars: int = 300, warmup: int = 60, keep_all: bool = False,
    opts: Optional[Dict] = None, slippage_pct: float = 0.0, min_grade: str = "D", breakeven_at_r: float = 0.0,
    killzone_only: bool = False, vote_fn=None,
) -> Dict:
    """Replay the engine bar by bar on stored/real history with NO look-ahead:
    at each closed entry bar the bias/setup analyses only contain HTF candles
    that had already closed. Entry = that bar's close; SL checked before TP on
    a bar that touches both (conservative). One trade at a time. Results in R
    after fees (`fee_pct` per side)."""
    cfg = STYLES[style]
    et = _entry_for(style, entry_tf)
    ent = frames.get(et) or []
    emsg = TF_MS[et]
    series = {tf: (frames.get(tf) or []) for tf in (cfg["bias_tf"], cfg["setup_tf"])}
    opens = {tf: [c["time"] for c in cs] for tf, cs in series.items()}
    cache: Dict[Tuple[str, int], Dict] = {}

    def ctx(tf: str, t_close: float) -> Dict:
        # candles of `tf` that were fully closed by t_close
        n = bisect.bisect_right(opens[tf], t_close - TF_MS[tf])
        key = (tf, n)
        if key not in cache:
            cache[key] = smc.analyze_full(series[tf][max(0, n - smc.SMC_WINDOW):n])
            if len(cache) > 400:
                cache.pop(next(iter(cache)))
        return cache[key]

    trades: List[Dict] = []
    i = max(warmup, 30)
    while i < len(ent):
        t_close = ent[i]["time"] + emsg
        res = evaluate_style(style, ctx(cfg["bias_tf"], t_close), ctx(cfg["setup_tf"], t_close),
                             ent[max(0, i - 150):i + 1], et, min_agree, None, opts)
        ok_filter = (GRADE_RANK.get(res["grade"], 0) >= GRADE_RANK.get(min_grade, 0)
                     and (not killzone_only or killzone(ent[i]["time"] / 1000) is not None))
        if ok_filter and vote_fn is not None and res["type"] in ("BUY", "SELL"):
            ok_filter = vote_fn(ent[max(0, i - 250):i + 1], res["type"])
        if ok_filter and res["type"] in ("BUY", "SELL") and res["plan"] and not (long_only and res["type"] == "SELL"):
            p = res["plan"]
            entry_px, sl, tp = p["entry"], p["sl"], p["tp"]
            risk = abs(entry_px - sl)
            long = res["type"] == "BUY"
            exit_px, outcome, j = None, "TIMEOUT", i
            cur_sl, be_armed = sl, False
            for j in range(i + 1, min(len(ent), i + 1 + max_hold_bars)):
                c = ent[j]
                hit_sl = c["low"] <= cur_sl if long else c["high"] >= cur_sl
                hit_tp = c["high"] >= tp if long else c["low"] <= tp
                if hit_sl:                       # SL first when both are inside one bar
                    exit_px, outcome = cur_sl, ("BE" if be_armed else "LOSS")
                    break
                if hit_tp:
                    exit_px, outcome = tp, "WIN"
                    break
                if breakeven_at_r > 0 and not be_armed:      # stop moves to entry from the NEXT bar
                    reached = (c["high"] - entry_px) >= breakeven_at_r * risk if long else (entry_px - c["low"]) >= breakeven_at_r * risk
                    if reached:
                        be_armed, cur_sl = True, entry_px
            else:
                j = min(len(ent), i + 1 + max_hold_bars) - 1
                exit_px = ent[j]["close"]
            slip = slippage_pct / 100 * entry_px           # adverse move on entry and on exit
            e_fill = entry_px + slip if long else entry_px - slip
            x_fill = exit_px - slip if long else exit_px + slip
            gross_r = ((x_fill - e_fill) if long else (e_fill - x_fill)) / risk
            fee_r = (2 * fee_pct / 100 * entry_px) / risk
            net_r = gross_r - fee_r
            if outcome in ("TIMEOUT", "BE"):
                outcome = "WIN" if net_r > 0 else "LOSS"
            trades.append({
                "side": res["type"], "entry_time": int(ent[i]["time"] / 1000), "exit_time": int(ent[j]["time"] / 1000),
                "entry": entry_px, "sl": sl, "tp": tp, "exit": exit_px, "result": outcome,
                "r": round(net_r, 3), "agreement": res["agreement"], "grade": res["grade"],
                "zone": f"{res['zone']['tf']} {res['zone']['src']}" if res.get("zone") else "",
                "bars_held": j - i, "style": style,
            })
            i = j + 1
            continue
        i += 1

    stats = trade_stats(trades)
    wins = [t for t in trades if t["result"] == "WIN"]
    gross_win = sum(t["r"] for t in trades if t["r"] > 0)
    gross_loss = -sum(t["r"] for t in trades if t["r"] < 0)
    equity, peak, dd, curve = 0.0, 0.0, 0.0, []
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        curve.append({"time": t["exit_time"], "r": round(equity, 3)})
    return {
        "style": style, "entry_tf": et, "bars": len(ent), "trades": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "net_r": round(equity, 2), "avg_r": round(equity / len(trades), 3) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not trades else 99.0),
        "max_drawdown_r": round(dd, 2), "equity": curve, "trade_log": trades[-600:], "stats": stats,
        "from": int(ent[0]["time"] / 1000) if ent else None, "to": int(ent[-1]["time"] / 1000) if ent else None,
        "note": f"Fees {fee_pct}%/side and slippage {slippage_pct}% included. R = multiples of the risked stop distance.",
    }


GRADE_RANK = {"D": 0, "C": 1, "B": 2, "A": 3, "A+": 4}


def trade_stats(trades: List[Dict]) -> Dict:
    """Extra statistics for the advanced backtest view (pure function, unit-tested)."""
    n = len(trades)
    if not n:
        return {"expectancy_r": 0, "avg_win_r": 0, "avg_loss_r": 0, "best_r": 0, "worst_r": 0, "max_win_streak": 0,
                "max_loss_streak": 0, "avg_bars_held": 0, "sharpe_r": None, "by_side": {}, "by_grade": {}, "by_hour": {},
                "by_weekday": {}, "by_month": {}, "dist": [], "drawdown": []}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    mean = sum(rs) / n
    var = sum((r - mean) ** 2 for r in rs) / n
    streak_w = streak_l = cur_w = cur_l = 0
    for r in rs:
        cur_w, cur_l = (cur_w + 1, 0) if r > 0 else (0, cur_l + 1)
        streak_w, streak_l = max(streak_w, cur_w), max(streak_l, cur_l)

    def group(keyf) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        for t in trades:
            g = out.setdefault(str(keyf(t)), {"trades": 0, "wins": 0, "net_r": 0.0})
            g["trades"] += 1
            g["wins"] += 1 if t["r"] > 0 else 0
            g["net_r"] = round(g["net_r"] + t["r"], 3)
        for g in out.values():
            g["win_rate"] = round(g["wins"] / g["trades"] * 100, 1)
        return out

    def dt(t):
        return datetime.fromtimestamp(t["entry_time"], tz=timezone.utc)

    lo, hi = min(rs), max(rs)
    bins = 12
    width = (hi - lo) / bins or 1.0
    dist = [0] * bins
    for r in rs:
        dist[min(bins - 1, int((r - lo) / width))] += 1
    eq = peak = 0.0
    dd_curve = []
    for t in trades:
        eq += t["r"]
        peak = max(peak, eq)
        dd_curve.append({"time": t["exit_time"], "dd": round(eq - peak, 3)})
    return {
        "expectancy_r": round(mean, 3),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0,
        "best_r": round(hi, 3), "worst_r": round(lo, 3), "max_win_streak": streak_w, "max_loss_streak": streak_l,
        "avg_bars_held": round(sum(t.get("bars_held", 0) for t in trades) / n, 1),
        "sharpe_r": round(mean / math.sqrt(var), 2) if var > 0 else None,
        "by_side": group(lambda t: t["side"]), "by_grade": group(lambda t: t["grade"]),
        "by_hour": group(lambda t: f"{dt(t).hour:02d}"), "by_weekday": group(lambda t: dt(t).strftime("%a")),
        "by_month": group(lambda t: dt(t).strftime("%Y-%m")),
        "dist": [{"from": round(lo + k * width, 2), "to": round(lo + (k + 1) * width, 2), "n": c} for k, c in enumerate(dist)],
        "drawdown": dd_curve,
    }
