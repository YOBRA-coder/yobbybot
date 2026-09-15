import asyncio
import json
import random
import time

import aiohttp

from .config import log
from .database import get_db, serialize_bot
from .services.market import fetch_binance_tickers, fetch_klines
from .ws_manager import manager


async def market_data_loop():
    """Continuously fetch market data and broadcast to connected clients."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                tickers = await fetch_binance_tickers(session)
                if manager.connected_users:
                    await manager.broadcast({"type": "TICKERS", "data": tickers})
            except Exception as e:
                log.error("Market loop error: %s", e)
            await asyncio.sleep(5)


async def kline_loop():
    """Push the latest 1m candle for the most-watched pairs so open charts stay live."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    candles = await fetch_klines(session, symbol, "1m", 2)
                    if candles:
                        await manager.broadcast({
                            "type": "KLINE", "symbol": symbol, "interval": "1m", "data": candles[-1],
                        })
            except Exception as e:
                log.error("Kline loop error: %s", e)
            await asyncio.sleep(2)


async def auto_signal_loop():
    """
    Background signal generation — the "auto generate, show them, user
    decides to copy/send, or generates manually if they want" requirement.
    Runs independently of the manual 'Generate' button on the Signals page.

    Only generates for currently-connected users (no point filling the DB
    with signals for someone who isn't logged in to see them), picks one
    random pair per user per cycle, and broadcasts over the socket only —
    consistent with the Phase 1 fix: nothing calls a matching REST endpoint
    for these, so nothing else double-adds them client-side.

    "bring accurate recent signals and verify market every time" — every
    cycle re-fetches fresh candles/ticker from Binance (never cached), and
    now reads 15m candles instead of 1h. A 1h candle is identical for the
    full hour it's open, so on a 1h read most cycles were just re-scoring
    the same stale bar; 15m candles actually move within the cycle window
    below, so "recent" reflects the last 15 minutes of price action, not
    up to an hour ago. Interval shortened from 90s to 45s to match.
    """
    import random as _random

    from .config import PAIRS
    from .services.indicators import generate_signal_from_ta
    from .services.market import fetch_klines, live_tickers

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(45)
            if not manager.connected_users:
                continue
            try:
                conn = get_db()
                for user_id in list(manager.connected_users):
                    symbol = _random.choice(PAIRS)
                    ticker = live_tickers.get(symbol)
                    if not ticker:
                        continue
                    candles = await fetch_klines(session, symbol, "15m", 96)
                    ta = generate_signal_from_ta(candles, ticker)
                    if ta["type"] == "HOLD":
                        continue  # only surface actionable auto-signals, not noise
                    sid = f"auto_{int(time.time()*1000)}_{symbol}"
                    conn.execute(
                        """INSERT INTO signals
                        (id,user_id,pair,type,strength,price,target_price,stop_loss,confidence,reason,ai_generated,status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,0,'ACTIVE')""",
                        (sid, user_id, symbol, ta["type"], ta["strength"], ticker["price"],
                         ta["targetPrice"], ta["stopLoss"], ta["confidence"], ta["reason"]),
                    )
                    await manager.send_to_user(user_id, {
                        "type": "NEW_SIGNALS",
                        "data": [{
                            "id": sid, "pair": symbol, "type": ta["type"], "strength": ta["strength"],
                            "price": ticker["price"], "target_price": ta["targetPrice"], "stop_loss": ta["stopLoss"],
                            "confidence": ta["confidence"], "reason": ta["reason"], "ai_generated": 0,
                            "status": "ACTIVE", "timestamp": int(time.time() * 1000),
                        }],
                    })
                conn.commit()
                conn.close()
            except Exception as e:
                log.error("Auto-signal loop error: %s", e)


