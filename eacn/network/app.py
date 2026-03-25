"""Network application: central coordinator for all task lifecycle operations."""

from __future__ import annotations

import logging
from typing import Any

from eacn.core.models import (
    Task, TaskStatus, TaskType, TaskLevel, Bid, BidStatus, Result, LogEntry, HumanContact,
)
from eacn.core.exceptions import TaskError, BudgetError
from eacn.network.task_manager import TaskManager
from eacn.network.push import PushService
from eacn.network.adjudication import AdjudicationService
from eacn.network.discovery import DiscoveryService
from eacn.network.matcher import GlobalMatcher
from eacn.network.logger import GlobalLogger
from eacn.network.reputation import GlobalReputation
from eacn.network.economy import EscrowService
from eacn.network.economy.settlement import SettlementService
from eacn.network.cluster.service import ClusterService

_log = logging.getLogger(__name__)


class Network:
    """EACN network node: stateless orchestration, DHT-redundant, gossip self-healing."""

    def __init__(
        self,
        db: "Database | None" = None,
        config: "NetworkConfig | None" = None,
    ) -> None:
        from eacn.network.config import load_config
        from eacn.network.db.database import Database

        self.config = config if config is not None else load_config()
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
        self.reputation = GlobalReputation(config=self.config.reputation, db=self.db)
        self.escrow = EscrowService(db=self.db)
        self.settlement = SettlementService(
            self.escrow,
            platform_fee_rate=self.config.economy.platform_fee_rate,
        )

        # Cluster layer (standalone when no seed nodes configured)
        cluster_cfg = self.config.cluster
        self.cluster = ClusterService(self.db, config=cluster_cfg)

    async def start(self) -> None:
        """Bootstrap the network node."""
        _log.info("EACN Network starting...")
        await self.escrow.load_from_db()
        await self.reputation.load_from_db()
        await self.cluster.start()

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
        human_contact: HumanContact | None = None,
        level: str | None = None,
        invited_agent_ids: list[str] | None = None,
    ) -> Task:
        """Publish a new task: freeze budget → create → discover → broadcast."""
        await self.escrow.freeze_budget(initiator_id, task_id, budget)
        try:
            task = Task(
                id=task_id,
                content=content,
                initiator_id=initiator_id,
                domains=domains,
                budget=budget,
                deadline=deadline,
                max_concurrent_bidders=max_concurrent_bidders or self.config.task.default_max_concurrent_bidders,
                max_depth=max_depth or self.config.task.default_max_depth,
                human_contact=human_contact,
                level=TaskLevel(level) if level else TaskLevel.GENERAL,
                invited_agent_ids=invited_agent_ids or [],
            )
            task = self.task_manager.create(task)
        except Exception:
            # Release frozen budget if task creation fails (#1)
            await self.escrow.release(task_id)
            raise
        self._log_event("create_task", task_id=task_id, agent_id=initiator_id)
        await self._broadcast_to_candidates(task)
        await self.cluster.broadcast_task({
            "task_id": task.id,
            "initiator_id": initiator_id,
            "domains": domains,
            "type": task.type.value,
            "budget": budget,
            "deadline": deadline,
            "content": content,
            "max_concurrent_bidders": task.max_concurrent_bidders,
            "level": task.level.value,
            "invited_agent_ids": task.invited_agent_ids,
        })

        return task


    async def submit_bid(
        self,
        task_id: str,
        agent_id: str,
        confidence: float,
        price: float,
        server_id: str | None = None,
    ) -> BidStatus:
        """Agent bids on a task: validate → add bid → push result."""
        async with self.task_manager.get_lock(task_id):
            return await self._submit_bid_inner(
                task_id, agent_id, confidence, price, server_id,
            )

    async def _submit_bid_inner(
        self,
        task_id: str,
        agent_id: str,
        confidence: float,
        price: float,
        server_id: str | None = None,
    ) -> BidStatus:
        task = self.task_manager.get(task_id)

        # Guard: reject bids if parent task has already terminated
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                    raise TaskError(
                        f"Parent task {task.parent_id} already terminated; "
                        f"cannot bid on child task {task_id}"
                    )
            except TaskError as e:
                if "already terminated" in str(e):
                    raise
                # Parent not found is OK (cross-node scenario)

        if any(b.agent_id == agent_id for b in task.bids):
            raise TaskError(f"Agent {agent_id} already bid on task {task_id}")

        scores = self.reputation.get_scores([agent_id])
        negotiation_gain = self.reputation.negotiation_gain(agent_id)
        is_adjudication = task.type == TaskType.ADJUDICATION

        # Get agent tier from discovery registry
        agent_tier = "general"
        agent_card = await self.discovery.bootstrap.get_agent_card(agent_id)
        if agent_card:
            agent_tier = agent_card.get("tier", "general")

        task_level = task.level.value if hasattr(task.level, "value") else str(task.level)
        is_invited = agent_id in task.invited_agent_ids

        check = self.matcher.check_bid(
            agent_id=agent_id,
            confidence=confidence,
            price=price,
            budget=task.budget,
            scores=scores,
            negotiation_gain=negotiation_gain,
            is_adjudication=is_adjudication,
            agent_tier=agent_tier,
            task_level=task_level,
            is_invited=is_invited,
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

        bid = Bid(agent_id=agent_id, confidence=confidence, price=price)
        bid_status = self.task_manager.add_bid(task_id, bid)

        await self.push.notify_bid_result(
            task_id, agent_id, accepted=True,
        )

        self._log_event("submit_bid", task_id=task_id, agent_id=agent_id)

        await self.gossip.exchange(agent_id, task.initiator_id)

        return bid_status


    async def invite_agent(self, task_id: str, initiator_id: str, agent_id: str) -> None:
        """Invite an agent to bid on a task (skips ability check)."""
        task = self.task_manager.get(task_id)
        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can invite agents")
        if task.status not in (TaskStatus.UNCLAIMED, TaskStatus.BIDDING):
            raise TaskError("Can only invite agents while task is open")

        # Guard: reject invites if parent task has already terminated
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                    raise TaskError(
                        f"Parent task {task.parent_id} already terminated; "
                        f"cannot invite agent to child task {task_id}"
                    )
            except TaskError as e:
                if "already terminated" in str(e):
                    raise

        if agent_id not in task.invited_agent_ids:
            task.invited_agent_ids.append(agent_id)

    async def reject_task(
        self,
        task_id: str,
        agent_id: str,
        reason: str = "",
    ) -> None:
        """Agent withdraws from task: reject bid → promote next."""
        task = self.task_manager.get(task_id)

        # Guard: reject if parent task has already terminated
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                    raise TaskError(
                        f"Parent task {task.parent_id} already terminated; "
                        f"cannot reject bid on child task {task_id}"
                    )
            except TaskError as e:
                if "already terminated" in str(e):
                    raise

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


    async def submit_result(
        self,
        task_id: str,
        agent_id: str,
        content: Any,
    ) -> None:
        """Agent submits result: validate → store → adjudicate → promote."""
        async with self.task_manager.get_lock(task_id):
            await self._submit_result_inner(task_id, agent_id, content)

    async def _submit_result_inner(
        self,
        task_id: str,
        agent_id: str,
        content: Any,
    ) -> None:
        task = self.task_manager.get(task_id)

        # Guard: reject results if parent task has already terminated
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                    raise TaskError(
                        f"Parent task {task.parent_id} already terminated "
                        f"(status={parent.status.value}); "
                        f"cannot submit result to child task {task_id}"
                    )
            except TaskError as e:
                if "already terminated" in str(e):
                    raise
                # Parent not found is OK (cross-node scenario)

        bidder_ids = [
            b.agent_id for b in task.bids
            if b.status in (BidStatus.EXECUTING, BidStatus.ACCEPTED, BidStatus.WAITING)
        ]
        if agent_id not in bidder_ids:
            raise TaskError(
                f"Agent {agent_id} is not an active bidder on task {task_id}"
            )

        # Prevent duplicate result submissions from same agent (#33)
        if any(r.agent_id == agent_id for r in task.results):
            raise TaskError(
                f"Agent {agent_id} already submitted a result for task {task_id}"
            )

        result = Result(agent_id=agent_id, content=content)

        self.task_manager.add_result(task_id, result)

        self._log_event("submit_result", task_id=task_id, agent_id=agent_id)

        #    collect result directly into parent task's result adjudications
        if task.type == TaskType.ADJUDICATION and task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                target = task.content.get("target_result_agent_id", "")
                # Extract verdict/score from content
                if isinstance(content, dict):
                    verdict = content.get("verdict", str(content))
                    try:
                        score = float(content.get("score", 1.0))
                    except (ValueError, TypeError):
                        score = 1.0
                    score = max(0.0, min(score, 1.0))
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
            except TaskError as e:
                if "not found" in str(e):
                    _log.debug("Adjudication parent task not found, skipping")
                else:
                    _log.warning("Adjudication collection failed: %s", e)
                    raise

        if self.adjudication.should_create_adjudication(task):
            await self._create_adjudication(task, agent_id)

        if self.task_manager.check_auto_collect(task_id):
            await self.push.notify_task_collected(task)
            await self._notify_status_cross_node(task, "awaiting_retrieval")
            return  # Task collected; skip promotion (#64)

        promoted = self.task_manager.promote_from_queue(task_id)
        if promoted:
            await self.push.notify_bid_result(
                task_id, promoted, accepted=True,
                reason="Promoted from wait queue",
            )

        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                await self.push.notify_subtask_completed(parent, task_id)
            except TaskError:
                _log.debug("Parent task %s not found for subtask completion", task.parent_id)


    async def select_result(
        self,
        task_id: str,
        agent_id: str,
        initiator_id: str,
        close_task: bool = False,
    ) -> None:
        """Initiator selects winning result: settle payment → update reputation.

        When close_task=True, allows selection during BIDDING status —
        the task is closed first, then the result is selected.
        """
        async with self.task_manager.get_lock(task_id):
            await self._select_result_inner(
                task_id, agent_id, initiator_id, close_task,
            )

    async def _select_result_inner(
        self,
        task_id: str,
        agent_id: str,
        initiator_id: str,
        close_task: bool = False,
    ) -> None:
        task = self.task_manager.get(task_id)

        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can select a result")

        if task.status not in (TaskStatus.AWAITING_RETRIEVAL, TaskStatus.COMPLETED):
            if close_task and task.status == TaskStatus.BIDDING:
                # Close the task first, then select
                task = self.task_manager.close_task(task_id)
                self._log_event("close_task", task_id=task_id, agent_id=initiator_id)
                if task.status == TaskStatus.NO_ONE_ABLE:
                    await self.settlement.refund_no_one_capable(task_id)
                    raise TaskError("No results submitted; task closed as no_one_able")
                await self._notify_status_cross_node(task, task.status.value)
            else:
                raise TaskError(
                    f"Cannot select result in status {task.status.value}; "
                    "task must be in awaiting_retrieval or completed "
                    "(use close_task=true to close during bidding)"
                )

        selected = self.task_manager.select_result(task_id, agent_id)

        bid_price = 0.0
        for bid in task.bids:
            if bid.agent_id == agent_id:
                bid_price = bid.price
                break

        if task.type != TaskType.ADJUDICATION and bid_price > 0:
            # Terminate children first to reclaim sub-escrow before settlement (#65)
            await self._terminate_children(task)
            try:
                await self.settlement.settle(task_id, agent_id, bid_price)
            except Exception:
                # Rollback bid states on settlement failure (#3)
                for bid in task.bids:
                    if bid.agent_id == agent_id:
                        bid.status = BidStatus.EXECUTING
                    elif bid.status == BidStatus.REJECTED:
                        # Can't reliably restore previous status; leave as-is
                        pass
                selected.selected = False
                raise

        await self.reputation.propagate_selection(task.initiator_id, agent_id)

        await self.cluster.trigger_gossip(task_id)

        self._log_event("select_result", task_id=task_id, agent_id=agent_id)

        # Children already terminated before settlement (#65)


    async def close_task(self, task_id: str, initiator_id: str) -> Task:
        """Initiator closes task → AWAITING_RETRIEVAL or NO_ONE_ABLE."""
        task = self.task_manager.get(task_id)

        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can close this task")

        task = self.task_manager.close_task(task_id)
        self._log_event("close_task", task_id=task_id, agent_id=initiator_id)

        if task.status == TaskStatus.AWAITING_RETRIEVAL:
            await self.push.notify_task_collected(task)
        elif task.status == TaskStatus.NO_ONE_ABLE:
            await self.settlement.refund_no_one_capable(task_id)

        await self._notify_status_cross_node(task, task.status.value)

        # Cascade-close child tasks (adjudication tasks etc.)
        await self._terminate_children(task)

        return task

    async def collect_results(self, task_id: str) -> list[Result]:
        """Initiator retrieves results. First call → COMPLETED."""
        results = self.task_manager.collect_results(task_id)
        self._log_event("collect_results", task_id=task_id)
        return results

    async def update_deadline(
        self, task_id: str, deadline: str, initiator_id: str,
    ) -> Task:
        task = self.task_manager.get(task_id)

        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can update the deadline")

        task = self.task_manager.update_deadline(task_id, deadline)
        self._log_event("update_deadline", task_id=task_id)
        return task

    async def update_discussions(
        self, task_id: str, message: str, initiator_id: str,
    ) -> Task:
        """Append discussion and push to all bidders."""
        task = self.task_manager.get(task_id)

        if task.initiator_id != initiator_id:
            raise TaskError("Only the task initiator can update discussions")

        if task.status not in (TaskStatus.BIDDING, TaskStatus.AWAITING_RETRIEVAL):
            raise TaskError(
                f"Cannot update discussions in status {task.status.value}; "
                "task must be in bidding or awaiting_retrieval"
            )

        # Guard: reject if parent task has already terminated
        if task.parent_id:
            try:
                parent = self.task_manager.get(task.parent_id)
                if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                    raise TaskError(
                        f"Parent task {task.parent_id} already terminated; "
                        f"cannot update discussions on child task {task_id}"
                    )
            except TaskError as e:
                if "already terminated" in str(e):
                    raise

        task = self.task_manager.update_discussions(task_id, message, author=initiator_id)
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
        """Initiator approves/rejects over-budget bids."""
        task = self.task_manager.get(task_id)

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
            await self.escrow.confirm_budget_increase(initiator_id, task_id, additional)
            try:
                task.budget = new_budget
                if task.remaining_budget is not None:
                    task.remaining_budget += additional
            except Exception:
                # Escrow already increased — this shouldn't fail for simple
                # attribute assignment, but guard against unexpected errors (#4)
                _log.error("Failed to update task budget after escrow increase")
                raise

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


    async def create_subtask(
        self,
        parent_task_id: str,
        initiator_id: str,
        content: dict[str, Any],
        domains: list[str],
        budget: float,
        deadline: str | None = None,
        level: str | None = None,
    ) -> Task:
        """Executor delegates subtask from parent's budget."""
        parent = self.task_manager.get(parent_task_id)

        # Guard: reject subtask creation if parent already terminated
        if parent.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
            raise TaskError(
                f"Parent task {parent_task_id} already terminated "
                f"(status={parent.status.value}); cannot create subtask"
            )

        bidder_ids = [b.agent_id for b in parent.bids]
        if initiator_id not in bidder_ids:
            raise TaskError(
                f"Agent {initiator_id} is not a bidder on parent task {parent_task_id}"
            )

        subtask = self.task_manager.create_subtask(
            parent_task_id=parent_task_id,
            content=content,
            domains=domains,
            budget=budget,
            initiator_id=initiator_id,
            deadline=deadline,
            level=level,
        )

        # Transfer escrow; rollback task_manager state on failure (#2)
        try:
            await self.escrow.allocate_subtask_budget(
                parent_task_id, subtask.id, initiator_id, budget,
            )
        except Exception:
            # Rollback: restore parent remaining_budget and remove subtask
            parent = self.task_manager.get(parent_task_id)
            if parent.remaining_budget is not None:
                parent.remaining_budget += budget
            if subtask.id in parent.child_ids:
                parent.child_ids.remove(subtask.id)
            self.task_manager._tasks.pop(subtask.id, None)
            raise

        self._log_event(
            "create_subtask", task_id=subtask.id, agent_id=initiator_id,
        )

        await self._broadcast_to_candidates(subtask)

        return subtask


    async def scan_deadlines(self, now: str | None = None) -> list[str]:
        """Expire overdue tasks and handle settlement."""
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
                await self.settlement.refund_no_one_capable(task.id)

            # Cascade-close child tasks on deadline expiry (#20)
            await self._terminate_children(task)

            await self._notify_status_cross_node(task, "timeout")

            self._log_event("task_timeout", task_id=task.id)

        return expired_ids


    async def receive_reputation_event(
        self,
        agent_id: str,
        event_type: str,
        server_id: str,
    ) -> float:
        """Receive a reputation event from a server.

        Only raw events are accepted (not scores).
        Returns the updated reputation score.
        """
        return await self.reputation.aggregate(
            agent_id,
            [{"type": event_type}],
            server_id=server_id,
        )


    async def _notify_status_cross_node(self, task: Task, status: str) -> None:
        """Notify participant nodes about a task status change.

        Collects all active agent IDs involved in the task and includes them
        as recipients so the receiving node can deliver locally.
        """
        participant_nodes = self.cluster.router.get_participants(task.id)
        if not participant_nodes:
            return
        # Gather agents involved in the task
        recipients = list({
            *[b.agent_id for b in task.bids
              if b.status.value in ("executing", "waiting", "accepted")],
            task.initiator_id,
        })
        await self.cluster.router.notify_status(
            task.id, status, participant_nodes,
            payload={"status": status, "recipients": recipients},
        )

    async def _terminate_children(self, task: Task) -> None:
        """Cascade-close all active child tasks when parent terminates.

        When a parent task terminates, none of its children should
        continue accepting bids or results.  Active bids are rejected
        and affected agents are notified.
        """
        for child_id in task.child_ids:
            try:
                child = self.task_manager.get(child_id)
            except TaskError:
                continue
            if child.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                continue

            # Reject all active bids and notify affected agents
            for bid in child.bids:
                if bid.status in (BidStatus.EXECUTING, BidStatus.WAITING, BidStatus.PENDING):
                    bid.status = BidStatus.REJECTED
                    await self.push.notify_bid_result(
                        child_id, bid.agent_id, accepted=False,
                        reason="Parent task terminated",
                    )

            try:
                child = self.task_manager.close_task(child_id)
                # Refund child escrow (#67)
                await self.settlement.refund_no_one_capable(child_id)
                _log.info(
                    "Cascade-closed child task %s (parent %s terminated)",
                    child_id, task.id,
                )
            except TaskError:
                continue
            # Recurse into grandchildren
            await self._terminate_children(child)

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
        # Don't create adjudication for terminated parents (#66)
        if parent_task.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
            return
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
