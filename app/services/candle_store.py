"""
Candle storage (SQLite).

Every candle fetched from the exchange is written here, so
  * the chart/analysis still has data when Binance blips or is unreachable,
  * the multi-timeframe scanner and the backtest can read months of history
    without hammering the API,
  * you can inspect what is stored (GET /market/store).

Table: candles(symbol, interval, t, o, h, l, c, v) — `t` is the bar's open
time in MILLISECONDS (same as Binance and fetch_klines). The newest bar is
still forming, so it is simply overwritten on every sync (INSERT OR REPLACE).
Demo candles (CRYPTO_DEMO=1) are never stored.
"""
from typing import Dict, List, Optional

from ..database import get_db

KEEP_PER_SERIES = 100_000   # rows kept per (symbol, interval) — ~70 days of 1m, years of 1h

_ready = False


def _ensure(conn) -> None:
    global _ready
    if _ready:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS candles (
               symbol TEXT NOT NULL, interval TEXT NOT NULL, t INTEGER NOT NULL,
               o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
               PRIMARY KEY (symbol, interval, t)
           )"""
    )
    conn.commit()
    _ready = True


def upsert(symbol: str, interval: str, candles: List[dict]) -> int:
    if not candles:
        return 0
    conn = get_db()
    try:
        _ensure(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO candles (symbol, interval, t, o, h, l, c, v) VALUES (?,?,?,?,?,?,?,?)",
            [(symbol, interval, int(c["time"]), c["open"], c["high"], c["low"], c["close"], c["volume"]) for c in candles],
        )
        n = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol=? AND interval=?", (symbol, interval)).fetchone()[0]
        if n > KEEP_PER_SERIES + 500:
            conn.execute(
                "DELETE FROM candles WHERE symbol=? AND interval=? AND t < "
                "(SELECT t FROM candles WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT 1 OFFSET ?)",
                (symbol, interval, symbol, interval, KEEP_PER_SERIES),
            )
        conn.commit()
        return len(candles)
    finally:
        conn.close()


def load(symbol: str, interval: str, limit: int = 300, before: Optional[int] = None) -> List[dict]:
    """Newest `limit` stored candles (optionally ending at/before `before` ms), oldest first."""
    conn = get_db()
    try:
        _ensure(conn)
        if before:
            rows = conn.execute(
                "SELECT t,o,h,l,c,v FROM candles WHERE symbol=? AND interval=? AND t<=? ORDER BY t DESC LIMIT ?",
                (symbol, interval, before, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT t,o,h,l,c,v FROM candles WHERE symbol=? AND interval=? ORDER BY t DESC LIMIT ?",
                (symbol, interval, limit),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"], "close": r["c"], "volume": r["v"]}
        for r in reversed(rows)
    ]


def stats() -> List[Dict]:
    conn = get_db()
    try:
        _ensure(conn)
        rows = conn.execute(
            "SELECT symbol, interval, COUNT(*) AS n, MIN(t) AS first_t, MAX(t) AS last_t "
            "FROM candles GROUP BY symbol, interval ORDER BY symbol, interval"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"symbol": r["symbol"], "interval": r["interval"], "count": r["n"],
         "first": int(r["first_t"] / 1000), "last": int(r["last_t"] / 1000)}
        for r in rows
    ]
