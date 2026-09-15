import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..config import log
from ..database import get_db, serialize_bot
from ..security import decode_token
from ..ws_manager import manager

router = APIRouter(tags=["ws"])


@router.websocket("/ws/{token}")
async def websocket_endpoint(websocket: WebSocket, token: str):
    user_id = decode_token(token)
    if not user_id:
        await websocket.close(code=4001)
        return

    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if not user:
        await websocket.close(code=4001)
        return

    await manager.connect(websocket, user_id)
    try:
        conn = get_db()
        bots = [serialize_bot(r) for r in conn.execute("SELECT * FROM bots WHERE user_id=?", (user_id,)).fetchall()]
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user_id,)
        ).fetchall()]
        signals = [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user_id,)
        ).fetchall()]
        conn.close()
        await websocket.send_json({"type": "INIT", "data": {"bots": bots, "trades": trades, "signals": signals}})

        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
                if msg.get("type") == "PING":
                    await websocket.send_json({"type": "PONG"})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "PING"})
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception as e:
        log.error("WS error: %s", e)
        manager.disconnect(websocket, user_id)
