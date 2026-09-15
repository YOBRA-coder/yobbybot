import json
from typing import Optional

import aiohttp

from ..config import ANTHROPIC_API, ANTHROPIC_KEY, log


async def get_ai_signal(session: aiohttp.ClientSession, symbol: str, ticker: dict, rsi: float, macd: dict) -> Optional[dict]:
    """Call Claude for an AI-generated trading signal. Returns None if no
    API key is configured or the call fails — callers must handle that."""
    if not ANTHROPIC_KEY:
        return None
    try:
        prompt = f"""Analyze {symbol} for a trading signal. Data:
Price: ${ticker['price']:.4f}, RSI(14): {rsi:.1f}, MACD_hist: {macd['hist']:.6f}
24h change: {ticker['changePct']:.2f}%, High: ${ticker['high24h']:.4f}, Low: ${ticker['low24h']:.4f}

Respond ONLY as valid JSON (no markdown, no extra text):
{{"type":"BUY|SELL|HOLD","confidence":85,"targetPrice":{ticker['price']*1.03:.4f},"stopLoss":{ticker['price']*0.97:.4f},"reason":"Brief analysis here"}}"""

        async with session.post(
            ANTHROPIC_API,
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status == 200:
                data = await r.json()
                text = data["content"][0]["text"].strip()
                text = text.replace("```json", "").replace("```", "").strip()
                return json.loads(text)
            log.warning("Anthropic API returned status %s", r.status)
    except Exception as e:
        log.warning("AI signal error: %s", e)
    return None
