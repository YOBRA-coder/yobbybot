from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str = Field(min_length=6)


class LoginRequest(BaseModel):
    email: str
    password: str


class BotCreate(BaseModel):
    name: str
    pair: str
    strategy: str
    capital: float = Field(default=1000.0, gt=0)
    mode: str = "DEMO"
    max_hold_minutes: Optional[int] = Field(default=None, ge=0)
    # "add extra config/settings" — per-bot tuning instead of one global
    # constant shared by every bot in the system.
    confidence_threshold: Optional[float] = Field(default=65, ge=50, le=95)
    timeframe: Optional[str] = Field(default="15m")
    cooldown_minutes: Optional[int] = Field(default=0, ge=0)
    # Consecutive-loss circuit breaker — auto-pauses the bot after this many
    # losses in a row instead of letting it keep trading through conditions
    # its strategy clearly isn't handling. 0 disables it entirely.
    auto_pause_after_losses: Optional[int] = Field(default=4, ge=0)
    # "stop after how many trades" — lifetime trade-count cap. Once the
    # bot's total trades reaches this, it auto-pauses regardless of how
    # it's performing. None/0 = no limit.
    max_trades: Optional[int] = Field(default=None, ge=0)
    # "improve to place often that work" — how many positions this bot may
    # hold at once. 1 keeps the original one-trade-at-a-time behaviour.
    max_open_positions: Optional[int] = Field(default=1, ge=1, le=10)
    # "can i create a bot that confirms from all or selected strategies" —
    # Confluence mode. When confluence_strategies has 2+ entries, `strategy`
    # must be the literal "CONFLUENCE" marker (see tasks.py) and the bot
    # evaluates every listed preset each cycle instead of just one.
    confluence_strategies: Optional[List[str]] = None
    confluence_min_agree: Optional[int] = Field(default=None, ge=1)

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v):
        if v not in ("DEMO", "LIVE"):
            raise ValueError("mode must be DEMO or LIVE")
        return v

    @field_validator("timeframe")
    @classmethod
    def timeframe_valid(cls, v):
        from .config import TIMEFRAMES
        if v is not None and v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v

    @field_validator("confluence_strategies")
    @classmethod
    def confluence_strategies_valid(cls, v):
        from .services.backtest import PRESETS
        if v is None:
            return v
        if len(v) < 2:
            raise ValueError("confluence_strategies needs at least 2 strategies — use a single strategy bot otherwise")
        bad = [s for s in v if s not in PRESETS]
        if bad:
            raise ValueError(f"Unknown strategy in confluence_strategies: {', '.join(bad)}. Must be one of: {', '.join(PRESETS)}")
        if len(set(v)) != len(v):
            raise ValueError("confluence_strategies has duplicates")
        return v


class BotUpdate(BaseModel):
    status: Optional[str] = None
    capital: Optional[float] = Field(default=None, gt=0)
    name: Optional[str] = None
    strategy: Optional[str] = None
    pair: Optional[str] = None
    mode: Optional[str] = None
    max_hold_minutes: Optional[int] = Field(default=None, ge=0)
    confidence_threshold: Optional[float] = Field(default=None, ge=50, le=95)
    timeframe: Optional[str] = None
    cooldown_minutes: Optional[int] = Field(default=None, ge=0)
    auto_pause_after_losses: Optional[int] = Field(default=None, ge=0)
    max_trades: Optional[int] = Field(default=None, ge=0)
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=10)
    confluence_strategies: Optional[List[str]] = None
    confluence_min_agree: Optional[int] = Field(default=None, ge=1)

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v is not None and v not in ("RUNNING", "PAUSED", "STOPPED"):
            raise ValueError("status must be RUNNING, PAUSED or STOPPED")
        return v

    @field_validator("mode")
    @classmethod
    def mode_valid(cls, v):
        if v is not None and v not in ("DEMO", "LIVE"):
            raise ValueError("mode must be DEMO or LIVE")
        return v

    @field_validator("confluence_strategies")
    @classmethod
    def confluence_strategies_valid(cls, v):
        from .services.backtest import PRESETS
        if v is None:
            return v
        if len(v) < 2:
            raise ValueError("confluence_strategies needs at least 2 strategies — use a single strategy bot otherwise")
        bad = [s for s in v if s not in PRESETS]
        if bad:
            raise ValueError(f"Unknown strategy in confluence_strategies: {', '.join(bad)}. Must be one of: {', '.join(PRESETS)}")
        if len(set(v)) != len(v):
            raise ValueError("confluence_strategies has duplicates")
        return v

    @field_validator("timeframe")
    @classmethod
    def timeframe_valid(cls, v):
        from .config import TIMEFRAMES
        if v is not None and v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v


