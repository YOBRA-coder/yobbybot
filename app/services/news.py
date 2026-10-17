"""
Crypto headlines from public RSS feeds (no API key). Cached for 5 minutes; if a feed is
unreachable the others still show, and if all fail the last good list (or an empty one) is returned.
"""
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Dict, List

import aiohttp

from ..config import log

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]
KEYWORDS = {
    "BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "BNB": ["bnb", "binance"], "SOL": ["solana", "sol "],
    "XRP": ["xrp", "ripple"], "ADA": ["cardano", "ada"], "DOGE": ["dogecoin", "doge"], "AVAX": ["avalanche", "avax"],
    "DOT": ["polkadot", "dot "], "MATIC": ["polygon", "matic"], "LTC": ["litecoin", "ltc"], "LINK": ["chainlink", "link "],
}
TTL = 300
_cache: Dict = {"ts": 0.0, "items": []}
_TAG = re.compile(r"<[^>]+>")


def parse_rss(xml_text: str, source: str) -> List[dict]:
    """Pure parser (unit-tested): RSS 2.0 <item>s -> [{title, link, source, time, summary}]."""
    out: List[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        try:
            ts = int(parsedate_to_datetime(it.findtext("pubDate") or "").timestamp())
        except Exception:  # noqa: BLE001
            ts = 0
        summary = _TAG.sub("", it.findtext("description") or "").strip()[:220]
        out.append({"title": title, "link": link, "source": source, "time": ts, "summary": summary})
    return out


async def _one(session: aiohttp.ClientSession, name: str, url: str) -> List[dict]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), headers={"User-Agent": "Mozilla/5.0 NexusAI"}) as r:
            if r.status != 200:
                return []
            return parse_rss(await r.text(), name)
    except Exception as e:  # noqa: BLE001
        log.warning("news feed %s failed: %s", name, e)
        return []


def filter_for(items: List[dict], symbol: str) -> List[dict]:
    base = symbol.replace("USDT", "")
    keys = KEYWORDS.get(base, [base.lower()] if base else [])
    if not keys:
        return items
    return [i for i in items if any(k in (i["title"] + " " + i["summary"]).lower() + " " for k in keys)]


async def get_news(symbol: str = "", limit: int = 30) -> Dict:
    if time.time() - _cache["ts"] > TTL:
        async with aiohttp.ClientSession() as s:
            got = await asyncio.gather(*(_one(s, n, u) for n, u in FEEDS))
        items = sorted([x for g in got for x in g], key=lambda x: -x["time"])
        if items:
            _cache.update(ts=time.time(), items=items)
        elif _cache["items"]:
            _cache["ts"] = time.time() - TTL + 60   # retry in a minute, keep serving the last good list
    items = filter_for(_cache["items"], symbol) if symbol else _cache["items"]
    return {"items": items[:limit], "updated": int(_cache["ts"]), "filtered": bool(symbol),
            "ok": bool(_cache["items"])}
