"""Cluster Router: decides whether to handle locally or forward to owner node.

Maintains task_id -> origin_node routing table.
Server-facing API calls hit this router to determine if the task is local.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class ClusterRouter:
    """Routes task operations to the correct owner node."""

    def __init__(self, db: "Database", local_node_id: str) -> None:
        self._db = db
        self._local_node_id = local_node_id
        self._routes: dict[str, str] = {}
        self._participants: dict[str, set[str]] = {}
        self._endpoints: dict[str, str] = {}

    def set_endpoint(self, node_id: str, endpoint: str) -> None:
        self._endpoints[node_id] = endpoint

    def get_endpoint(self, node_id: str) -> str | None:
        return self._endpoints.get(node_id)

    # ── Route management ─────────────────────────────────────────────

    def is_local(self, task_id: str) -> bool:
        origin = self._routes.get(task_id)
        return origin is None or origin == self._local_node_id

    def set_route(self, task_id: str, origin_node: str) -> None:
        self._routes[task_id] = origin_node

    def get_route(self, task_id: str) -> str | None:
        return self._routes.get(task_id)

    def remove_route(self, task_id: str) -> None:
        self._routes.pop(task_id, None)

    # ── Participant tracking ─────────────────────────────────────────

    def add_participant(self, task_id: str, node_id: str) -> None:
        self._participants.setdefault(task_id, set()).add(node_id)

    def get_participants(self, task_id: str) -> set[str]:
        return self._participants.get(task_id, set())

    def remove_participants(self, task_id: str) -> None:
        self._participants.pop(task_id, None)

    # ── Forwarding ───────────────────────────────────────────────────

    def _resolve(self, task_id: str) -> str:
        """Resolve task_id → peer endpoint URL. Raises ValueError on failure."""
        origin = self._routes.get(task_id)
        if not origin:
            raise ValueError(f"No route for task {task_id}")
        endpoint = self._endpoints.get(origin)
        if not endpoint:
            raise ValueError(f"No endpoint for node {origin}")
        return endpoint

    async def _post(self, url: str, body: dict, timeout: float = 10.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()

    async def forward_bid(self, task_id: str, agent_id: str,
                          server_id: str | None, confidence: float,
                          price: float) -> dict[str, Any]:
        ep = self._resolve(task_id)
        return await self._post(f"{ep}/peer/task/bid", {
            "task_id": task_id, "agent_id": agent_id,
            "server_id": server_id or "", "confidence": confidence,
            "price": price, "from_node": self._local_node_id,
        })

    async def forward_result(self, task_id: str, agent_id: str,
                             content: Any) -> dict[str, Any]:
        ep = self._resolve(task_id)
        return await self._post(f"{ep}/peer/task/result", {
            "task_id": task_id, "agent_id": agent_id,
            "content": content, "from_node": self._local_node_id,
        })

    async def forward_reject(self, task_id: str,
                             agent_id: str) -> dict[str, Any]:
        ep = self._resolve(task_id)
        return await self._post(f"{ep}/peer/task/reject", {
            "task_id": task_id, "agent_id": agent_id,
            "from_node": self._local_node_id,
        })

    async def forward_subtask(self, parent_task_id: str,
                              subtask_data: dict[str, Any]) -> dict[str, Any]:
        ep = self._resolve(parent_task_id)
        return await self._post(f"{ep}/peer/task/subtask", {
            "parent_task_id": parent_task_id,
            "subtask_data": subtask_data,
            "from_node": self._local_node_id,
        })

    async def _broadcast_to_nodes(self, nodes: set[str], path: str,
                                  body: dict, timeout: float = 5.0) -> None:
        """POST to multiple nodes, skip self and missing endpoints, swallow errors."""
        for node_id in nodes:
            if node_id == self._local_node_id:
                continue
            endpoint = self._endpoints.get(node_id)
            if not endpoint:
                continue
            try:
                await self._post(f"{endpoint}{path}", body, timeout)
            except Exception:
                _log.warning("Failed to reach node %s at %s", node_id, path)

    async def notify_status(self, task_id: str, status: str,
                            participant_nodes: set[str],
                            payload: dict[str, Any] | None = None) -> None:
        await self._broadcast_to_nodes(participant_nodes, "/peer/task/status", {
            "task_id": task_id, "status": status, "payload": payload or {},
        })

    async def forward_push(self, event_type: str, task_id: str,
                           recipients: list[str], payload: dict[str, Any],
                           target_nodes: set[str]) -> None:
        await self._broadcast_to_nodes(target_nodes, "/peer/push", {
            "type": event_type, "task_id": task_id,
            "recipients": recipients, "payload": payload,
        })
