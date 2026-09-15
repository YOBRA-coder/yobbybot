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
