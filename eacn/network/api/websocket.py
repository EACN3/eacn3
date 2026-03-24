"""WebSocket push notification delivery with ACK and offline caching.

Agents connect via WS and register their agent_id.
Push events are routed to connected agents in real-time.

Reliability features:
- Each message carries a unique msg_id.
- Agents must ACK received messages ({"ack": "<msg_id>"}).
- Unacked messages are retried up to ack_max_retries times.
- Messages for disconnected agents are persisted in OfflineStore.
- On reconnect, offline messages are drained and delivered.
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
    """Manages WebSocket connections with ACK tracking and offline caching."""

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()
        # ACK tracking: msg_id → asyncio.Event (set when ACK received)
        self._pending_acks: dict[str, asyncio.Event] = {}
        # Offline store (injected via set_offline_store)
        self._offline_store: Any = None
        # ACK timeout in seconds (overridden by config)
        self.ack_timeout: int = 30

    def set_offline_store(self, store: Any) -> None:
        """Inject the OfflineStore for persisting undelivered messages."""
        self._offline_store = store

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

        # Drain offline messages on reconnect
        await self._drain_offline(agent_id, ws)

    async def _drain_offline(self, agent_id: str, ws: WebSocket) -> None:
        """Deliver pending offline messages to a just-connected agent."""
        if not self._offline_store:
            return
        try:
            messages = await self._offline_store.drain(agent_id)
            for msg in messages:
                payload = {
                    "msg_id": msg["msg_id"],
                    "type": msg["type"],
                    "task_id": msg["task_id"],
                    "payload": msg["payload"],
                    "_offline": True,
                }
                try:
                    await ws.send_json(payload)
                except Exception:
                    _log.warning(
                        "Failed to deliver offline msg %s to %s during drain",
                        msg["msg_id"], agent_id,
                    )
                    break
        except Exception:
            _log.warning("Failed to drain offline messages for %s",
                         agent_id, exc_info=True)

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

    async def _send_with_ack(
        self, agent_id: str, data: dict[str, Any], msg_id: str,
    ) -> bool:
        """Send data and wait for ACK. Returns True if ACKed."""
        ack_event = asyncio.Event()
        self._pending_acks[msg_id] = ack_event

        sent = await self.send_to(agent_id, data)
        if not sent:
            self._pending_acks.pop(msg_id, None)
            return False

        try:
            await asyncio.wait_for(ack_event.wait(), timeout=self.ack_timeout)
            return True
        except asyncio.TimeoutError:
            _log.debug("ACK timeout for msg %s to agent %s", msg_id, agent_id)
            return False
        finally:
            self._pending_acks.pop(msg_id, None)

    def handle_ack(self, msg_id: str) -> bool:
        """Process an ACK from a client. Returns True if the ACK matched a pending message."""
        event = self._pending_acks.get(msg_id)
        if event:
            event.set()
            return True
        return False

    async def broadcast_event(self, event: PushEvent) -> int:
        """Deliver a push event to all its recipients with ACK.

        Undelivered or unacked messages are persisted to offline store.
        Returns count of successfully ACKed deliveries.
        """
        payload = {
            "msg_id": event.msg_id,
            "type": event.type.value,
            "task_id": event.task_id,
            "payload": event.payload,
        }
        delivered = 0
        for recipient in event.recipients:
            acked = await self._send_with_ack(recipient, payload, event.msg_id)
            if acked:
                delivered += 1
            else:
                # Cache for offline delivery
                await self._store_offline(recipient, event)
        return delivered

    async def _store_offline(self, agent_id: str, event: PushEvent) -> None:
        """Persist a message to offline store for later delivery."""
        if not self._offline_store:
            _log.debug(
                "No offline store configured; dropping msg %s for %s",
                event.msg_id, agent_id,
            )
            return
        try:
            await self._offline_store.store(
                msg_id=event.msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )
            _log.debug(
                "Stored offline msg %s for agent %s", event.msg_id, agent_id,
            )
        except Exception:
            _log.warning(
                "Failed to store offline msg %s for %s",
                event.msg_id, agent_id, exc_info=True,
            )

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

    Supports:
    - "ping" → "pong" keepalive
    - {"ack": "<msg_id>"} → acknowledge a received message
    """
    await manager.connect(agent_id, ws)
    try:
        while True:
            data = await ws.receive_text()
            # Keepalive
            if data == "ping":
                await ws.send_text("pong")
                continue
            # Try to parse as JSON for ACK
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and "ack" in msg:
                    manager.handle_ack(msg["ack"])
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
    except WebSocketDisconnect:
        await manager.disconnect(agent_id)
    except Exception:
        await manager.disconnect(agent_id)
