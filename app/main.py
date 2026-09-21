import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import CORS_ORIGINS, log
from .database import init_db
from .routers import account, auth, bots, market, settings, signals, strategies, trades, ws
from .tasks import (
    auto_signal_loop, bot_simulation_loop, kline_loop, market_data_loop,
    position_monitor_loop, reconcile_exchange_exits_loop,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    t1 = asyncio.create_task(market_data_loop())
    t2 = asyncio.create_task(bot_simulation_loop())
    t3 = asyncio.create_task(kline_loop())
    t4 = asyncio.create_task(auto_signal_loop())
    t5 = asyncio.create_task(position_monitor_loop())
    # Settles LIVE trades that Binance's own resting OCO closed — including
    # ones it filled while this process was down.
    t6 = asyncio.create_task(reconcile_exchange_exits_loop())
    log.info("NexusAI backend started")
    yield
    for t in (t1, t2, t3, t4, t5, t6):
        t.cancel()
    log.info("NexusAI backend stopped")


app = FastAPI(title="NexusAI Trading API", version="2.5.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(account.router)
app.include_router(market.router)
app.include_router(signals.router)
app.include_router(bots.router)
app.include_router(trades.router)
app.include_router(strategies.router)
app.include_router(settings.router)
app.include_router(ws.router)


@app.get("/health")
async def health():
    from .ws_manager import manager
    return {
        "status": "ok", "connected": len(manager.connections), "version": app.version,
        # A way to prove, from OUTSIDE the running process, which fixes are
        # actually deployed — hit GET /health right after restarting and
        # compare against what you expect. If this still shows an older
        # build tag, the server process was not actually restarted with
        # the new code (re-uploading files does not reload a running
        # Python process). If it shows this tag and a symptom persists,
        # it's a real remaining bug, not a stale deploy — report it as
        # that.
        "build": "2026-smc-overlays-retests-botlogs",
    }