async def bot_simulation_loop():
    """
    Real strategy-driven bot ENTRY logic. Previously this just flipped a
    weighted coin every cycle (12% chance of a trade, random win/loss) —
    "enhance bot to work best conditions place best trades" meant actually
    tying entries to the same technical-analysis engine used elsewhere.

    Closing (stop-loss/take-profit hits) is handled by position_monitor_loop
    below, which covers every open trade in the system — manual, copied,
    and bot-owned alike — rather than duplicating that logic per-bot here.

    Each cycle, for every RUNNING or PAUSED bot: runs generate_signal_from_ta
    (the same engine backing /signals/generate and auto-signals) or the
    bot's chosen preset against its pair, and records that read on the bot
    regardless of status. Only RUNNING bots with no open position ever go on
    to actually open a trade — PAUSED bots (manual, or auto-paused by the
    consecutive-loss circuit breaker) get their market read refreshed like
    everyone else but never trade until resumed.

    Each bot now reads its OWN candle timeframe (bots.timeframe, default
    15m) rather than a hardcoded "1h" — this is the fix for "shows current
    market and its always hold" and for two bots on the same pair trading
    at wildly uneven rates: an hourly candle is identical for the full
    hour it's open, so most 15s-poll cycles were just re-scoring a signal
    that hadn't (and couldn't have) changed since the last check, and any
    difference in trade count between two same-pair bots came down to
    which exact hourly bar happened to be open when each one polled, not
    their strategies. A shorter, per-bot timeframe means the market
    picture — and the "verify market every time" requirement — is actually
    live on the scale this loop runs at, and different bots/strategies on
    the same pair diverge for real strategic reasons rather than polling
    luck.

    confidence_threshold and cooldown_minutes are also per-bot now (bots
    table), not one global constant — "add extra config/settings" so each
    bot's aggressiveness can be tuned independently instead of every bot
    in the system sharing the exact same entry bar.
    """
    from .crypto import decrypt
    from .services.backtest import evaluate_confluence_live, evaluate_preset_live
    from .services.binance_client import extract_fill, place_market_order
    from .services.indicators import generate_signal_from_ta
    from .services.market import fetch_klines, live_tickers

    RISK_FRACTION_DEFAULT = 0.08     # fallback if a user row is somehow missing the column
    MIN_CONFIDENCE_DEFAULT = 65      # fallback if a bot row is somehow missing the column
    # "timeframes works for all add missing" — 4h/1d added so a bot set to
    # either actually gets a sensible warm-up window instead of silently
    # falling through to the .get(timeframe, 96) default below, which was
    # tuned for 15m (24h of history), not remotely enough real "bars" of
    # context for a 4h/1d strategy to evaluate meaningfully. ~10 days for
    # 4h, ~2 months for 1d — both comfortably past evaluate_preset_live's
    # own 30-candle minimum.
    TIMEFRAME_CANDLES = {"1m": 120, "5m": 120, "15m": 96, "1h": 60, "4h": 60, "1d": 60}

    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(15)
            try:
                conn = get_db()
                # Missing feature: PAUSED bots (including ones auto-paused by
                # the consecutive-loss circuit breaker) used to go completely
                # dark — no market signal, no reason log, nothing — until
                # resumed, so there was no way to see whether conditions had
                # improved without turning the bot back on blind. Now paused
                # bots still get their market read refreshed every cycle;
                # they just skip the "open a new trade" section below.
                bots = conn.execute("SELECT * FROM bots WHERE status IN ('RUNNING','PAUSED')").fetchall()
                touched_users = set()
                creds_cache: dict = {}  # user_id -> (api_key, api_secret, testnet) | None

                def get_creds(user_id: str):
                    if user_id in creds_cache:
                        return creds_cache[user_id]
                    row = conn.execute(
                        "SELECT binance_api_key_enc, binance_api_secret_enc, binance_testnet FROM users WHERE id=?",
                        (user_id,),
                    ).fetchone()
                    creds = None
                    if row and row["binance_api_key_enc"] and row["binance_api_secret_enc"]:
                        key = decrypt(row["binance_api_key_enc"])
                        secret = decrypt(row["binance_api_secret_enc"])
                        if key and secret:
                            creds = (key, secret, bool(row["binance_testnet"]))
                    creds_cache[user_id] = creds
                    return creds

                for bot_row in bots:
                    bot = dict(bot_row)
                    touched_users.add(bot["user_id"])
                    ticker = live_tickers.get(bot["pair"])
                    if not ticker:
                        continue
                    price = ticker["price"]
                    is_live = bot["mode"] == "LIVE"

                    already_open = conn.execute(
                        "SELECT 1 FROM trades WHERE bot_id=? AND status='FILLED' LIMIT 1", (bot["id"],)
                    ).fetchone()

                    timeframe = bot["timeframe"] or "15m"
                    candles = await fetch_klines(session, bot["pair"], timeframe, TIMEFRAME_CANDLES.get(timeframe, 96))
                    # "can i create a bot that confirms from all or selected
                    # strategies" — Confluence mode takes priority over the
                    # normal single-strategy pipeline below when configured.
                    if bot["strategy"] == "CONFLUENCE" and bot["confluence_strategies"]:
                        try:
                            confluence_list = json.loads(bot["confluence_strategies"])
                        except (json.JSONDecodeError, TypeError):
                            confluence_list = []
                        ta = evaluate_confluence_live(confluence_list, candles, bot["confluence_min_agree"]) if confluence_list else None
                    else:
                        ta = None
                    # BUG FIX: this always called generate_signal_from_ta here,
                    # completely ignoring bot["strategy"] — a bot set to "RSI
                    # Scalper" behaved identically to one set to "EMA Cross" or
                    # a user's own custom strategy, because none of them were
                    # ever actually evaluated. evaluate_preset_live runs the
                    # real rule for one of the 5 built-in presets.
                    if ta is None:
                        ta = evaluate_preset_live(bot["strategy"], candles)
                    if ta is None:
                        # bot["strategy"] isn't a preset name directly — it
                        # may be the NAME of one of the user's own strategies
                        # (created on the Strategy page), whose actual
                        # runnable rule lives in that row's `execution`
                        # field, not its name. Look it up and evaluate the
                        # real rule it was backtested with, rather than
                        # falling straight through to the generic engine.
                        strat_row = conn.execute(
                            "SELECT execution, parameters FROM strategies WHERE user_id=? AND name=?",
                            (bot["user_id"], bot["strategy"]),
                        ).fetchone()
                        if strat_row:
                            try:
                                strat_params = json.loads(strat_row["parameters"]) if strat_row["parameters"] else {}
                            except (json.JSONDecodeError, TypeError):
                                strat_params = {}
                            ta = evaluate_preset_live(strat_row["execution"], candles, strat_params)
                    if ta is None:
                        # Truly custom, no structured rule to run — fall back
                        # to the general TA signal engine, same as before.
                        ta = generate_signal_from_ta(candles, ticker)

                    # Volatility as the average absolute 1h % move over the
                    # last 20 candles (simple, symmetric, doesn't need a
                    # long lookback to mean something) — and risk:reward as
                    # the ratio between the distance to this signal's target
                    # vs its stop, so "1.8" reads as "risking $1 to make
                    # $1.80", independent of whatever confidence score the
                    # strategy itself reports.
                    recent = candles[-20:] if len(candles) >= 2 else candles
                    pct_moves = [
                        abs(c["close"] - c["open"]) / c["open"] * 100
                        for c in recent if c.get("open")
                    ]
                    volatility_pct = (sum(pct_moves) / len(pct_moves)) if pct_moves else None
                    tp_dist = abs((ta.get("targetPrice") or price) - price)
                    sl_dist = abs(price - (ta.get("stopLoss") or price))
                    rr_ratio = round(tp_dist / sl_dist, 2) if sl_dist > 0 else None

                    conn.execute(
                        """UPDATE bots SET market_signal=?, market_confidence=?, volatility_pct=?,
                           rr_ratio=?, market_updated_at=?, reason_log=? WHERE id=?""",
                        (ta["type"], ta.get("confidence"), volatility_pct, rr_ratio, int(time.time()),
                         ta.get("reason", ""), bot["id"]),
                    )

                    if bot["status"] != "RUNNING":
                        continue  # PAUSED: show the market read, but never open a new trade

                    if already_open:
                        continue  # position_monitor_loop will close it when SL/TP is hit

                    # "stop after how many trades" — a lifetime trade-count
                    # cap, separate from the consecutive-loss circuit
                    # breaker above (that one only reacts to a losing
                    # streak; this one is a deliberate session/plan limit
                    # the user sets regardless of performance). 0/NULL =
                    # no limit. Auto-pauses (not stops) so the bot, its
                    # stats, and its capital stay intact and resumable —
                    # matches the auto_pause_after_losses UX.
                    max_trades = bot["max_trades"] or 0
                    if max_trades > 0 and bot["trades"] >= max_trades:
                        conn.execute("UPDATE bots SET status='PAUSED' WHERE id=?", (bot["id"],))
                        log.info("Bot %s auto-paused after reaching its %d-trade limit", bot["id"], max_trades)
                        await manager.send_to_user(bot["user_id"], {
                            "type": "BOT_ERROR",
                            "data": {
                                "bot_id": bot["id"],
                                "message": f'"{bot["name"]}" auto-paused — reached its {max_trades}-trade limit. Raise the limit or resume to continue.',
                            },
                        })
                        continue

                    # Per-bot cooldown — "increase chances of placing trades... but
                    # only with high confidence" also means not re-firing on the
                    # same signal every single 15s cycle the moment a position
                    # closes. 0 (the default) means no cooldown at all.
                    cooldown_minutes = bot["cooldown_minutes"] or 0
                    if cooldown_minutes > 0:
                        last_trade = conn.execute(
                            "SELECT created_at FROM trades WHERE bot_id=? ORDER BY created_at DESC LIMIT 1",
                            (bot["id"],),
                        ).fetchone()
                        if last_trade and (time.time() - last_trade["created_at"]) / 60 < cooldown_minutes:
                            continue

                    if is_live:
                        creds = get_creds(bot["user_id"])
                        if not creds:
                            # Credentials were removed/disconnected after this bot was
                            # set to LIVE (e.g. via /settings/broker DELETE, which
                            # already force-stops LIVE bots — this is a defensive
                            # fallback in case that ever gets out of sync).
                            conn.execute("UPDATE bots SET status='STOPPED' WHERE id=?", (bot["id"],))
                            await manager.send_to_user(bot["user_id"], {
                                "type": "BOT_ERROR",
                                "data": {"bot_id": bot["id"], "message": f'"{bot["name"]}" stopped — Binance is no longer connected'},
                            })
                            continue
                        api_key, api_secret, testnet = creds

                    min_confidence = bot["confidence_threshold"] or MIN_CONFIDENCE_DEFAULT
                    if ta["type"] not in ("BUY", "SELL") or ta["confidence"] < min_confidence:
                        continue
                    risk_row = conn.execute("SELECT risk_per_trade FROM users WHERE id=?", (bot["user_id"],)).fetchone()
                    risk_fraction = risk_row["risk_per_trade"] if risk_row and risk_row["risk_per_trade"] else RISK_FRACTION_DEFAULT
                    risk_amount = bot["capital"] * risk_fraction
                    amount = round(risk_amount / price, 6)
                    if amount <= 0:
                        continue

                    entry_price = price
                    if is_live:
                        try:
                            order = await place_market_order(
                                session, api_key, api_secret, testnet,
                                bot["pair"], ta["type"], amount,
                            )
                            fill = extract_fill(order)
                            if fill:
                                entry_price = fill["avg_price"]
                                amount = fill["executed_qty"]
                        except Exception as e:
                            log.error("LIVE entry order failed for bot %s: %s", bot["id"], e)
                            await manager.send_to_user(bot["user_id"], {
                                "type": "BOT_ERROR",
                                "data": {"bot_id": bot["id"], "message": f'Failed to place live order for "{bot["name"]}": {e}'},
                            })
                            continue  # don't record a trade that never actually happened

                    total = entry_price * amount
                    fee = total * 0.001

                    if not is_live:
                        # DEMO bots spend from their OWN reserved capital pool, not
                        # the shared wallet balance. That reservation happens once,
                        # up front, when the bot is created/funded (see
                        # routers/bots.py) — exactly like opening a sub-account —
                        # so a bot's trading can never eat into money the user
                        # allocated to something else, and "capital" on the bot
                        # actually reflects that bot's current tradable equity
                        # rather than just being a number used for sizing math.
                        if bot["capital"] < total + fee:
                            continue  # not enough capital allocated to this bot for this position size
                        conn.execute("UPDATE bots SET capital = capital - ? WHERE id=?", (total + fee, bot["id"]))

                    trade_id = f"t_{int(time.time()*1000)}_{bot['id'][-4:]}"
                    # "let every trade taken has a reason by bot" — the exact
                    # reason text the signal engine (or preset rule) computed
                    # for this entry, persisted onto the trade itself rather
                    # than only living transiently in the WS payload.
                    reason = ta.get("reason", "")
                    conn.execute(
                        """INSERT INTO trades (id,user_id,bot_id,pair,side,price,amount,total,fee,pnl,stop_loss,take_profit,live,status,reason)
                        VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,'FILLED',?)""",
                        (trade_id, bot["user_id"], bot["id"], bot["pair"], ta["type"],
                         entry_price, amount, total, fee, ta["stopLoss"], ta["targetPrice"], int(is_live), reason),
                    )
                    new_trades = bot["trades"] + 1
                    conn.execute(
                        "UPDATE bots SET trades=?, current_position=? WHERE id=?",
                        (new_trades, "LONG" if ta["type"] == "BUY" else "SHORT", bot["id"]),
                    )
                    await manager.send_to_user(bot["user_id"], {
                        "type": "NEW_TRADE",
                        "data": {"id": trade_id, "pair": bot["pair"], "side": ta["type"],
                                 "price": entry_price, "amount": amount, "total": total, "fee": fee,
                                 "pnl": 0, "stop_loss": ta["stopLoss"], "take_profit": ta["targetPrice"],
                                 "reason": reason, "confidence": ta.get("confidence"),
                                 "live": is_live, "bot_id": bot["id"], "timestamp": int(time.time() * 1000), "status": "FILLED"},
                    })

                conn.commit()
                for uid in touched_users:
                    updated = conn.execute("SELECT * FROM bots WHERE user_id=?", (uid,)).fetchall()
                    await manager.send_to_user(uid, {"type": "BOTS_UPDATE", "data": [serialize_bot(b) for b in updated]})
                conn.close()
            except Exception as e:
                log.error("Bot loop error: %s", e)


