import aiohttp

from ..config import TELEGRAM_API, log


async def send_telegram(session: aiohttp.ClientSession, token: str, chat_id: str, message: str) -> bool:
    try:
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        async with session.post(
            url, json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            return r.status == 200
    except Exception as e:
        log.warning("Telegram error: %s", e)
        return False


def format_signal_message(s: dict) -> str:
    emoji = "🟢" if s["type"] == "BUY" else "🔴" if s["type"] == "SELL" else "🟡"
    return (
        f"{emoji} <b>{s['type']} Signal — {s['pair']}</b>\n\n"
        f"💰 Entry: <code>${s['price']:.4f}</code>\n"
        f"🎯 Target: <code>${s['target_price']:.4f}</code>\n"
        f"🛑 Stop Loss: <code>${s['stop_loss']:.4f}</code>\n"
        f"📊 Confidence: <b>{s['confidence']:.0f}%</b>\n\n"
        f"📝 {s['reason']}\n\n"
        f"{'🤖 <i>AI-powered signal</i>' if s['ai_generated'] else ''}\n"
        f"⚡ NexusAI Trading Platform"
    )


def _px(p: float) -> str:
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.6f}"


def format_mtf_signal(r: dict) -> str:
    emoji = "🟢" if r["side"] == "BUY" else "🔴"
    return (
        f"{emoji} <b>{r['side']} {r['symbol']}</b> · {r['style']} {r['entry_tf']} · grade {r['grade']} ({r['agreement']:.0f}%)\n\n"
        f"Entry: <code>{_px(r['entry'])}</code>\n"
        f"Stop: <code>{_px(r['sl'])}</code>\n"
        f"Target: <code>{_px(r['tp'])}</code>  ({r['rr']}R)\n\n"
        f"📍 {r['zone']}\n"
        f"📝 {r['reason']}\n\n"
        f"<i>Not financial advice — manage your own risk.</i>"
    )


def format_mtf_result(r: dict) -> str:
    win = r["status"] == "WIN"
    return (
        f"{'✅' if win else '❌'} <b>{r['symbol']} {r['side']} {r['status']}</b> "
        f"({r['r_result']:+.1f}R) — entry {_px(r['entry'])}, exit {_px(r['exit_price'] or 0)}"
    )