class TradeCreate(BaseModel):
    pair: str
    side: str
    amount: float = Field(gt=0)
    order_type: str = "market"
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    # "when i place a trade add option to execute in binance" — DEMO (the
    # default) fills against the app's paper wallet exactly as before;
    # BINANCE sends a real signed market order to the connected account.
    # Defaulting to DEMO is deliberate: an omitted or unrecognised venue
    # must never silently spend real money.
    venue: str = "DEMO"

    @field_validator("venue")
    @classmethod
    def venue_valid(cls, v):
        v = (v or "DEMO").upper()
        if v not in ("DEMO", "BINANCE"):
            raise ValueError("venue must be DEMO or BINANCE")
        return v

    @field_validator("side")
    @classmethod
    def side_valid(cls, v):
        v = v.upper()
        if v not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        return v


class TradeUpdate(BaseModel):
    """Used for the 'click to adjust TP/SL' flow. At least one field required."""
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)

    @field_validator("stop_loss", "take_profit")
    @classmethod
    def must_be_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("must be a positive number")
        return v


class CopySignalRequest(BaseModel):
    signal_id: str
    amount: float = Field(default=0.01, gt=0)


class ConfluenceBacktestRequest(BaseModel):
    """Ad-hoc backtest for a Confluence combo — not tied to a saved
    Strategy row, so a combo can be checked from the bot-creation form
    before committing to actually creating (and funding) the bot."""
    pair: str = "BTCUSDT"
    timeframe: str = Field(default="15m")
    confluence_strategies: List[str]
    confluence_min_agree: Optional[int] = Field(default=None, ge=1)

    @field_validator("confluence_strategies")
    @classmethod
    def confluence_strategies_valid(cls, v):
        from .services.backtest import PRESETS
        if len(v) < 2:
            raise ValueError("Pick at least 2 strategies to backtest a confluence combo")
        bad = [s for s in v if s not in PRESETS]
        if bad:
            raise ValueError(f"Unknown strategy: {', '.join(bad)}. Must be one of: {', '.join(PRESETS)}")
        if len(set(v)) != len(v):
            raise ValueError("confluence_strategies has duplicates")
        return v

    @field_validator("timeframe")
    @classmethod
    def timeframe_valid(cls, v):
        from .config import TIMEFRAMES
        if v is not None and v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v


class StrategyCreate(BaseModel):
    name: str
    description: str = ""
    execution: str = "EMA Cross"  # which preset engine this strategy backtests with
    pair: str = "BTCUSDT"
    timeframe: str = "15m"
    parameters: Dict[str, float] = Field(default_factory=dict)

    @field_validator("execution")
    @classmethod
    def execution_valid(cls, v):
        from .services.backtest import PRESETS
        if v not in PRESETS:
            raise ValueError(f"execution must be one of: {', '.join(PRESETS)}")
        return v

    @field_validator("timeframe")
    @classmethod
    def timeframe_valid(cls, v):
        from .config import TIMEFRAMES
        if v is not None and v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v


class StrategyUpdate(BaseModel):
    description: Optional[str] = None
    execution: Optional[str] = None
    pair: Optional[str] = None
    timeframe: Optional[str] = None
    parameters: Optional[Dict[str, float]] = None
    active: Optional[bool] = None

    @field_validator("timeframe")
    @classmethod
    def timeframe_valid(cls, v):
        from .config import TIMEFRAMES
        if v is not None and v not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of: {', '.join(TIMEFRAMES)}")
        return v


class TelegramSettings(BaseModel):
    telegram_token: str
    telegram_chat_id: str


class BrokerSettings(BaseModel):
    api_key: str = Field(min_length=10)
    api_secret: str = Field(min_length=10)
    testnet: bool = True  # defaults to Binance's sandbox, not real funds


class RiskSettings(BaseModel):
    risk_per_trade: float = Field(gt=0.01, le=0.5)  # fraction of bot capital per position; 1%-50%
