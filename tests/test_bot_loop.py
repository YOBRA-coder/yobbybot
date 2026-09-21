"""End-to-end check of the bot loop's activity logging, with Binance and the
web framework stubbed out.  Run:  python -m tests.test_bot_loop  (from backend/)

Drives the REAL `bot_simulation_loop` and `position_monitor_loop` one cycle
each against a throw-away SQLite file and asserts on what lands in bot_logs.
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── stub the two third-party packages the loops import ─────────────────────
aiohttp = types.ModuleType("aiohttp")


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


aiohttp.ClientSession = _Session
aiohttp.ClientTimeout = lambda **kw: None
sys.modules.setdefault("aiohttp", aiohttp)
fastapi = types.ModuleType("fastapi")
fastapi.WebSocket = object
sys.modules.setdefault("fastapi", fastapi)

from app import database  # noqa: E402
_tmp = Path(tempfile.mkdtemp()) / "test.db"
database.DB_PATH = _tmp
from app import tasks  # noqa: E402
from app.services import market  # noqa: E402
from tests.synthetic import candle  # noqa: E402
from tests.test_smc import flat  # noqa: E402


async def _noop(*a, **k): return None
tasks.manager.send_to_user = _noop
tasks.manager.broadcast = _noop


def _retest_market():
    """270 quiet bars, then an order block, a rally, and a return into it."""
    cs = flat(270)
    n = len(cs)
    cs += [
        candle(n, 100.1, 100.15, 99.85, 99.9), candle(n + 1, 99.9, 101.7, 99.85, 101.5),
        candle(n + 2, 101.5, 102.0, 101.4, 101.9), candle(n + 3, 101.9, 102.3, 101.8, 102.2),
        candle(n + 4, 102.2, 102.4, 101.9, 102.0), candle(n + 5, 102.0, 102.1, 101.5, 101.6),
        candle(n + 6, 101.6, 101.7, 100.9, 101.0), candle(n + 7, 101.0, 101.1, 100.0, 100.08),
    ]
    return cs


def _run_one_cycle(loop_fn):
    """Let a `while True: await asyncio.sleep(x); ...` loop run exactly once."""
    real_sleep = asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()
        await real_sleep(0)

    asyncio.sleep = fake_sleep
    try:
        asyncio.run(loop_fn())
    except asyncio.CancelledError:
        pass
    finally:
        asyncio.sleep = real_sleep


def _setup(candles, price):
    database.init_db()
    conn = database.get_db()
    conn.execute("INSERT INTO users (id,name,email,password_hash) VALUES ('u1','T','t@t','x')")
    conn.execute(
        "INSERT INTO bots (id,user_id,name,pair,strategy,status,mode,capital,timeframe,confidence_threshold) "
        "VALUES ('b1','u1','SMC bot','BTCUSDT','Smart Money Concepts','RUNNING','DEMO',1000,'15m',65)"
    )
    conn.commit()
    conn.close()
    seen = {}

    async def fake_klines(session, symbol, interval, limit, end_time=None):
        seen["limit"] = limit
        return candles

    market.fetch_klines = fake_klines
    tasks.fetch_klines = fake_klines
    market.live_tickers["BTCUSDT"] = {"price": price}
    return seen


def test_bot_logs_every_process_and_places_the_trade():
    cs = _retest_market()
    seen = _setup(cs, cs[-1]["close"])
    _run_one_cycle(tasks.bot_simulation_loop)

    assert seen["limit"] == 300, "SMC bot must read the same 300-candle window as the chart"

    conn = database.get_db()
    steps = [r["step"] for r in conn.execute("SELECT step FROM bot_logs WHERE bot_id='b1' ORDER BY id")]
    for needed in ("SCAN", "STRUCTURE", "SWEEP", "ZONES", "RETEST", "DECISION", "ENTRY"):
        assert needed in steps, f"missing {needed}: {steps}"
    trade = conn.execute("SELECT * FROM trades WHERE bot_id='b1'").fetchone()
    assert trade is not None and trade["side"] == "BUY"
    assert "retest" in trade["reason"].lower()
    entry = conn.execute("SELECT message FROM bot_logs WHERE step='ENTRY'").fetchone()["message"]
    assert "DEMO BUY" in entry and "stop" in entry and "target" in entry
    conn.close()


def test_second_cycle_is_deduplicated_and_gated():
    cs = _retest_market()
    conn = database.get_db()
    before = conn.execute("SELECT COUNT(*) n FROM bot_logs").fetchone()["n"]
    conn.close()
    _run_one_cycle(tasks.bot_simulation_loop)   # same market, bot already holds 1/1
    conn = database.get_db()
    rows = conn.execute("SELECT step,message FROM bot_logs ORDER BY id").fetchall()
    conn.close()
    new = rows[before:]
    # Unchanged structure/sweep/zone/retest lines must NOT be re-logged...
    assert not any(r["step"] in ("STRUCTURE", "SWEEP", "ZONES", "RETEST") for r in new), new
    # ...but the reason no second trade was taken is worth a line.
    assert any(r["step"] == "GATE" and "1/1" in r["message"] for r in new), new


def test_exit_is_logged():
    conn = database.get_db()
    t = conn.execute("SELECT id, take_profit FROM trades WHERE bot_id='b1'").fetchone()
    conn.close()
    market.live_tickers["BTCUSDT"] = {"price": t["take_profit"] * 1.01}   # price runs through the target
    _run_one_cycle(tasks.position_monitor_loop)
    conn = database.get_db()
    ex = conn.execute("SELECT level, message FROM bot_logs WHERE step='EXIT'").fetchone()
    closed = conn.execute("SELECT status FROM trades WHERE id=?", (t["id"],)).fetchone()["status"]
    conn.close()
    assert closed == "CLOSED"
    assert ex and "take-profit hit" in ex["message"] and ex["level"] == "ok", ex


if __name__ == "__main__":
    ok = 0
    for fn in (test_bot_logs_every_process_and_places_the_trade,
               test_second_cycle_is_deduplicated_and_gated, test_exit_is_logged):
        try:
            fn(); ok += 1; print("PASS ", fn.__name__)
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc(); print("FAIL ", fn.__name__, type(e).__name__, e)
    print(f"\n{ok}/3 passed")
    sys.exit(0 if ok == 3 else 1)
