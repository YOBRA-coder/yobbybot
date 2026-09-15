"""
SQLite connection + schema.

NOTE: kept as sqlite3 (matching the original project) rather than swapped
for an ORM, so this refactor stays a drop-in replacement. If the app grows
past a single instance, this is the first thing to swap for Postgres.
"""
import json
import sqlite3

from .config import DB_PATH, log


def serialize_bot(bot) -> dict:
    """confluence_strategies is stored as a JSON-encoded TEXT column (see
    the migration below) — decode it back into a real array before it goes
    out over the API or a websocket push, otherwise the frontend receives
    a JSON string nested inside JSON (e.g. '["RSI Scalper","EMA Cross"]' as
    a literal string) instead of an actual array it can render as
    checkboxes/a chip list. Shared by routers/bots.py, routers/ws.py, and
    tasks.py's websocket pushes so every place a bot row leaves the
    process decodes it the same way.
    """
    bot = dict(bot)
    raw = bot.get("confluence_strategies")
    if raw:
        try:
            bot["confluence_strategies"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            bot["confluence_strategies"] = None
    else:
        bot["confluence_strategies"] = None
    return bot


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    telegram_token TEXT DEFAULT '',
    telegram_chat_id TEXT DEFAULT '',
    balance REAL DEFAULT 10000,
    binance_api_key_enc TEXT DEFAULT NULL,
    binance_api_secret_enc TEXT DEFAULT NULL,
    binance_testnet INTEGER DEFAULT 1,
    risk_per_trade REAL DEFAULT 0.08,
    created_at INTEGER DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS bots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    pair TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT DEFAULT 'STOPPED',
    mode TEXT DEFAULT 'DEMO',
    profit REAL DEFAULT 0,
    trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    capital REAL DEFAULT 1000,
    current_position TEXT DEFAULT NULL,
    started_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    bot_id TEXT DEFAULT NULL,
    signal_id TEXT DEFAULT NULL,
    pair TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    total REAL NOT NULL,
    fee REAL NOT NULL,
    pnl REAL DEFAULT 0,
    stop_loss REAL DEFAULT NULL,
    take_profit REAL DEFAULT NULL,
    live INTEGER DEFAULT 0,
    status TEXT DEFAULT 'FILLED',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    closed_at INTEGER DEFAULT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    pair TEXT NOT NULL,
    type TEXT NOT NULL,
    strength REAL DEFAULT 0,
    price REAL NOT NULL,
    target_price REAL NOT NULL,
    stop_loss REAL NOT NULL,
    confidence REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    ai_generated INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ACTIVE',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    execution TEXT DEFAULT '',
    pair TEXT DEFAULT 'BTCUSDT',
    win_rate REAL DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    profit_factor REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    parameters TEXT DEFAULT '{}',
    active INTEGER DEFAULT 1,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

# Columns added after the original schema shipped. Applied defensively so
# upgrading an existing nexusai.db file doesn't require dropping data.
MIGRATIONS = [
    ("trades", "signal_id", "TEXT DEFAULT NULL"),
    ("trades", "stop_loss", "REAL DEFAULT NULL"),
    ("trades", "take_profit", "REAL DEFAULT NULL"),
    ("trades", "closed_at", "INTEGER DEFAULT NULL"),
    ("trades", "live", "INTEGER DEFAULT 0"),
    ("bots", "wins", "INTEGER DEFAULT 0"),
    ("bots", "losses", "INTEGER DEFAULT 0"),
    ("strategies", "execution", "TEXT DEFAULT ''"),
    ("strategies", "pair", "TEXT DEFAULT 'BTCUSDT'"),
    ("strategies", "active", "INTEGER DEFAULT 1"),
    ("users", "balance", "REAL DEFAULT 10000"),
    ("users", "binance_api_key_enc", "TEXT DEFAULT NULL"),
    ("users", "binance_api_secret_enc", "TEXT DEFAULT NULL"),
    ("users", "binance_testnet", "INTEGER DEFAULT 1"),
    ("users", "risk_per_trade", "REAL DEFAULT 0.08"),
    ("bots", "mode", "TEXT DEFAULT 'DEMO'"),
    ("users", "last_demo_deposit_at", "INTEGER DEFAULT NULL"),
    ("bots", "max_hold_minutes", "INTEGER DEFAULT NULL"),
    # Live per-bot market read, refreshed every simulation cycle regardless
    # of whether the bot actually opens a trade — "volatility, ratio,
    # percentage currently in market what it says either buy or sell" so
    # the Bots page can show what each bot is currently seeing without a
    # separate round-trip per card.
    ("bots", "market_signal", "TEXT DEFAULT NULL"),
    ("bots", "market_confidence", "REAL DEFAULT NULL"),
    ("bots", "volatility_pct", "REAL DEFAULT NULL"),
    ("bots", "rr_ratio", "REAL DEFAULT NULL"),
    ("bots", "market_updated_at", "INTEGER DEFAULT NULL"),
    # "add extra config/settings to ensure ultimate bot and profit" — these
    # let each bot be tuned independently instead of sharing one hardcoded
    # global (MIN_CONFIDENCE=65, 1h candles) that made every bot behave the
    # same regardless of its own risk appetite:
    #  - confidence_threshold: this bot's own minimum confidence to enter
    #    (was a single hardcoded constant for every bot in the system)
    #  - timeframe: which candle interval this bot evaluates signals on.
    #    Was hardcoded to "1h" everywhere — since a 1h candle only changes
    #    once an hour, a bot polling every 15s spent ~99% of its cycles
    #    reading the exact same unchanged signal, which is the root cause
    #    of "always shows HOLD" and of trades being rare/uneven between
    #    bots on the same pair. Shorter timeframes update far more often.
    #  - cooldown_minutes: minimum gap between this bot's trades, so a
    #    volatile pair can't have it re-entering every cycle back-to-back
    #  - reason_log: the bot's own last-evaluated reason text, so "why
    #    hasn't this bot traded" is answerable even with no open position
    ("bots", "confidence_threshold", "REAL DEFAULT 65"),
    ("bots", "timeframe", "TEXT DEFAULT '15m'"),
    ("bots", "cooldown_minutes", "INTEGER DEFAULT 0"),
    ("bots", "reason_log", "TEXT DEFAULT ''"),
    # Strategy-level timeframe, plumbed into both backtest and live bot
    # evaluation of that strategy so the two actually agree with each
    # other — previously backtest always replayed 1h candles regardless
    # of what a bot running that same strategy live was evaluating.
    ("strategies", "timeframe", "TEXT DEFAULT '15m'"),
    # "let every trade taken has a reason by bot" — trades had no reason
    # column at all; the signal engines always computed one but it was
    # discarded before it ever reached the trades table.
    ("trades", "reason", "TEXT DEFAULT ''"),
    # Missing risk-management feature: nothing ever stopped a bot from
    # taking loss after loss back-to-back if market conditions just weren't
    # a fit for its strategy right now — a human trader would step back
    # after a losing streak; a bot with no cooldown or circuit breaker
    # would just keep re-entering. consecutive_losses tracks the current
    # losing streak (reset to 0 on any win); auto_pause_after_losses lets a
    # bot pause ITSELF (status -> PAUSED) once that streak hits the
    # configured limit, rather than trading on regardless. 0 = disabled.
    ("bots", "consecutive_losses", "INTEGER DEFAULT 0"),
    ("bots", "auto_pause_after_losses", "INTEGER DEFAULT 4"),
    # Missing feature: "stop after how many trades" — a lifetime trade-count
    # cap per bot, distinct from auto_pause_after_losses (which only reacts
    # to a losing streak). A bot can be doing fine and a user may still
    # want it to stop itself after a fixed number of trades (a session
    # limit, a "try it for 20 trades then review" plan, etc). NULL/0 means
    # no limit — checked in bot_simulation_loop against bots.trades, the
    # bot's own lifetime trade counter.
    ("bots", "max_trades", "INTEGER DEFAULT NULL"),
    # "can i create a bot that confirms from all or selected strategies" —
    # Confluence mode. When set, bot.strategy is the literal "CONFLUENCE"
    # marker and these two columns drive entry instead of the normal
    # single-strategy/custom pipeline:
    #  - confluence_strategies: JSON array of preset names (from
    #    services/backtest.py PRESETS) this bot evaluates every cycle.
    #  - confluence_min_agree: how many of those must independently signal
    #    BUY in the same cycle before the bot actually enters. Defaults to
    #    len(confluence_strategies) (i.e. ALL must agree) when NULL/0 —
    #    fewer, higher-conviction trades. Set to 1 for "ANY" mode instead —
    #    enters the moment a single selected strategy fires, which trades
    #    more often since it isn't waiting on every strategy to line up.
    ("bots", "confluence_strategies", "TEXT DEFAULT NULL"),
    ("bots", "confluence_min_agree", "INTEGER DEFAULT NULL"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    for table, column, coltype in MIGRATIONS:
        if not _column_exists(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            log.info("migrated: added %s.%s", table, column)
    conn.commit()
    conn.close()
    log.info("Database ready at %s", DB_PATH)
