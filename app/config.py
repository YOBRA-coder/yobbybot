"""
Central configuration. All environment-driven settings live here so the
rest of the app never touches os.getenv directly.
"""
import logging
import os
from pathlib import Path

ALGORITHM = "HS256"

BASE_DIR = Path(__file__).resolve().parent.parent


def _secret_key() -> str:
    """Token-signing key. Use SECRET_KEY from the environment if set; otherwise generate a random one ONCE and keep it
    in backend/.secret_key (so logins survive restarts). The old built-in default was public — anyone could forge tokens."""
    env = os.getenv("SECRET_KEY", "").strip()
    if env and env != "nexusai-super-secret-key-change-in-production":
        return env
    f = BASE_DIR / ".secret_key"
    try:
        if f.exists() and f.read_text().strip():
            return f.read_text().strip()
        import secrets
        key = secrets.token_hex(32)
        f.write_text(key)
        try:
            f.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError:
        import secrets
        return secrets.token_hex(32)   # read-only disk: valid for this run only (logins reset on restart)


SECRET_KEY = _secret_key()
DB_PATH = BASE_DIR / "nexusai.db"

BINANCE_REST = os.getenv("BINANCE_REST", "https://api.binance.com")
# Hosts tried in order for PUBLIC market data. api.binance.com is blocked in
# some regions / on some cloud hosts (HTTP 451/403) — a big reason a chart
# can sit empty while everything else "runs". data-api.binance.vision serves
# the same public klines/tickers and usually works where the main host does
# not. The first host that answers is remembered (see services/market.py).
BINANCE_HOSTS = [BINANCE_REST] + [
    h for h in (
        "https://api.binance.com", "https://api1.binance.com", "https://api2.binance.com",
        "https://api3.binance.com", "https://data-api.binance.vision",
    ) if h != BINANCE_REST
]
# Opt-in synthetic market for UI testing with no internet:  CRYPTO_DEMO=1.
# Never on by default and always flagged to the UI ("demo": true) — the
# no-fake-data rule for real trading stays intact.
DEMO_MODE = os.getenv("CRYPTO_DEMO", "").lower() in ("1", "true", "yes")
TELEGRAM_API = "https://api.telegram.org"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

PAIRS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT", "LTCUSDT", "LINKUSDT",
]

# Candle intervals a bot/strategy may evaluate on. "1h" was previously the
# only option, hardcoded everywhere — a candle that only changes once an
# hour is why a 15s-polling bot loop mostly just re-read the same signal,
# i.e. "always shows HOLD". Shorter timeframes update, and can actionably
# change, far more often.
# "timeframes works for all add missing" — the chart's own timeframe
# selector (ProChart.tsx's DEFAULT_TIMEFRAMES) always offered 4h and 1d
# too, but bots/strategies/backtesting rejected them (BotCreate/
# StrategyCreate validate against this exact tuple) since they weren't
# listed here — you could VIEW a 4h/1d chart but never run a bot or
# backtest on that timeframe. Added so every timeframe the chart offers
# actually works everywhere else too.
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
DEFAULT_TIMEFRAME = "15m"

# Global fallback only — actual bots use their own bots.confidence_threshold
# (see database.py migrations), defaulting to this same value.
MIN_CONFIDENCE_DEFAULT = 65

BASE_PRICES = {
    "BTCUSDT": 67420, "ETHUSDT": 3521, "BNBUSDT": 412, "SOLUSDT": 178,
    "XRPUSDT": 0.5923, "ADAUSDT": 0.4512, "DOGEUSDT": 0.1234, "AVAXUSDT": 38.45,
    "DOTUSDT": 8.21, "MATICUSDT": 0.7834, "LTCUSDT": 84.12, "LINKUSDT": 14.87,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nexusai")
