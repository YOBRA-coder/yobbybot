"""
Find the engine settings that worked best on a pair's own history — without fooling yourself.

The history is split in two: settings are searched on the first ~65% ("train") and then re-tested on
the untouched last ~35% ("validate"). A setting only counts as a recommendation if it is positive on
BOTH halves with a reasonable number of trades; otherwise the honest answer is "no robust edge found —
keep the defaults". Search is staged (not a huge grid) so it finishes in tens of seconds:

  1. sensitivity preset x entry-risk guard          (9 runs)
  2. target (R) x stop buffer for the best two      (24 runs)
  3. minimum agreement around the best              (4 runs)

Analyses are computed once and shared by every run (they do not depend on the settings).
"""
import math
from typing import Dict, List, Optional

from . import mtf

MIN_TRAIN_TRADES = 8
MIN_VAL_TRADES = 4
ROBUST_VAL_TRADES = 8


def _metrics(r: Dict) -> Dict:
    return {"trades": r["trades"], "win_rate": r["win_rate"], "avg_r": r["avg_r"], "net_r": r["net_r"],
            "profit_factor": r["profit_factor"], "max_drawdown_r": r["max_drawdown_r"]}


def _score(m: Dict) -> float:
    """Expectancy that rewards a bigger sample (avg R x sqrt(n)) and charges for drawdown."""
    n = m["trades"]
    if n < MIN_TRAIN_TRADES:
        return -9.0 + n / 100
    return m["avg_r"] * math.sqrt(n) - 0.1 * m["max_drawdown_r"]


def optimize(frames: Dict[str, List[dict]], style: str, entry_tf: str, fee_pct: float = 0.1, slippage_pct: float = 0.0,
             base_custom: Optional[Dict] = None, split: float = 0.65) -> Dict:
    cfg = mtf.STYLES[style]
    et = mtf._entry_for(style, entry_tf)
    ent = frames.get(et) or []
    warm = 60
    n = len(ent)
    if n < 400:
        raise ValueError("Not enough candles to optimize — sync more history first")
    cut = int(warm + (n - warm) * split)
    cache: Dict = {}
    base_custom = {k: v for k, v in (base_custom or {}).items() if k in mtf.OPT_KEYS}
    runs = 0
    seen: Dict[str, Dict] = {}

    def run(params: Dict, lo: int, hi: Optional[int]) -> Dict:
        nonlocal runs
        custom = dict(base_custom)
        for k in ("risk_guard", f"rr_{style}", "sl_buffer"):
            if params.get(k) is not None:
                custom[k] = params[k]
        opts = mtf.opts_for(params["sens"], custom)
        runs += 1
        return mtf.backtest(frames, style, et, params["min_agree"], fee_pct, False, 300, warm, False, opts, slippage_pct,
                            "D", 0.0, False, None, ctx_cache=cache, i_from=lo, i_to=hi)

    def key(p: Dict) -> str:
        return "|".join(f"{k}={p.get(k)}" for k in sorted(p))

    def train(p: Dict) -> Dict:
        k = key(p)
        if k not in seen:
            seen[k] = {"params": dict(p), "train": _metrics(run(p, warm, cut))}
            seen[k]["score"] = round(_score(seen[k]["train"]), 3)
        return seen[k]

    # stage 1 — how picky, and how strict about risky entries
    for sens in mtf.SENSITIVITY:
        for guard in (0, 1, 2):
            train({"sens": sens, "min_agree": mtf.SENSITIVITY[sens]["min_agree"], "risk_guard": guard})
    top = sorted(seen.values(), key=lambda c: -c["score"])[:2]
    # stage 2 — target and stop buffer
    rr_opts = (1.2, 1.5, 2.0, 2.5) if style == "scalp" else (1.5, 2.0, 2.5, 3.0)
    for c in top:
        for rr in rr_opts:
            for slb in (0.15, 0.25, 0.5):
                train({**c["params"], f"rr_{style}": rr, "sl_buffer": slb})
    best = max(seen.values(), key=lambda c: c["score"])
    # stage 3 — agreement threshold
    for delta in (-10, -5, 5, 10):
        ma = best["params"]["min_agree"] + delta
        if 50 <= ma <= 95:
            train({**best["params"], "min_agree": ma})

    ranked = sorted(seen.values(), key=lambda c: -c["score"])
    shortlist: List[Dict] = []
    sigs = set()
    for c in ranked:                         # settings that trade identically are one candidate, not several
        sig = (c["train"]["trades"], c["train"]["net_r"], c["train"]["win_rate"])
        if c["train"]["trades"] >= MIN_TRAIN_TRADES and sig not in sigs:
            sigs.add(sig)
            shortlist.append(c)
        if len(shortlist) == 4:
            break
    baseline_p = {"sens": "balanced", "min_agree": mtf.SENSITIVITY["balanced"]["min_agree"], "risk_guard": 1}
    base = train(baseline_p)
    out_c: List[Dict] = []
    for c in shortlist:
        c = dict(c)
        c["val"] = _metrics(run(c["params"], cut, None))
        out_c.append(c)
    baseline = {"params": baseline_p, "train": base["train"], "val": _metrics(run(baseline_p, cut, None))}

    def ok(c: Dict) -> bool:
        return c["train"]["avg_r"] > 0 and c["val"]["trades"] >= MIN_VAL_TRADES and c["val"]["avg_r"] > 0

    good = sorted([c for c in out_c if ok(c)], key=lambda c: -(c["val"]["avg_r"] * math.sqrt(c["val"]["trades"])))
    chosen = good[0] if good else None
    if chosen and chosen["val"]["trades"] >= ROBUST_VAL_TRADES:
        verdict = "robust"
        note = "Positive on both the search period and the untouched recent period."
    elif chosen:
        verdict = "tentative"
        note = "Positive on both periods, but the recent period has few trades — treat as a hint, re-run after more history."
    else:
        verdict = "none"
        note = ("No setting stayed profitable on the untouched recent period. That is a real answer: on this pair and "
                "timeframe the strategy has no proven edge right now — keep the defaults, or trade smaller / skip it.")
    profile = None
    if chosen:
        p = chosen["params"]
        profile = {k: v for k, v in p.items() if v is not None}
        profile["entry_tf"] = et
        profile["score"] = round(chosen["val"]["avg_r"], 3)
    return {
        "style": style, "entry_tf": et, "bars": n, "runs": runs, "verdict": verdict, "note": note,
        "split_time": int(ent[cut]["time"] / 1000), "from": int(ent[0]["time"] / 1000), "to": int(ent[-1]["time"] / 1000),
        "best": chosen, "profile": profile, "baseline": baseline, "candidates": out_c,
        "fee_pct": fee_pct, "slippage_pct": slippage_pct,
    }