async def position_monitor_loop():
    """
    Auto-closes ANY open trade (manual, signal-copied, or bot-owned — DEMO
    or LIVE) once price crosses its stop-loss or take-profit. Previously
    only bot-owned trades were ever auto-closed (inside bot_simulation_loop)
    — a manually-placed or signal-copied trade with SL/TP set just sat
    there forever with no monitoring at all, no matter how far price moved
    past either level.
    """
    from .crypto import decrypt
    from .services.binance_client import extract_fill, place_market_order
    from .services.market import live_tickers

    async with aiohttp.ClientSession() as session:
        while True:
            # "bots places trade that dont hold for long especially the
            # stop loss" — this loop only reads the already-cached
            # live_tickers dict (no network call except when actually
            # closing a position), so there's no real cost to checking it
            # often. It was checking every 10s on top of tickers only
            # refreshing every 5s, meaning a stop-loss hit could sit
            # undetected for up to ~15s while price kept moving against an
            # open position. 3s tightens that worst case to ~8s.
            await asyncio.sleep(3)
            try:
                conn = get_db()
                open_trades = conn.execute(
                    "SELECT * FROM trades WHERE status='FILLED'"
                ).fetchall()
                touched_users = set()
                touched_bots = set()
                creds_cache: dict = {}

                def get_creds(user_id: str):
                    if user_id in creds_cache:
                        return creds_cache[user_id]
                    row = conn.execute(
                        "SELECT binance_api_key_enc, binance_api_secret_enc, binance_testnet FROM users WHERE id=?",
                        (user_id,),
                    ).fetchone()
                    creds = None
                    if row and row["binance_api_key_enc"] and row["binance_api_secret_enc"]:
                        key = decrypt(row["binance_api_key_enc"])
                        secret = decrypt(row["binance_api_secret_enc"])
                        if key and secret:
                            creds = (key, secret, bool(row["binance_testnet"]))
                    creds_cache[user_id] = creds
                    return creds

                for row in open_trades:
                    trade = dict(row)
                    ticker = live_tickers.get(trade["pair"])
                    if not ticker:
                        continue
                    price = ticker["price"]
                    sl, tp, side = trade["stop_loss"], trade["take_profit"], trade["side"]
                    hit_sl = sl and ((side == "BUY" and price <= sl) or (side == "SELL" and price >= sl))
                    hit_tp = tp and ((side == "BUY" and price >= tp) or (side == "SELL" and price <= tp))

                    # "user select time a trade should take and exits" — an
                    # optional max-hold-time per bot (bots.max_hold_minutes),
                    # checked here alongside SL/TP so a trade auto-closes at
                    # market once it's been open too long, regardless of
                    # where price is. Only bot-owned trades have this; manual
                    # and copied trades are unaffected.
                    hit_time = False
                    if trade["bot_id"]:
                        bot_row = conn.execute(
                            "SELECT max_hold_minutes FROM bots WHERE id=?", (trade["bot_id"],)
                        ).fetchone()
                        if bot_row and bot_row["max_hold_minutes"]:
                            elapsed_minutes = (time.time() - trade["created_at"]) / 60
                            hit_time = elapsed_minutes >= bot_row["max_hold_minutes"]

                    if not (hit_sl or hit_tp or hit_time):
                        continue

                    exit_price = price
                    if trade["live"]:
                        creds = get_creds(trade["user_id"])
                        if not creds:
                            continue  # can't close a live position without credentials; leave it, will retry
                        api_key, api_secret, testnet = creds
                        try:
                            closing_side = "SELL" if side == "BUY" else "BUY"
                            order = await place_market_order(session, api_key, api_secret, testnet, trade["pair"], closing_side, trade["amount"])
                            fill = extract_fill(order)
                            if fill:
                                exit_price = fill["avg_price"]
                        except Exception as e:
                            log.error("Auto-close live order failed for trade %s: %s", trade["id"], e)
                            await manager.send_to_user(trade["user_id"], {
                                "type": "BOT_ERROR",
                                "data": {"bot_id": trade["bot_id"], "message": f"Failed to auto-close live position: {e}"},
                            })
                            continue

                    pnl = ((exit_price - trade["price"]) if side == "BUY" else (trade["price"] - exit_price)) * trade["amount"]
                    pnl -= trade["fee"]
                    if hit_time and not (hit_sl or hit_tp):
                        log.info("Trade %s auto-closed on max hold time", trade["id"])
                    conn.execute(
                        "UPDATE trades SET status='CLOSED', pnl=?, closed_at=? WHERE id=?",
                        (pnl, int(time.time()), trade["id"]),
                    )

                    if not trade["live"]:
                        if trade["bot_id"]:
                            # Bot-owned demo trade — settles against the bot's OWN
                            # reserved capital pool (see the matching deduction in
                            # bot_simulation_loop), not the shared wallet balance.
                            # This is what makes "on profit also update bot and
                            # balance" correct: the bot's capital (and therefore
                            # what's shown for it) grows or shrinks with its own
                            # results, and the user's wallet balance is only
                            # touched when capital is actually allocated to or
                            # withdrawn from a bot (create/adjust/delete).
                            pass  # capital credit happens in the single UPDATE below
                        else:
                            # Manual/copied demo trade — settles against the
                            # user's own wallet balance.
                            conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (trade["total"] + pnl, trade["user_id"]))

                    if trade["bot_id"]:
                        bot = conn.execute("SELECT * FROM bots WHERE id=?", (trade["bot_id"],)).fetchone()
                        if bot:
                            bot = dict(bot)
                            new_wins = bot["wins"] + (1 if pnl >= 0 else 0)
                            new_losses = bot["losses"] + (0 if pnl >= 0 else 1)
                            new_wr = (new_wins / bot["trades"] * 100) if bot["trades"] else 0
                            capital_credit = (trade["total"] + pnl) if not trade["live"] else 0

                            # Missing feature: a consecutive-loss circuit breaker.
                            # A losing streak resets on any win; hitting the
                            # bot's own configured limit auto-pauses it instead
                            # of letting it keep re-entering into conditions its
                            # strategy clearly isn't handling right now. The
                            # user can always resume it manually from the Bots
                            # page once they're happy with it again.
                            new_streak = 0 if pnl >= 0 else bot["consecutive_losses"] + 1
                            limit = bot["auto_pause_after_losses"] or 0
                            should_pause = limit > 0 and new_streak >= limit and bot["status"] == "RUNNING"
                            new_status = "PAUSED" if should_pause else bot["status"]

                            conn.execute(
                                """UPDATE bots SET capital=capital+?, profit=profit+?, wins=?, losses=?,
                                   win_rate=?, current_position=NULL, consecutive_losses=?, status=? WHERE id=?""",
                                (capital_credit, pnl, new_wins, new_losses, new_wr, new_streak, new_status, bot["id"]),
                            )
                            touched_bots.add(bot["id"])
                            if should_pause:
                                log.info("Bot %s auto-paused after %d consecutive losses", bot["id"], new_streak)
                                await manager.send_to_user(trade["user_id"], {
                                    "type": "BOT_ERROR",
                                    "data": {
                                        "bot_id": bot["id"],
                                        "message": f'"{bot["name"]}" auto-paused after {new_streak} losses in a row — review and resume when ready',
                                    },
                                })

                    touched_users.add(trade["user_id"])
                    await manager.send_to_user(trade["user_id"], {
                        "type": "NEW_TRADE",
                        "data": {**trade, "status": "CLOSED", "pnl": pnl, "timestamp": int(time.time() * 1000)},
                    })

                conn.commit()
                for uid in touched_users:
                    updated = conn.execute("SELECT * FROM bots WHERE user_id=?", (uid,)).fetchall()
                    await manager.send_to_user(uid, {"type": "BOTS_UPDATE", "data": [serialize_bot(b) for b in updated]})
                conn.close()
            except Exception as e:
                log.error("Position monitor loop error: %s", e)
