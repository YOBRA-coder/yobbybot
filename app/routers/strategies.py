import json
import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db
from ..schemas import StrategyCreate, StrategyUpdate
from ..security import get_current_user
from ..services.backtest import DEFAULT_PARAMS, run_backtest as run_backtest_engine
from ..services.market import fetch_klines

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("")
async def list_strategies(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM strategies WHERE user_id=?", (user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.post("")
async def create_strategy(req: StrategyCreate, user=Depends(get_current_user)):
    sid = f"strat_{int(time.time()*1000)}"
    # Seed the preset's default tunable parameters when none were given —
    # without this, the Parameters panel on the Strategy page had nothing
    # to show for a brand-new strategy, and every strategy built on the
    # same preset ran identically no matter what you named it.
    parameters = req.parameters or DEFAULT_PARAMS.get(req.execution, {})
    conn = get_db()
    conn.execute(
        """INSERT INTO strategies (id,user_id,name,description,execution,pair,timeframe,parameters)
        VALUES (?,?,?,?,?,?,?,?)""",
        (sid, user["id"], req.name, req.description, req.execution, req.pair,
         req.timeframe or "15m", json.dumps(parameters)),
    )
    conn.commit()
    s = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone())
    conn.close()
    return s


@router.patch("/{strategy_id}")
async def update_strategy(strategy_id: str, req: StrategyUpdate, user=Depends(get_current_user)):
    conn = get_db()
    existing = conn.execute("SELECT id FROM strategies WHERE id=? AND user_id=?", (strategy_id, user["id"])).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Strategy not found")
    if req.description is not None:
        conn.execute("UPDATE strategies SET description=? WHERE id=?", (req.description, strategy_id))
    if req.execution is not None:
        conn.execute("UPDATE strategies SET execution=? WHERE id=?", (req.execution, strategy_id))
    if req.pair is not None:
        conn.execute("UPDATE strategies SET pair=? WHERE id=?", (req.pair, strategy_id))
    if req.timeframe is not None:
        conn.execute("UPDATE strategies SET timeframe=? WHERE id=?", (req.timeframe, strategy_id))
    if req.parameters is not None:
        conn.execute("UPDATE strategies SET parameters=? WHERE id=?", (json.dumps(req.parameters), strategy_id))
    if req.active is not None:
        conn.execute("UPDATE strategies SET active=? WHERE id=?", (int(req.active), strategy_id))
    conn.commit()
    s = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone())
    conn.close()
    return s


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: str, user=Depends(get_current_user)):
    conn = get_db()
    conn.execute("DELETE FROM strategies WHERE id=? AND user_id=?", (strategy_id, user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}


# Candle count per timeframe chosen so a backtest still covers a
# meaningful window (roughly 1-3 weeks) at Binance's 1000-candle-per-call
# cap, rather than always fetching 500 hourly candles regardless of what
# timeframe the strategy (and any live bot running it) actually evaluates
# on — a strategy backtested on 1h candles but run live on 5m candles
# would report a win rate that has nothing to do with how the bot
# actually trades.
BACKTEST_LIMIT = {"1m": 1000, "5m": 1000, "15m": 1000, "1h": 500, "4h": 500, "1d": 500}


@router.post("/{strategy_id}/backtest")
async def backtest(strategy_id: str, user=Depends(get_current_user)):
    """
    Real backtest — replaces the old `random.random()` placeholder. Fetches
    historical candles for the strategy's pair AND its own timeframe (so
    results actually reflect how a live bot running this strategy would
    behave) and replays the strategy's preset entry/exit rules against
    them, logging every simulated trade (see services/backtest.py). Also
    persists the aggregate stats onto the strategy row so the sidebar list
    (win_rate, total_trades, etc.) reflects the most recent run instead of
    staying at its creation-time defaults forever.
    """
    conn = get_db()
    row = conn.execute("SELECT * FROM strategies WHERE id=? AND user_id=?", (strategy_id, user["id"])).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Strategy not found")
    strat = dict(row)
    timeframe = strat.get("timeframe") or "15m"

    async with aiohttp.ClientSession() as session:
        candles = await fetch_klines(session, strat["pair"], timeframe, BACKTEST_LIMIT.get(timeframe, 500))

    # "ensure i get accurate data for accurate results and consistency" —
    # fetch_klines no longer fabricates candles when Binance is
    # unreachable (see services/market.py), so a real data outage now
    # surfaces here as too few/no candles instead of silently backtesting
    # against fake bars. Fail loudly with a clear message rather than
    # quietly returning a "0 trades / 0% win rate" result that looks like
    # a real (if boring) backtest but is actually just missing data.
    if len(candles) < 30:
        conn.close()
        raise HTTPException(
            503,
            "Couldn't fetch enough real market data to backtest right now "
            "(Binance may be temporarily unreachable) — try again shortly.",
        )

    try:
        parameters = json.loads(strat["parameters"]) if strat["parameters"] else {}
    except (json.JSONDecodeError, TypeError):
        parameters = {}
    result = run_backtest_engine(strat["execution"], candles, starting_capital=1000.0, parameters=parameters)

    # profit_factor: gross wins / gross losses (a standard backtest metric,
    # distinct from raw profit — undefined/infinite when there are no losses).
    trade_log = result["tradeLog"]
    gross_win = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trade_log if t["pnl"] < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0)

    conn.execute(
        "UPDATE strategies SET win_rate=?, total_trades=?, profit_factor=?, max_drawdown=? WHERE id=?",
        (result["winRate"], result["trades"], profit_factor, result["maxDrawdown"], strategy_id),
    )
    conn.commit()
    conn.close()

    return {
        "profit": result["profit"],
        "trades": result["trades"],
        "winRate": result["winRate"],
        "sharpeRatio": result["sharpeRatio"],
        "maxDrawdown": result["maxDrawdown"],
        "tradeLog": trade_log,
        "pair": strat["pair"],
        "timeframe": timeframe,
        "candleCount": len(candles),
    }
