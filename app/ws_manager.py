from typing import Dict, List

from fastapi import WebSocket

from .config import log


class ConnectionManager:
    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}  # user_id -> [ws]

    async def connect(self, ws: WebSocket, user_id: str):
        await ws.accept()
        self.connections.setdefault(user_id, []).append(ws)
        log.info("WS connected: user=%s total=%d", user_id, len(self.connections[user_id]))

    def disconnect(self, ws: WebSocket, user_id: str):
        if user_id in self.connections:
            self.connections[user_id] = [c for c in self.connections[user_id] if c != ws]
            if not self.connections[user_id]:
                del self.connections[user_id]
        log.info("WS disconnected: user=%s", user_id)

    async def send_to_user(self, user_id: str, data: dict):
        for ws in list(self.connections.get(user_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def broadcast(self, data: dict):
        for user_id in list(self.connections.keys()):
            await self.send_to_user(user_id, data)

    @property
    def connected_users(self):
        return list(self.connections.keys())


manager = ConnectionManager()
