"""
Central configuration. All environment-driven settings live here so the
rest of the app never touches os.getenv directly.
"""
import logging
import os
from pathlib import Path

SECRET_KEY = os.getenv("SECRET_KEY", "nexusai-super-secret-key-change-in-production")
ALGORITHM = "HS256"

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "nexusai.db"

BINANCE_REST = "https://api.binance.com"
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
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
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
