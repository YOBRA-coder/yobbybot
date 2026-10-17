import asyncio

import aiohttp
from fastapi import APIRouter, Depends, HTTPException

from ..database import get_settings
from ..security import get_current_user
from ..services import approvals, mtf
from .market import _check_symbol

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/verdicts")
async def verdicts(user=Depends(get_current_user)):
    s = get_settings()
    have = approvals.load_verdicts()
    rows = []
    for key in approvals.expected_keys(s):
        sym, style, tf = key
        eff = approvals.effective(sym, style, tf, s, have)
        own = have.get(key)
        rows.append({"symbol": sym, "style": style, "entry_tf": tf, **eff,
                     "own_verdict": own["verdict"] if own else "PENDING"})
    return {"rows": rows, "queue": approvals.status(),
            "rules": {"min_trades": s["bt_min_trades"], "min_pf": s["bt_min_pf"], "require_backtest": s["require_backtest"]}}


@router.get("/{symbol}")
async def run(symbol: str, style: str = "intraday", entry_tf: str | None = None, days: int = 60,
              min_agree: float | None = None, save: bool = False, user=Depends(get_current_user)):
    """Replay the engine (key levels + AMD + approved confirmations included) on history — no look-ahead,
    fees + slippage included. `save=true` also stores the approval verdict for that pair/strategy."""
    symbol = _check_symbol(symbol)
    if style not in mtf.STYLES:
        raise HTTPException(400, f"style must be one of: {', '.join(mtf.STYLES)}")
    settings = get_settings()
    entry_tf = entry_tf or mtf._entry_for(style, settings["entry_tf"])
    if entry_tf not in mtf.ENTRY_TFS:
        raise HTTPException(400, f"entry_tf must be one of: {', '.join(mtf.ENTRY_TFS)}")
    days = min(max(days, 10), 120)
    async with aiohttp.ClientSession() as s:
        try:
            res = await approvals.run_backtest(s, symbol, style, entry_tf, settings, days, min_agree, keep_all=True)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
    verdict, why = approvals.judge(res, settings)
    res["verdict"], res["why"] = verdict, why
    if save:
        approvals.save_verdict(symbol, style, entry_tf, verdict, why, res)
    return res


@router.post("/run-all")
async def run_all(user=Depends(get_current_user)):
    """Queue a fresh approval backtest for every watchlist pair × approved strategy (runs in the background)."""
    s = get_settings()
    keys = approvals.expected_keys(s)
    for k in keys:
        approvals.queue(*k)
    return {"queued": len(keys), **approvals.status()}
