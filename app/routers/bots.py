import json
import time

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..database import get_db, serialize_bot
from ..schemas import BotCreate, BotUpdate, ConfluenceBacktestRequest
from ..security import get_current_user
from ..services.backtest import run_confluence_backtest
from ..services.botlog import serialize_rows
from ..services.market import fetch_klines

router = APIRouter(prefix="/bots", tags=["bots"])

# Same reasoning as strategies.py's BACKTEST_LIMIT — enough candles for a
# meaningful window at each timeframe within Binance's 1000-per-call cap.
BACKTEST_LIMIT = {"1m": 1000, "5m": 1000, "15m": 1000, "1h": 500, "4h": 500, "1d": 500}


def _require_broker_for_live(user: dict, mode: str):
    """LIVE mode places real orders on the user's own Binance account —
    never allow it without a connected (and by implication, previously
    tested) API key on file."""
    if mode == "LIVE" and not user.get("binance_api_key_enc"):
        raise HTTPException(400, "Connect and test your Binance API keys in Settings before switching a bot to LIVE")


def _validate_confluence(strategy: str, confluence_strategies):
    """Confluence mode is signaled by BOTH the strategy marker AND the list
    being present and consistent — catches a half-configured request
    (e.g. a strategy dropdown reset to a preset name after strategies were
    picked, or vice versa) before it reaches the DB, where tasks.py would
    otherwise silently treat it as either a normal preset bot or an
    unconfigured confluence bot."""
    if confluence_strategies and strategy != "CONFLUENCE":
        raise HTTPException(400, "confluence_strategies was set but strategy isn't 'CONFLUENCE' — pick Confluence mode")
    if strategy == "CONFLUENCE" and not confluence_strategies:
        raise HTTPException(400, "Confluence mode needs at least 2 strategies selected")


@router.get("")
async def list_bots(user=Depends(get_current_user)):
    conn = get_db()
    rows = conn.execute("SELECT * FROM bots WHERE user_id=? ORDER BY started_at DESC", (user["id"],)).fetchall()
    conn.close()
    return [serialize_bot(r) for r in rows]


@router.get("/logs/feed")
async def logs_feed(
    pair: str | None = None, since_id: int = 0, limit: int = 150, user=Depends(get_current_user),
):
    """Activity log across ALL of this user's bots, optionally narrowed to one
    pair — what the chart's activity panel polls. `since_id` returns only rows
    newer than the last one the client already has, so polling stays cheap.
    Oldest-first, so the client can simply append."""
    limit = max(1, min(limit, 500))
    conn = get_db()
    if pair:
        rows = conn.execute(
            "SELECT * FROM bot_logs WHERE user_id=? AND pair=? AND id>? ORDER BY id DESC LIMIT ?",
            (user["id"], pair, since_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM bot_logs WHERE user_id=? AND id>? ORDER BY id DESC LIMIT ?",
            (user["id"], since_id, limit),
        ).fetchall()
    conn.close()
    return serialize_rows(reversed(rows))


@router.get("/{bot_id}/logs")
async def bot_logs(bot_id: str, since_id: int = 0, limit: int = 200, user=Depends(get_current_user)):
    """One bot's activity log, oldest-first."""
    limit = max(1, min(limit, 500))
    conn = get_db()
    bot = conn.execute("SELECT id FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"])).fetchone()
    if not bot:
        conn.close()
        raise HTTPException(404, "Bot not found")
    rows = conn.execute(
        "SELECT * FROM bot_logs WHERE bot_id=? AND id>? ORDER BY id DESC LIMIT ?", (bot_id, since_id, limit),
    ).fetchall()
    conn.close()
    return serialize_rows(reversed(rows))


@router.get("/{bot_id}")
async def get_bot(bot_id: str, user=Depends(get_current_user)):
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"])).fetchone()
    if not bot:
        conn.close()
        raise HTTPException(404, "Bot not found")
    trades = conn.execute(
        "SELECT * FROM trades WHERE bot_id=? ORDER BY created_at DESC LIMIT 200", (bot_id,)
    ).fetchall()
    conn.close()
    return {**serialize_bot(bot), "recent_trades": [dict(t) for t in trades]}


