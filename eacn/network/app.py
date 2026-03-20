"""Network application: the full orchestration layer.

This is the central coordinator that ties together all network-side modules.
All task lifecycle operations flow through here.

Architecture:
- Stateless logic (horizontally scalable)
- Task data in persistent storage (TaskManager)
- DHT for distributed discovery
- Gossip for self-healing agent propagation
"""

from __future__ import annotations

import logging
from typing import Any

from eacn.core.models import (
    Task, TaskStatus, TaskType, Bid, BidStatus, Result,
    AgentCard, LogEntry, PushEventType,
)
from eacn.core.exceptions import TaskError, BudgetError
from eacn.network.task_manager import TaskManager
from eacn.network.push import PushService
from eacn.network.adjudication import AdjudicationService
from eacn.network.discovery import DiscoveryService
from eacn.network.matcher import GlobalMatcher
from eacn.network.logger import GlobalLogger, log_event
from eacn.network.reputation import GlobalReputation
from eacn.network.economy import EscrowService
from eacn.network.economy.settlement import SettlementService
from eacn.network.cluster.service import ClusterService

_log = logging.getLogger(__name__)


class Network:
    """EACN network node: stateless orchestration, DHT-redundant, gossip self-healing.

    Public API methods (all async):
    - create_task: publish a new task
    - submit_bid: agent bids on a task
    - submit_result: agent submits execution result
    - select_result: initiator picks winning result
    - close_task: initiator manually closes task
    - collect_results: initiator retrieves results (first call → COMPLETED)
    - create_subtask: executor delegates part of work
    - update_deadline: change task deadline
    - update_discussions: append discussion message
    - confirm_budget: initiator approves/rejects over-budget bid
    - scan_deadlines: periodic deadline expiration check
    """

    def __init__(
        self,
        db: "Database | None" = None,
        config: "NetworkConfig | None" = None,
    ) -> None:
        from eacn.network.config import NetworkConfig
        from eacn.network.db.database import Database

        self.config = config or NetworkConfig()
        self.db = db or Database()

        # Core modules — config + db injected
        self.discovery = DiscoveryService(self.db)
        self.dht = self.discovery.dht
        self.gossip = self.discovery.gossip
        self.bootstrap = self.discovery.bootstrap
        self.task_manager = TaskManager()
        self.push = PushService(config=self.config.push)
        self.adjudication = AdjudicationService()
        self.matcher = GlobalMatcher(config=self.config.matcher)
        self.logger = GlobalLogger()
        self.reputation = GlobalReputation(config=self.config.reputation)
        self.escrow = EscrowService()
        self.settlement = SettlementService(
            self.escrow,
            platform_fee_rate=self.config.economy.platform_fee_rate,
        )

        # Cluster layer (standalone when no seed nodes configured)
        from eacn.network.cluster.config import ClusterConfig as CConfig
        cluster_cfg = CConfig(**self.config.cluster.model_dump())
        self.cluster = ClusterService(self.db, config=cluster_cfg)

    async def start(self) -> None:
        """Bootstrap the network node."""
        _log.info("EACN Network starting...")
        await self.cluster.start()

    # ══════════════════════════════════════════════════════════════════
    # Task creation
    # ══════════════════════════════════════════════════════════════════

    async def create_task(
        self,
        task_id: str,
        initiator_id: str,
        content: dict[str, Any],
        domains: list[str],
        budget: float,
        deadline: str | None = None,
        max_concurrent_bidders: int | None = None,
        max_depth: int | None = None,
    ) -> Task:
        """Publish a new task to the network.

        Flow:
        1. Freeze initiator's budget → escrow
        2. Create task in TaskManager
        3. Discover candidate agents via DHT
        4. Match and rank candidates
        5. Push task broadcast to candidates
        """
        # 1. Freeze budget
        self.escrow.freeze_budget(initiator_id, task_id, budget)

        # 2. Create task
        task = Task(
            id=task_id,
            content=content,
            initiator_id=initiator_id,
            domains=domains,
            budget=budget,
            deadline=deadline,
            max_concurrent_bidders=max_concurrent_bidders or self.config.task.default_max_concurrent_bidders,
            max_depth=max_depth or self.config.task.default_max_depth,
        )
        task = self.task_manager.create(task)

        # 3. Log
        self._log_event("create_task", task_id=task_id, agent_id=initiator_id)

        # 4. Discover + match + push
        await self._broadcast_to_candidates(task)

        # 5. Cluster: broadcast to peer nodes
        await self.cluster.broadcast_task({
            "task_id": task.id,
            "initiator_id": initiator_id,
            "domains": domains,
            "type": task.type.value,
            "budget": budget,
            "deadline": deadline,
            "content": content,
            "max_concurrent_bidders": task.max_concurrent_bidders,
        })

        return task

    # ══════════════════════════════════════════════════════════════════
    # Bidding
    # ══════════════════════════════════════════════════════════════════

    async def submit_bid(
        self,
        task_id: str,
        agent_id: str,
        confidence: float,
        price: float,
        server_id: str | None = None,
    ) -> BidStatus:
        """Agent submits a bid on a task.

        Flow:
        1. Check for duplicate bid
        2. Validate via Matcher (ability gate + price gate)
        3. If price exceeds tolerance:
           a. If concurrent slots not full → request budget confirmation
           b. If concurrent slots full → reject (budget locked)
        4. Add bid to TaskManager (EXECUTING or WAITING)
        5. Push bid result to agent
        """
        task = self.task_manager.get(task_id)

        # 0. Duplicate bid check
        if any(b.agent_id == agent_id for b in task.bids):
            raise TaskError(f"Agent {agent_id} already bid on task {task_id}")

        # Fetch reputation data
        scores = self.reputation.get_scores([agent_id])
        negotiation_gain = self.reputation.negotiation_gain(agent_id)
        is_adjudication = task.type == TaskType.ADJUDICATION

        # 1. Validate
        check = self.matcher.check_bid(
            agent_id=agent_id,
            confidence=confidence,
            price=price,
            budget=task.budget,
            scores=scores,
            negotiation_gain=negotiation_gain,
            is_adjudication=is_adjudication,
        )

        if not check.passed:
            if check.needs_budget_confirmation:
                if task.budget_locked:
                    # Concurrent slots full → reject directly
                    # Still create Bid record with REJECTED status per doc
                    bid = Bid(
                        agent_id=agent_id, confidence=confidence,
                        price=price, status=BidStatus.REJECTED,
                    )
                    task.bids.append(bid)
                    await self.push.notify_bid_result(
                        task_id, agent_id, accepted=False,
                        reason="Budget locked (concurrent limit reached)",
                    )
                    self._log_event(
                        "submit_bid_rejected", task_id=task_id,
                        agent_id=agent_id,
                        extra={"reason": "budget_locked"},
                    )
                    return BidStatus.REJECTED
                else:
                    # Request budget confirmation from initiator
                    await self.push.request_budget_confirmation(
                        task_id=task_id,
                        initiator_id=task.initiator_id,
                        agent_id=agent_id,
                        price=price,
                        excess=check.excess_amount,
                    )
                    self._log_event(
                        "submit_bid_pending_confirmation", task_id=task_id,
                        agent_id=agent_id,
                    )
                    # Add bid as PENDING (awaiting confirmation)
                    bid = Bid(
                        agent_id=agent_id,
                        confidence=confidence,
                        price=price,
                        status=BidStatus.PENDING,
                    )
                    task.bids.append(bid)
                    return BidStatus.PENDING
            else:
                # Ability check failed → reject, still create Bid record per doc
                bid = Bid(
                    agent_id=agent_id, confidence=confidence,
                    price=price, status=BidStatus.REJECTED,
                )
                task.bids.append(bid)
                await self.push.notify_bid_result(
                    task_id, agent_id, accepted=False, reason=check.reason,
                )
                self._log_event(
                    "submit_bid_rejected", task_id=task_id,
                    agent_id=agent_id,
                    extra={"reason": check.reason},
                )
                return BidStatus.REJECTED

        # 2. Passed validation → add bid
        bid = Bid(agent_id=agent_id, confidence=confidence, price=price)
        bid_status = self.task_manager.add_bid(task_id, bid)

        # 3. Push result
        await self.push.notify_bid_result(
            task_id, agent_id, accepted=True,
        )

        # 4. Log
        self._log_event("submit_bid", task_id=task_id, agent_id=agent_id)

        # 5. Gossip: exchange known agents between bidder and initiator
        await self.gossip.exchange(agent_id, task.initiator_id)

        return bid_status

    # ══════════════════════════════════════════════════════════════════
    # Task rejection (agent withdraws)
    # ══════════════════════════════════════════════════════════════════

    async def reject_task(
        self,
        task_id: str,
        agent_id: str,
        reason: str = "",
    ) -> None:
        """Agent rejects/withdraws from an assigned task.

        Flow:
        1. Mark bid as REJECTED in TaskManager
        2. Promote next from wait queue
        3. Push rejection notification
        """
        task = self.task_manager.get(task_id)

        # Find and reject the agent's bid
        self.task_manager.reject_bid(task_id, agent_id)

        # Log
        self._log_event(
            "reject_task", task_id=task_id, agent_id=agent_id,
            extra={"reason": reason},
        )

        # Promote next from wait queue
        promoted = self.task_manager.promote_from_queue(task_id)
        if promoted:
            await self.push.notify_bid_result(
                task_id, promoted, accepted=True,
                reason="Promoted from wait queue",
            )

    # ══════════════════════════════════════════════════════════════════
    # Result submission
    # ══════════════════════════════════════════════════════════════════

    async def submit_result(
        self,
        task_id: str,
        agent_id: str,
        content: Any,
    ) -> None:
        """Agent submits execution result.

        Flow:
        1. Validate caller is in bidders
        2. Add result to TaskManager
        3. If adjudication task → auto-collect to parent result
        4. If normal task → trigger adjudication
        5. Check auto-collection
        6. Promote next from wait queue
        """
        task = self.task_manager.get(task_id)

        # 1. Validate caller is in bidders (has an active bid)
        bidder_ids = [
            b.agent_id for b in task.bids
            if b.status in (BidStatus.EXECUTING, BidStatus.ACCEPTED, BidStatus.WAITING)
        ]
        if agent_id not in bidder_ids:
            raise TaskError(
                f"Agent {agent_id} is not an active bidder on task {task_id}"
            )

        result = Result(agent_id=agent_id, content=content)

        # 2. Add result
        self.task_manager.add_result(task_id, result)

        # 3. Log
        self._log_event("submit_result", task_id=task_id, agent_id=agent_id)

        # 4. Adjudication auto-collection: if this is an adjudication task,
        #    collect result directly into parent task's result adjudications
        if task.type == TaskType.ADJUDICATION and task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                target = task.content.get("target_result_agent_id", "")
                # Extract verdict/score from content
                if isinstance(content, dict):
                    verdict = content.get("verdict", str(content))
                    score = float(content.get("score", 1.0))
                else:
                    verdict = str(content)
                    score = 1.0
                self.adjudication.collect_adjudication_result(
                    parent_task=parent,
                    target_result_agent_id=target,
                    adjudicator_id=agent_id,
                    verdict=verdict,
                    score=score,
                )
            except TaskError:
                pass  # parent not found, skip

        # 5. Trigger adjudication for normal tasks (non-blocking side path)
        if self.adjudication.should_create_adjudication(task):
            await self._create_adjudication(task, agent_id)

        # 6. Check auto-collection
        if self.task_manager.check_auto_collect(task_id):
            await self.push.notify_task_collected(task)

        # 7. Promote next from wait queue
        promoted = self.task_manager.promote_from_queue(task_id)
        if promoted:
            await self.push.notify_bid_result(
                task_id, promoted, accepted=True,
                reason="Promoted from wait queue",
            )

        # 8. If this is a subtask, notify parent executors
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                await self.push.notify_subtask_completed(parent, task_id)
            except TaskError:
                pass  # parent not found, ignore

    # ══════════════════════════════════════════════════════════════════
    # Result selection (settlement)
    # ══════════════════════════════════════════════════════════════════

    async def select_result(
        self,
        task_id: str,
        agent_id: str,
        initiator_id: str,
    ) -> None:
        """Initiator selects a winning result.

        Preconditions:
        - Caller must be the task initiator
        - Task status must be AWAITING_RETRIEVAL or COMPLETED

        Flow:
        1. Mark result as selected in TaskManager
        2. Settle payment (escrow → executor + platform fee + refund)
        3. Propagate reputation (PageRank)
        """
        task = self.task_manager.get(task_id)

        # Auth: only initiator can select
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can select a result")

        # State: must be AWAITING_RETRIEVAL or COMPLETED
        if task.status not in (TaskStatus.AWAITING_RETRIEVAL, TaskStatus.COMPLETED):
            raise TaskError(
                f"Cannot select result in status {task.status.value}; "
                "task must be in awaiting_retrieval or completed"
            )

        # 1. Select result
        selected = self.task_manager.select_result(task_id, agent_id)

        # 2. Find the bid price
        bid_price = 0.0
        for bid in task.bids:
            if bid.agent_id == agent_id:
                bid_price = bid.price
                break

        # 3. Settle (skip for adjudication tasks which have zero budget)
        if task.type != TaskType.ADJUDICATION and bid_price > 0:
            self.settlement.settle(task_id, agent_id, bid_price)

        # 4. Reputation propagation
        self.reputation.propagate_selection(task.initiator_id, agent_id)

        # 5. Cluster: gossip exchange with participating nodes
        await self.cluster.trigger_gossip(task_id)

        # 6. Log
        self._log_event("select_result", task_id=task_id, agent_id=agent_id)

    # ══════════════════════════════════════════════════════════════════
    # Task control
    # ══════════════════════════════════════════════════════════════════

    async def close_task(self, task_id: str, initiator_id: str) -> Task:
        """Initiator manually closes a task.

        Preconditions:
        - Caller must be the task initiator
        """
        task = self.task_manager.get(task_id)

        # Auth: only initiator can close
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can close this task")

        task = self.task_manager.close_task(task_id)
        self._log_event("close_task", task_id=task_id, agent_id=initiator_id)

        if task.status == TaskStatus.AWAITING_RETRIEVAL:
            await self.push.notify_task_collected(task)
        elif task.status == TaskStatus.NO_ONE_ABLE:
            self.settlement.refund_no_one_capable(task_id)

        return task

    async def collect_results(self, task_id: str) -> list[Result]:
        """Initiator retrieves results. First call → COMPLETED."""
        results = self.task_manager.collect_results(task_id)
        self._log_event("collect_results", task_id=task_id)
        return results

    async def update_deadline(
        self, task_id: str, deadline: str, initiator_id: str,
    ) -> Task:
        """Update task deadline.

        Preconditions:
        - Caller must be the task initiator
        """
        task = self.task_manager.get(task_id)

        # Auth: only initiator can update deadline
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can update the deadline")

        task = self.task_manager.update_deadline(task_id, deadline)
        self._log_event("update_deadline", task_id=task_id)
        return task

    async def update_discussions(
        self, task_id: str, message: str, initiator_id: str,
    ) -> Task:
        """Append discussion and push to all bidders.

        Preconditions:
        - Caller must be the task initiator
        - Task status must be BIDDING
        """
        task = self.task_manager.get(task_id)

        # Auth: only initiator can update discussions
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can update discussions")

        # State: must be BIDDING
        if task.status != TaskStatus.BIDDING:
            raise TaskError(
                f"Cannot update discussions in status {task.status.value}; "
                "task must be in bidding"
            )

        task = self.task_manager.update_discussions(task_id, message)
        self._log_event("update_discussions", task_id=task_id)
        await self.push.notify_discussion_update(task)
        return task

    async def confirm_budget(
        self,
        task_id: str,
        initiator_id: str,
        approved: bool,
        new_budget: float | None = None,
    ) -> None:
        """Initiator responds to over-budget bid confirmation request.

        - approved=True + new_budget → update budget and re-evaluate pending bids
        - approved=False → reject all pending bids
        """
        task = self.task_manager.get(task_id)

        # Auth: only initiator
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can confirm budget")

        self._log_event(
            "confirm_budget", task_id=task_id, agent_id=initiator_id,
        )

        if not approved:
            # Reject all pending bids
            for bid in task.bids:
                if bid.status == BidStatus.PENDING:
                    bid.status = BidStatus.REJECTED
                    await self.push.notify_bid_result(
                        task_id, bid.agent_id, accepted=False,
                        reason="Budget not approved by initiator",
                    )
            return

        # Approved: update budget if new_budget provided
        if new_budget is not None and new_budget > task.budget:
            additional = new_budget - task.budget
            self.escrow.confirm_budget_increase(initiator_id, task_id, additional)
            task.budget = new_budget
            if task.remaining_budget is not None:
                task.remaining_budget += additional

        # Re-evaluate pending bids
        for bid in task.bids:
            if bid.status == BidStatus.PENDING:
                # Re-check with new budget
                scores = self.reputation.get_scores([bid.agent_id])
                neg_gain = self.reputation.negotiation_gain(bid.agent_id)
                check = self.matcher.check_bid(
                    agent_id=bid.agent_id,
                    confidence=bid.confidence,
                    price=bid.price,
                    budget=task.budget,
                    scores=scores,
                    negotiation_gain=neg_gain,
                    is_adjudication=task.type == TaskType.ADJUDICATION,
                )
                if check.passed:
                    if not task.concurrent_slots_full:
                        bid.status = BidStatus.EXECUTING
                    else:
                        bid.status = BidStatus.WAITING
                    await self.push.notify_bid_result(
                        task_id, bid.agent_id, accepted=True,
                        reason="Budget confirmed",
                    )

    # ══════════════════════════════════════════════════════════════════
    # Subtask delegation
    # ══════════════════════════════════════════════════════════════════

    async def create_subtask(
        self,
        parent_task_id: str,
        initiator_id: str,
        content: dict[str, Any],
        domains: list[str],
        budget: float,
        deadline: str | None = None,
    ) -> Task:
        """Executor creates a subtask, allocating from parent's budget.

        Preconditions:
        - Caller must be in parent task's bidders

        Flow:
        1. Validate caller is a bidder on parent
        2. Allocate budget from parent escrow
        3. Create subtask in TaskManager
        4. Discover + broadcast to candidates
        """
        parent = self.task_manager.get(parent_task_id)

        # Auth: caller must be in parent task's bidders
        bidder_ids = [b.agent_id for b in parent.bids]
        if initiator_id not in bidder_ids:
            raise TaskError(
                f"Agent {initiator_id} is not a bidder on parent task {parent_task_id}"
            )

        # 1. Create subtask
        subtask = self.task_manager.create_subtask(
            parent_task_id=parent_task_id,
            content=content,
            domains=domains,
            budget=budget,
            initiator_id=initiator_id,
            deadline=deadline,
        )

        # Transfer escrow
        self.escrow.allocate_subtask_budget(
            parent_task_id, subtask.id, initiator_id, budget,
        )

        # 2. Log
        self._log_event(
            "create_subtask", task_id=subtask.id, agent_id=initiator_id,
        )

        # 3. Discover + push
        await self._broadcast_to_candidates(subtask)

        return subtask

    # ══════════════════════════════════════════════════════════════════
    # Deadline management
    # ══════════════════════════════════════════════════════════════════

    async def scan_deadlines(self, now: str | None = None) -> list[str]:
        """Periodic scan for expired tasks.

        Returns list of task IDs that were expired.
        For each:
        - Has results → AWAITING_RETRIEVAL + notify
        - No results → NO_ONE_ABLE + refund + notify
        """
        expired = self.task_manager.scan_expired(now)
        expired_ids = []

        for task in expired:
            new_status = self.task_manager.handle_expired(task.id)
            expired_ids.append(task.id)

            # Push timeout notification
            await self.push.notify_timeout(task)

            if new_status == TaskStatus.AWAITING_RETRIEVAL:
                await self.push.notify_task_collected(task)
            elif new_status == TaskStatus.NO_ONE_ABLE:
                self.settlement.refund_no_one_capable(task.id)

            self._log_event("task_timeout", task_id=task.id)

        return expired_ids

    # ══════════════════════════════════════════════════════════════════
    # Reputation events (from servers)
    # ══════════════════════════════════════════════════════════════════

    def receive_reputation_event(
        self,
        agent_id: str,
        event_type: str,
        server_id: str,
    ) -> float:
        """Receive a reputation event from a server.

        Only raw events are accepted (not scores).
        Returns the updated reputation score.
        """
        return self.reputation.aggregate(
            agent_id,
            [{"type": event_type}],
            server_id=server_id,
        )

    # ══════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════

    async def _broadcast_to_candidates(self, task: Task) -> None:
        """Discover agents, match, and push task broadcast."""
        # Discover by domain
        all_agent_ids: set[str] = set()
        for domain in task.domains:
            ids = await self.discovery.discover(domain)
            all_agent_ids.update(ids)

        if not all_agent_ids:
            return

        # We'd normally fetch AgentCards here; for now push to all discovered
        await self.push.broadcast_task(task, list(all_agent_ids))

    async def _create_adjudication(
        self, parent_task: Task, result_agent_id: str
    ) -> None:
        """Create and broadcast an adjudication task (non-blocking)."""
        adj_task = self.adjudication.create_adjudication_task(
            parent_task, result_agent_id,
        )
        # Register in TaskManager (no escrow needed, budget=0)
        self.task_manager.create(adj_task)

        # Discover adjudicators and push
        all_agent_ids: set[str] = set()
        for domain in adj_task.domains:
            ids = await self.discovery.discover(domain)
            all_agent_ids.update(ids)

        # Exclude the result agent from adjudicating their own work
        all_agent_ids.discard(result_agent_id)

        if all_agent_ids:
            await self.push.notify_adjudication_task(
                adj_task, list(all_agent_ids),
            )

    def _log_event(
        self,
        fn_name: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        server_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Convenience method to record a log entry."""
        from datetime import datetime, timezone

        entry = LogEntry(
            fn_name=fn_name,
            args=extra or {},
            timestamp=datetime.now(timezone.utc).isoformat(),
            task_id=task_id,
            agent_id=agent_id,
            server_id=server_id,
        )
        self.logger.record(entry)
