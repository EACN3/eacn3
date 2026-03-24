"""Offline message store: caches push events for disconnected agents.

When a push event cannot be delivered via WebSocket (agent offline),
the message is persisted to SQLite. On reconnect, pending messages
are drained and delivered in order.

Features:
- Per-agent message cap (oldest evicted when exceeded)
- TTL-based expiration
- Atomic drain (fetch + delete in one operation)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class OfflineStore:
    """Manages offline message persistence for disconnected agents."""

    def __init__(
        self,
        db: "Database",
        max_per_agent: int = 200,
        ttl_seconds: int = 86400,
    ) -> None:
        self._db = db
        self.max_per_agent = max_per_agent
        self.ttl_seconds = ttl_seconds

    def _expires_at(self) -> str:
        """Compute expiration timestamp."""
        return (
            datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")

    async def store(
        self,
        msg_id: str,
        agent_id: str,
        event_type: str,
        task_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Store a message for an offline agent.

        Automatically prunes overflow beyond max_per_agent.
        """
        expires_at = self._expires_at()
        await self._db.offline_store(
            msg_id=msg_id,
            agent_id=agent_id,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            expires_at=expires_at,
        )
        # Prune overflow
        pruned = await self._db.offline_prune_overflow(agent_id, self.max_per_agent)
        if pruned:
            _log.info(
                "Pruned %d oldest offline messages for agent %s (cap=%d)",
                pruned, agent_id, self.max_per_agent,
            )

    async def drain(self, agent_id: str) -> list[dict[str, Any]]:
        """Retrieve and delete all pending messages for an agent.

        Returns messages oldest-first. Expired messages are auto-pruned.
        """
        messages = await self._db.offline_drain(agent_id)
        if messages:
            _log.info(
                "Drained %d offline messages for agent %s",
                len(messages), agent_id,
            )
        return messages

    async def count(self, agent_id: str) -> int:
        """Count pending messages for an agent."""
        return await self._db.offline_count(agent_id)

    async def count_all(self) -> dict[str, int]:
        """Count pending messages grouped by agent."""
        return await self._db.offline_count_all()