@router.post("/confluence-backtest")
async def confluence_backtest(req: ConfluenceBacktestRequest, user=Depends(get_current_user)):
    """
    "which strategy is the best...can i create a bot that confirms from
    all or selected strategies" — lets a Confluence combo be checked
    against real history BEFORE creating (and funding) the bot, same as
    you'd want to know a single preset's numbers first. Not tied to a
    saved Strategy row, so this is throwaway/exploratory by design — the
    combo only gets saved once you actually create the bot with it.
    """
    async with aiohttp.ClientSession() as session:
        candles = await fetch_klines(session, req.pair, req.timeframe, BACKTEST_LIMIT.get(req.timeframe, 500))

    if len(candles) < 30:
        raise HTTPException(
            503,
            "Couldn't fetch enough real market data to backtest right now "
            "(Binance may be temporarily unreachable) — try again shortly.",
        )

    result = run_confluence_backtest(
        req.confluence_strategies, candles, starting_capital=1000.0, min_agree=req.confluence_min_agree,
    )
    trade_log = result["tradeLog"]
    gross_win = sum(t["pnl"] for t in trade_log if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trade_log if t["pnl"] < 0)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (gross_win if gross_win > 0 else 0)

    return {
        "profit": result["profit"],
        "trades": result["trades"],
        "winRate": result["winRate"],
        "sharpeRatio": result["sharpeRatio"],
        "maxDrawdown": result["maxDrawdown"],
        "profitFactor": profit_factor,
        "tradeLog": trade_log,
        "pair": req.pair,
        "timeframe": req.timeframe,
        "candleCount": len(candles),
        "requiredAgree": req.confluence_min_agree or len(req.confluence_strategies),
    }


@router.post("")
async def create_bot(req: BotCreate, user=Depends(get_current_user)):
    _require_broker_for_live(user, req.mode)
    _validate_confluence(req.strategy, req.confluence_strategies)
    conn = get_db()
    # BUG FIX: creating a bot never touched the wallet at all — `capital`
    # was just a number used later to size trades (risk_fraction *
    # capital), so a user could spin up bots whose capital added up to far
    # more than they actually had, and the balance shown in Settings never
    # moved when a bot was created. DEMO bots now reserve their capital
    # from the wallet up front, exactly like opening a sub-account — this
    # is also what makes "on profit also update bot and balance" coherent:
    # a bot's own capital is what its trades draw from and return to (see
    # tasks.py), and the wallet balance is what's left un-allocated.
    # LIVE bots trade real Binance funds and never touch this at all.
    if req.mode == "DEMO":
        row = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        if row["balance"] < req.capital:
            conn.close()
            raise HTTPException(
                400,
                f"Insufficient balance: this bot needs ${req.capital:,.2f} but you only have ${row['balance']:,.2f} available",
            )
        conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (req.capital, user["id"]))

    bid = f"bot_{int(time.time()*1000)}"
    conn.execute(
        """INSERT INTO bots
        (id,user_id,name,pair,strategy,capital,mode,status,win_rate,max_hold_minutes,
         confidence_threshold,timeframe,cooldown_minutes,auto_pause_after_losses,max_trades,
         max_open_positions,confluence_strategies,confluence_min_agree)
        VALUES (?,?,?,?,?,?,?,'RUNNING',0,?,?,?,?,?,?,?,?,?)""",
        (bid, user["id"], req.name, req.pair, req.strategy, req.capital, req.mode,
         req.max_hold_minutes or None, req.confidence_threshold or 65,
         req.timeframe or "15m", req.cooldown_minutes or 0, req.auto_pause_after_losses
         if req.auto_pause_after_losses is not None else 4, req.max_trades or None,
         req.max_open_positions or 1,
         json.dumps(req.confluence_strategies) if req.confluence_strategies else None,
         req.confluence_min_agree or None),
    )
    conn.commit()
    bot = dict(conn.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone())
    conn.close()
    return serialize_bot(bot)


