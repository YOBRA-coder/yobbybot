import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import CORS_ORIGINS, log
from .database import init_db
from .routers import auth, market, settings
from .tasks import market_data_loop, mtf_scanner_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # CryptoSignal is a SIGNAL app: it reads the market and tells you what to
    # watch. There are no bots, no order placement and no exchange keys. The
    # old trading loops/routers (bots, trades, strategies, account, live
    # positions) are intentionally not started — their code is still in the
    # repo but unused.
    t1 = asyncio.create_task(market_data_loop())   # live prices for the top bar / scanner
    t2 = asyncio.create_task(mtf_scanner_loop())   # scan, store candles, record + alert signals
    log.info("CryptoSignal backend started")
    yield
    for t in (t1, t2):
        t.cancel()


app = FastAPI(title="CryptoSignal API", version="3.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(market.router)
app.include_router(settings.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version, "build": "2026-signals-only-mtf"}
