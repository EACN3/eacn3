"""Push notification service: best-effort delivery to agents/initiators.

Design:
- Best-effort delivery (no guarantee)
- Limited retries (non-blocking)
- Does not block main task flow
- Typed events via PushEvent model

Push event types:
- TASK_BROADCAST: new task → matching agents
- BID_REQUEST_CONFIRMATION: over-budget bid → initiator
- BID_RESULT: accept/reject → executor
- DISCUSSION_UPDATE: discussion change → all bidders
- SUBTASK_COMPLETED: child done → parent executors
- TASK_COLLECTED: pending collection → initiator
- TASK_TIMEOUT: deadline passed → initiator + executors
- ADJUDICATION_TASK: new adjudication → candidate adjudicators
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from eacn.core.models import Task, PushEvent, PushEventType

_logger = logging.getLogger(__name__)

# Type for push delivery callback
PushHandler = Callable[[PushEvent], Awaitable[None]]


class PushService:
    """Pushes events to agents. Supports pluggable delivery handler."""

    def __init__(self, handler: PushHandler | None = None, config: "PushConfig | None" = None) -> None:
        from eacn.network.config import PushConfig
        cfg = config or PushConfig()
        self.MAX_RETRIES: int = cfg.max_retries
        self._handler = handler
        self._history: list[PushEvent] = []
        self._max_history: int = 1000  # Prevent unbounded memory growth

    def set_handler(self, handler: PushHandler) -> None:
        self._handler = handler

    # ── Event constructors ───────────────────────────────────────────

    async def broadcast_task(
        self, task: Task, agent_ids: list[str]
    ) -> PushEvent:
        """Push new task to candidate agents for bidding."""
        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id=task.id,
            recipients=agent_ids,
            payload={
                "content": task.content,
                "domains": task.domains,
                "budget": task.budget,
                "deadline": task.deadline,
                "max_concurrent_bidders": task.max_concurrent_bidders,
            },
        )
        await self._deliver(event)
        return event

    async def request_budget_confirmation(
        self,
        task_id: str,
        initiator_id: str,
        agent_id: str,
        price: float,
        excess: float,
    ) -> PushEvent:
        """Request initiator to approve an over-budget bid."""
        event = PushEvent(
            type=PushEventType.BID_REQUEST_CONFIRMATION,
            task_id=task_id,
            recipients=[initiator_id],
            payload={
                "agent_id": agent_id,
                "price": price,
                "excess_amount": excess,
            },
        )
        await self._deliver(event)
        return event

    async def notify_bid_result(
        self,
        task_id: str,
        agent_id: str,
        accepted: bool,
        reason: str = "",
    ) -> PushEvent:
        """Notify executor of bid acceptance or rejection."""
        event = PushEvent(
            type=PushEventType.BID_RESULT,
            task_id=task_id,
            recipients=[agent_id],
            payload={"accepted": accepted, "reason": reason},
        )
        await self._deliver(event)
        return event

    async def notify_discussion_update(
        self, task: Task
    ) -> PushEvent:
        """Notify all bidders of discussion update."""
        bidder_ids = [
            b.agent_id for b in task.bids
            if b.status.value in ("executing", "waiting", "accepted", "pending")
        ]
        if not bidder_ids:
            bidder_ids = [task.initiator_id]

        event = PushEvent(
            type=PushEventType.DISCUSSION_UPDATE,
            task_id=task.id,
            recipients=bidder_ids,
            payload={"discussions": task.content.get("discussions", [])},
        )
        await self._deliver(event)
        return event

    async def notify_subtask_completed(
        self,
        parent_task: Task,
        subtask_id: str,
    ) -> PushEvent:
        """Notify parent's executors that a subtask completed."""
        executor_ids = parent_task.executing_agents
        if not executor_ids:
            executor_ids = [parent_task.initiator_id]

        event = PushEvent(
            type=PushEventType.SUBTASK_COMPLETED,
            task_id=parent_task.id,
            recipients=executor_ids,
            payload={"subtask_id": subtask_id},
        )
        await self._deliver(event)
        return event

    async def notify_task_collected(
        self, task: Task
    ) -> PushEvent:
        """Notify initiator that task entered AWAITING_RETRIEVAL."""
        event = PushEvent(
            type=PushEventType.TASK_COLLECTED,
            task_id=task.id,
            recipients=[task.initiator_id],
            payload={"status": task.status.value},
        )
        await self._deliver(event)
        return event

    async def notify_timeout(
        self, task: Task
    ) -> PushEvent:
        """Notify both initiator and executors of deadline timeout."""
        recipients = list(set([task.initiator_id] + task.executing_agents))
        event = PushEvent(
            type=PushEventType.TASK_TIMEOUT,
            task_id=task.id,
            recipients=recipients,
            payload={"deadline": task.deadline},
        )
        await self._deliver(event)
        return event

    async def notify_adjudication_task(
        self, adj_task: Task, agent_ids: list[str]
    ) -> PushEvent:
        """Push adjudication task to candidate adjudicators."""
        event = PushEvent(
            type=PushEventType.ADJUDICATION_TASK,
            task_id=adj_task.id,
            recipients=agent_ids,
            payload={
                "content": adj_task.content,
                "domains": adj_task.domains,
            },
        )
        await self._deliver(event)
        return event

    # ── Delivery ─────────────────────────────────────────────────────

    async def _deliver(self, event: PushEvent) -> None:
        """Best-effort delivery with limited retries."""
        self._history.append(event)
        # Trim history to prevent unbounded memory growth
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        if not self._handler:
            return

        import asyncio
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await self._handler(event)
                return
            except asyncio.CancelledError:
                raise  # Don't swallow cancellation (#83)
            except Exception:
                _logger.warning(
                    "Push delivery attempt %d/%d failed for %s",
                    attempt, self.MAX_RETRIES, event.type.value,
                    exc_info=True,
                )

    # ── Query ────────────────────────────────────────────────────────

    def get_history(self, task_id: str | None = None) -> list[PushEvent]:
        if task_id is None:
            return list(self._history)
        return [e for e in self._history if e.task_id == task_id]