@router.patch("/{bot_id}")
async def update_bot(bot_id: str, req: BotUpdate, user=Depends(get_current_user)):
    conn = get_db()
    existing = conn.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"])).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, "Bot not found")
    bot = dict(existing)

    # Validate the RESULTING strategy/confluence_strategies combination
    # (not just whichever one this request happens to touch) — a request
    # that only changes strategy back to a single preset while an old
    # confluence_strategies list is still sitting on the row, or vice
    # versa, would otherwise leave the bot half-configured.
    resulting_strategy = req.strategy if req.strategy is not None else bot["strategy"]
    resulting_confluence = req.confluence_strategies if req.confluence_strategies is not None else (
        json.loads(bot["confluence_strategies"]) if bot["confluence_strategies"] else None
    )
    if req.strategy is not None and req.strategy != "CONFLUENCE":
        resulting_confluence = None  # switching to a plain preset clears any old confluence list
    _validate_confluence(resulting_strategy, resulting_confluence)

    if req.pair is not None and req.pair != bot["pair"] and bot["status"] == "RUNNING":
        conn.close()
        raise HTTPException(400, "Stop the bot before changing its trading pair")

    has_open_trade = conn.execute(
        "SELECT 1 FROM trades WHERE bot_id=? AND status='FILLED' LIMIT 1", (bot_id,)
    ).fetchone() is not None

    if req.mode is not None and req.mode != bot["mode"]:
        if bot["status"] == "RUNNING":
            conn.close()
            raise HTTPException(400, "Stop the bot before switching between DEMO and LIVE")
        if has_open_trade:
            conn.close()
            raise HTTPException(400, "Close this bot's open position before switching between DEMO and LIVE")
        try:
            _require_broker_for_live(user, req.mode)
        except HTTPException:
            conn.close()
            raise
        # Moving a bot between DEMO and LIVE moves its capital reservation
        # too: DEMO capital is carved out of the wallet balance (see
        # create_bot); LIVE bots trade real Binance funds and hold nothing
        # back from the demo wallet at all.
        user_row = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
        if req.mode == "LIVE":
            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (bot["capital"], user["id"]))
        else:  # switching back to DEMO — re-reserve its capital
            if user_row["balance"] < bot["capital"]:
                conn.close()
                raise HTTPException(
                    400,
                    f"Insufficient balance: switching this bot back to DEMO needs ${bot['capital']:,.2f} "
                    f"but you only have ${user_row['balance']:,.2f} available",
                )
            conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (bot["capital"], user["id"]))

    if req.status:
        # Resuming from PAUSED (including an auto-pause from the
        # consecutive-loss circuit breaker) clears the loss streak, so the
        # bot gets a clean slate rather than auto-pausing again on its
        # very next loss.
        if req.status == "RUNNING" and bot["status"] == "PAUSED":
            conn.execute("UPDATE bots SET consecutive_losses=0 WHERE id=? AND user_id=?", (bot_id, user["id"]))
        conn.execute("UPDATE bots SET status=? WHERE id=? AND user_id=?", (req.status, bot_id, user["id"]))
    if req.auto_pause_after_losses is not None:
        conn.execute(
            "UPDATE bots SET auto_pause_after_losses=? WHERE id=? AND user_id=?",
            (req.auto_pause_after_losses, bot_id, user["id"]),
        )
    if req.capital is not None and req.capital != bot["capital"]:
        # BUG FIX: adjusting a bot's capital used to just overwrite the
        # number with no connection to the wallet at all — you could raise
        # a bot's capital past what you actually had, or lower it and never
        # see that money reappear in your balance.
        effective_mode = req.mode if req.mode is not None else bot["mode"]
        if effective_mode == "DEMO":
            delta = req.capital - bot["capital"]
            user_row = conn.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
            if delta > 0 and user_row["balance"] < delta:
                conn.close()
                raise HTTPException(
                    400,
                    f"Insufficient balance: increasing this bot's capital by ${delta:,.2f} needs that much "
                    f"free, but you only have ${user_row['balance']:,.2f} available",
                )
            if delta < 0 and has_open_trade:
                conn.close()
                raise HTTPException(400, "Close this bot's open position before lowering its capital")
            conn.execute("UPDATE users SET balance = balance - ? WHERE id=?", (delta, user["id"]))
        conn.execute("UPDATE bots SET capital=? WHERE id=? AND user_id=?", (req.capital, bot_id, user["id"]))
    if req.name is not None:
        conn.execute("UPDATE bots SET name=? WHERE id=? AND user_id=?", (req.name, bot_id, user["id"]))
    if req.strategy is not None:
        conn.execute("UPDATE bots SET strategy=? WHERE id=? AND user_id=?", (req.strategy, bot_id, user["id"]))
        if req.strategy != "CONFLUENCE" and req.confluence_strategies is None:
            # Switching to a plain preset/custom strategy — clear any old
            # confluence config so it doesn't linger and reactivate later.
            conn.execute("UPDATE bots SET confluence_strategies=NULL, confluence_min_agree=NULL WHERE id=? AND user_id=?", (bot_id, user["id"]))
    if req.pair is not None:
        conn.execute("UPDATE bots SET pair=? WHERE id=? AND user_id=?", (req.pair, bot_id, user["id"]))
    if req.mode is not None:
        conn.execute("UPDATE bots SET mode=? WHERE id=? AND user_id=?", (req.mode, bot_id, user["id"]))
    if req.max_hold_minutes is not None:
        # 0 clears the limit (no max hold time); any positive value sets it.
        conn.execute(
            "UPDATE bots SET max_hold_minutes=? WHERE id=? AND user_id=?",
            (req.max_hold_minutes or None, bot_id, user["id"]),
        )
    if req.confidence_threshold is not None:
        conn.execute(
            "UPDATE bots SET confidence_threshold=? WHERE id=? AND user_id=?",
            (req.confidence_threshold, bot_id, user["id"]),
        )
    if req.timeframe is not None:
        conn.execute(
            "UPDATE bots SET timeframe=? WHERE id=? AND user_id=?",
            (req.timeframe, bot_id, user["id"]),
        )
    if req.cooldown_minutes is not None:
        conn.execute(
            "UPDATE bots SET cooldown_minutes=? WHERE id=? AND user_id=?",
            (req.cooldown_minutes, bot_id, user["id"]),
        )
    if req.max_trades is not None:
        # 0 clears the limit (no trade cap); any positive value sets it.
        conn.execute(
            "UPDATE bots SET max_trades=? WHERE id=? AND user_id=?",
            (req.max_trades or None, bot_id, user["id"]),
        )
    if req.max_open_positions is not None:
        conn.execute(
            "UPDATE bots SET max_open_positions=? WHERE id=? AND user_id=?",
            (req.max_open_positions, bot_id, user["id"]),
        )
    if req.confluence_strategies is not None:
        conn.execute(
            "UPDATE bots SET confluence_strategies=? WHERE id=? AND user_id=?",
            (json.dumps(req.confluence_strategies), bot_id, user["id"]),
        )
    if req.confluence_min_agree is not None:
        conn.execute(
            "UPDATE bots SET confluence_min_agree=? WHERE id=? AND user_id=?",
            (req.confluence_min_agree, bot_id, user["id"]),
        )
    conn.commit()
    bot = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
    conn.close()
    return serialize_bot(bot)


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, user=Depends(get_current_user)):
    conn = get_db()
    bot = conn.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"])).fetchone()
    if not bot:
        conn.close()
        raise HTTPException(404, "Bot not found")
    bot = dict(bot)

    has_open_trade = conn.execute(
        "SELECT 1 FROM trades WHERE bot_id=? AND status='FILLED' LIMIT 1", (bot_id,)
    ).fetchone() is not None
    if has_open_trade:
        conn.close()
        raise HTTPException(400, "Close this bot's open position before deleting it")

    # BUG FIX: deleting a bot silently discarded whatever capital was still
    # allocated to it — that money just vanished from the user's world
    # instead of going back to the wallet it came from at creation.
    if bot["mode"] == "DEMO" and bot["capital"]:
        conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (bot["capital"], user["id"]))

    conn.execute("DELETE FROM bot_logs WHERE bot_id=? AND user_id=?", (bot_id, user["id"]))
    conn.execute("DELETE FROM bots WHERE id=? AND user_id=?", (bot_id, user["id"]))
    conn.commit()
    conn.close()
    return {"ok": True}
