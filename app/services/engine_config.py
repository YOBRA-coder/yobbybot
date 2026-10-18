"""
Engine settings stored in the database (one JSON row), so the background scanner — which fires the
signals you get on Telegram — uses the same rules you picked in the app.

Interactive screens send sensitivity / consensus / weighting with each request; the "advanced" knobs
(targets, stop buffer, optional gates) and the scanner defaults live here.
"""
import json
from typing import Dict

from ..database import get_db

DEFAULTS: Dict = {
    "mode": "combined", "entry_tf": "15m", "min_agree": 70, "sens": "balanced",
    "consensus": False, "weighting": "equal",
    "rr_intraday": None, "rr_scalp": None, "sl_buffer": None, "gate_sweep": None, "gate_killzone": None,
    "risk_guard": None, "max_chase_atr": None, "min_room_rr": None, "max_risk_pct": None,
    "rsi_guard": None, "vol_guard": None, "avoid_weekend": None,
    "profiles": {},          # per-pair tuned settings from the backtest optimizer: {"BTCUSDT": {...}}
}
_ready = False


def _ensure(conn) -> None:
    global _ready
    if not _ready:
        conn.execute("CREATE TABLE IF NOT EXISTS app_config (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        conn.commit()
        _ready = True


def get() -> Dict:
    conn = get_db()
    try:
        _ensure(conn)
        row = conn.execute("SELECT v FROM app_config WHERE k='engine'").fetchone()
    finally:
        conn.close()
    out = dict(DEFAULTS)
    if row:
        try:
            out.update({k: v for k, v in json.loads(row["v"]).items() if k in DEFAULTS})
        except ValueError:
            pass
    return out


def _num(v, lo, hi):
    if v in (None, ""):
        return None
    f = float(v)
    if not lo <= f <= hi:
        raise ValueError(f"must be between {lo} and {hi}")
    return f


def clean(patch: Dict) -> Dict:
    """Validate a partial update; raises ValueError with a readable message."""
    from . import mtf
    out: Dict = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            continue
        if k == "mode":
            if v not in mtf.MODES:
                raise ValueError(f"mode must be one of {', '.join(mtf.MODES)}")
        elif k == "entry_tf":
            if v not in mtf.ENTRY_TFS:
                raise ValueError(f"entry_tf must be one of {', '.join(mtf.ENTRY_TFS)}")
        elif k == "sens":
            if v not in mtf.SENSITIVITY:
                raise ValueError(f"sens must be one of {', '.join(mtf.SENSITIVITY)}")
        elif k == "weighting":
            if v not in ("equal", "best"):
                raise ValueError("weighting must be equal or best")
        elif k == "min_agree":
            v = _num(v, 40, 100)
        elif k in ("rr_intraday", "rr_scalp"):
            v = _num(v, 1.0, 6.0)
        elif k == "sl_buffer":
            v = _num(v, 0.0, 2.0)
        elif k == "risk_guard":
            v = None if v in (None, "") else int(_num(v, 0, 2))
        elif k == "max_chase_atr":
            v = _num(v, 0.3, 4.0)
        elif k == "min_room_rr":
            v = _num(v, 0.8, 4.0)
        elif k == "max_risk_pct":
            v = _num(v, 0.3, 10.0)
        elif k == "profiles":
            v = clean_profiles(v)
        elif k in ("consensus", "gate_sweep", "gate_killzone", "rsi_guard", "vol_guard", "avoid_weekend"):
            v = None if v is None else bool(v)
        out[k] = v
    return out


PROFILE_KEYS = ("sens", "min_agree", "entry_tf", "mode", "rr_intraday", "rr_scalp", "sl_buffer", "gate_sweep", "gate_killzone",
                "risk_guard", "max_chase_atr", "min_room_rr", "max_risk_pct", "rsi_guard", "vol_guard", "avoid_weekend")


def clean_profiles(v) -> Dict:
    """{SYMBOL: {settings}} — each profile is validated with the same rules as the global settings."""
    if not isinstance(v, dict):
        raise ValueError("profiles must be an object")
    out: Dict = {}
    for sym, prof in v.items():
        sym = str(sym).upper()
        if not sym.isalnum() or len(sym) > 20 or prof is None:
            continue
        if not isinstance(prof, dict):
            raise ValueError("a profile must be an object")
        sub = clean({k: x for k, x in prof.items() if k in PROFILE_KEYS})
        sub = {k: x for k, x in sub.items() if x is not None}
        if sub:
            sub["tuned_at"] = int(prof.get("tuned_at") or 0) or None
            if prof.get("score") is not None:
                sub["score"] = prof["score"]
            out[sym] = {k: x for k, x in sub.items() if x is not None}
    return out


def profile_for(symbol: str, cfg: Dict = None) -> Dict:
    cfg = cfg or get()
    return dict((cfg.get("profiles") or {}).get(symbol.upper()) or {})


def set_profile(symbol: str, prof: Dict) -> Dict:
    cur = get()
    profiles = dict(cur.get("profiles") or {})
    if prof:
        profiles[symbol.upper()] = prof
    else:
        profiles.pop(symbol.upper(), None)
    return set_({"profiles": profiles})


def set_(patch: Dict) -> Dict:
    cur = get()
    cur.update(clean(patch))
    conn = get_db()
    try:
        _ensure(conn)
        conn.execute("INSERT OR REPLACE INTO app_config (k, v) VALUES ('engine', ?)", (json.dumps(cur),))
        conn.commit()
    finally:
        conn.close()
    return cur


def custom_opts(cfg: Dict) -> Dict:
    keys = ("rr_intraday", "rr_scalp", "sl_buffer", "gate_sweep", "gate_killzone", "risk_guard", "max_chase_atr",
            "min_room_rr", "max_risk_pct", "rsi_guard", "vol_guard", "avoid_weekend")
    return {k: cfg[k] for k in keys if cfg.get(k) is not None}
