"""
Bot activity log — the "what is each bot doing, and why" trail.

Every bot cycle runs a series of processes (read candles -> structure ->
sweep -> zones -> retest -> decision -> risk gates -> order). Until now only
the LAST one-line reason was kept (bots.reason_log), so once a bot had
traded or been skipped there was no way to see what it had actually looked
at. This module writes one row per step into `bot_logs`.

NOISE CONTROL: a bot cycles every 15 seconds, and most cycles reach the
same conclusion as the one before ("still ranging, still no retest"). Rows
are therefore written only when a step's outcome CHANGES (or, for steady
state, once every HEARTBEAT_SECONDS as a "still watching" pulse), so the
log reads as a story of what happened rather than 4 identical blocks per
minute. Events that matter on their own (orders, exits, errors) are always
written.
"""
import json
import time
from typing import Any, Dict, Iterable, List, Optional

HEARTBEAT_SECONDS = 300       # re-log an unchanged step at most this often
KEEP_PER_BOT = 600            # rows retained per bot; older ones are pruned

# (bot_id, step) -> (signature, last_written_ts)
_last: Dict[tuple, tuple] = {}


def add_log(
    conn, bot: dict, step: str, message: str, level: str = "info",
    data: Optional[dict] = None, sig: Optional[str] = None, always: bool = False,
) -> Optional[dict]:
    """Insert one log row. Returns the row as a dict (for websocket push), or
    None if it was suppressed as a repeat of the previous cycle.

    `sig` is the step's price-free fingerprint (see smc._step); with
    `always=True` the dedupe is skipped entirely."""
    now = int(time.time())
    key = (bot["id"], step)
    fingerprint = sig if sig is not None else message
    if not always:
        prev = _last.get(key)
        if prev and prev[0] == fingerprint and now - prev[1] < HEARTBEAT_SECONDS:
            return None
    _last[key] = (fingerprint, now)

    cur = conn.execute(
        "INSERT INTO bot_logs (bot_id, user_id, pair, ts, level, step, message, data) VALUES (?,?,?,?,?,?,?,?)",
        (bot["id"], bot["user_id"], bot["pair"], now, level, step, message,
         json.dumps(data) if data else None),
    )
    row_id = cur.lastrowid
    if row_id % 25 == 0:
        conn.execute(
            "DELETE FROM bot_logs WHERE bot_id=? AND id NOT IN "
            "(SELECT id FROM bot_logs WHERE bot_id=? ORDER BY id DESC LIMIT ?)",
            (bot["id"], bot["id"], KEEP_PER_BOT),
        )
    return {
        "id": row_id, "bot_id": bot["id"], "pair": bot["pair"], "ts": now,
        "level": level, "step": step, "message": message, "data": data,
    }


_STATUS_LEVEL = {"pass": "ok", "wait": "info", "block": "warn", "info": "info"}


def log_decision_trace(conn, bot: dict, steps: Iterable[dict], timeframe: str) -> List[dict]:
    """Write the SMC decision trace (smc.evaluate_entry()['steps'])."""
    out = []
    for st in steps:
        row = add_log(
            conn, bot, st["step"], f"[{timeframe}] {st['message']}",
            level=_STATUS_LEVEL.get(st["status"], "info"),
            data={"status": st["status"]}, sig=f"{st['status']}:{st.get('sig', '')}",
        )
        if row:
            out.append(row)
    return out


def serialize_rows(rows) -> List[dict]:
    out = []
    for r in rows:
        d = dict(r)
        raw = d.get("data")
        try:
            d["data"] = json.loads(raw) if raw else None
        except (json.JSONDecodeError, TypeError):
            d["data"] = None
        out.append(d)
    return out
