"""WebSocket push notification delivery.

Agents connect via WS and register their agent_id.
Push events are routed to connected agents in real-time.
Disconnected agents miss events (best-effort).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from eacn.core.models import PushEvent

_log = logging.getLogger(__name__)

ws_router = APIRouter()


class ConnectionManager:
    """Manages WebSocket connections indexed by agent_id."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, agent_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            # Disconnect existing connection for same agent
            old = self._connections.get(agent_id)
            if old:
                try:
                    await old.close()
                except Exception:
                    pass
            self._connections[agent_id] = ws
        _log.info("Agent %s connected via WebSocket", agent_id)

    async def disconnect(self, agent_id: str) -> None:
        async with self._lock:
            self._connections.pop(agent_id, None)
        _log.info("Agent %s disconnected", agent_id)

    async def send_to(self, agent_id: str, data: dict[str, Any]) -> bool:
        """Send JSON data to a connected agent. Returns True if delivered."""
        async with self._lock:
            ws = self._connections.get(agent_id)
        if not ws:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            await self.disconnect(agent_id)
            return False

    async def broadcast_event(self, event: PushEvent) -> int:
        """Deliver a push event to all its recipients. Returns count delivered."""
        payload = {
            "type": event.type.value,
            "task_id": event.task_id,
            "payload": event.payload,
        }
        delivered = 0
        for recipient in event.recipients:
            if await self.send_to(recipient, payload):
                delivered += 1
        return delivered

    @property
    def connected_count(self) -> int:
        return len(self._connections)

    def is_connected(self, agent_id: str) -> bool:
        return agent_id in self._connections


# Singleton manager
manager = ConnectionManager()


@ws_router.websocket("/ws/{agent_id}")
async def websocket_endpoint(ws: WebSocket, agent_id: str):
    """Agent connects, receives push events.

    Agents can also send messages (e.g., heartbeat/ack), but we currently
    just keep the connection alive and deliver events.
    """
    await manager.connect(agent_id, ws)
    try:
        while True:
            # Keep alive — read pings/messages from agent
            data = await ws.receive_text()
            # Could handle ack/heartbeat here in the future
            if data == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        await manager.disconnect(agent_id)
    except Exception:
        await manager.disconnect(agent_id)
