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
        elif k in ("consensus", "gate_sweep", "gate_killzone"):
            v = None if v is None else bool(v)
        out[k] = v
    return out


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
    return {k: cfg[k] for k in ("rr_intraday", "rr_scalp", "sl_buffer", "gate_sweep", "gate_killzone") if cfg.get(k) is not None}
