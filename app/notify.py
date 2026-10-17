"""Fan-out for events: the bell feed (DB), live websocket push, and Telegram."""
import json
from typing import Dict

import aiohttp

from .config import log
from .database import add_alert, get_db, get_settings
from .services.telegram import format_outcome_message, format_signal_message, send_telegram
from .ws_manager import manager


async def _telegram_all(session: aiohttp.ClientSession, text: str) -> int:
    if not get_settings().get("telegram_alerts", True):
        return 0
    conn = get_db()
    try:
        users = conn.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token != '' AND telegram_chat_id != ''").fetchall()
    finally:
        conn.close()
    sent = 0
    for u in users:
        if await send_telegram(session, u["telegram_token"], u["telegram_chat_id"], text):
            sent += 1
    return sent


def _decode(sig: Dict) -> Dict:
    d = dict(sig)
    try:
        d["detail"] = json.loads(d.get("detail") or "{}")
    except (ValueError, TypeError):
        d["detail"] = {}
    return d


async def notify_signal(session: aiohttp.ClientSession, sig: Dict) -> None:
    s = _decode(sig)
    title = f"{s['side']} {s['symbol']} — {s['style']} {s['entry_tf']}, grade {s['grade']}"
    body = f"Entry {s['entry']:.6g} · SL {s['sl']:.6g} · TP {s['tp']:.6g} ({s['rr']}R) — {s['reason']}"
    aid = add_alert("SIGNAL", title, body, s["symbol"], s["id"])
    await manager.broadcast({"type": "SIGNAL", "alert_id": aid, "title": title, "body": body, "signal": s})
    n = await _telegram_all(session, format_signal_message(s))
    log.info("signal #%s %s %s alerted (telegram: %d)", s["id"], s["side"], s["symbol"], n)


async def notify_outcome(session: aiohttp.ClientSession, sig: Dict) -> None:
    s = _decode(sig)
    word = {"WIN": "Target hit", "LOSS": "Stop hit", "BE": "Break-even", "EXPIRED": "Expired"}.get(s["status"], s["status"])
    r = s.get("r_result")
    title = f"{word} — {s['side']} {s['symbol']}" + (f" ({r:+.2f}R)" if r is not None else "")
    body = f"{s['style']} {s['entry_tf']} · entry {s['entry']:.6g}" + (f" → exit {s['exit_price']:.6g}" if s.get("exit_price") else "")
    aid = add_alert(s["status"], title, body, s["symbol"], s["id"])
    await manager.broadcast({"type": "OUTCOME", "alert_id": aid, "title": title, "body": body, "signal": s})
    await _telegram_all(session, format_outcome_message(s))
