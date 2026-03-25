"""Task storage, state machine, tree structure, concurrent bidder management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from eacn.core.models import Task, TaskStatus, TaskType, TaskLevel, Bid, BidStatus, Result
from eacn.core.exceptions import TaskError, BudgetError


class TaskManager:
    """Single source of truth for task state.

    Responsibilities:
    - CRUD for tasks
    - State machine transitions (only valid ones)
    - Concurrent bidder slots + wait queue + auto-promotion
    - Subtask creation with depth guard
    - Deadline-based scanning
    """

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    # ── CRUD ─────────────────────────────────────────────────────────

    def create(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise TaskError(f"Task {task.id} already exists")
        if task.remaining_budget is None:
            task.remaining_budget = task.budget
        self._tasks[task.id] = task
        if task.parent_id and task.parent_id in self._tasks:
            self._tasks[task.parent_id].child_ids.append(task.id)
        return task

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if not task:
            raise TaskError(f"Task {task_id} not found")
        return task

    def list_all(self) -> list[Task]:
        return list(self._tasks.values())

    # ── State machine ────────────────────────────────────────────────

    def transition(self, task_id: str, new_status: TaskStatus) -> Task:
        """Execute a validated state transition."""
        task = self.get(task_id)
        valid = self._valid_transitions(task.status)
        if new_status not in valid:
            raise TaskError(
                f"Invalid transition: {task.status} → {new_status}"
            )
        task.status = new_status
        return task

    @staticmethod
    def _valid_transitions(status: TaskStatus) -> list[TaskStatus]:
        return {
            TaskStatus.UNCLAIMED: [TaskStatus.BIDDING, TaskStatus.NO_ONE_ABLE],
            TaskStatus.BIDDING: [
                TaskStatus.AWAITING_RETRIEVAL,
                TaskStatus.NO_ONE_ABLE,
            ],
            TaskStatus.AWAITING_RETRIEVAL: [
                TaskStatus.COMPLETED,
                TaskStatus.NO_ONE_ABLE,
            ],
            TaskStatus.COMPLETED: [],
            TaskStatus.NO_ONE_ABLE: [],
        }.get(status, [])

    # ── Bidding & concurrent management ──────────────────────────────

    def add_bid(self, task_id: str, bid: Bid) -> BidStatus:
        """Add a bid. Returns the bid's resulting status.

        - If task is UNCLAIMED, transition to BIDDING.
        - If concurrent slots available, mark EXECUTING.
        - If slots full, mark WAITING (queue).
        """
        task = self.get(task_id)
        if task.status == TaskStatus.UNCLAIMED:
            task.status = TaskStatus.BIDDING

        if task.status not in (TaskStatus.BIDDING,):
            raise TaskError(f"Cannot bid on task in status {task.status}")

        if not task.concurrent_slots_full:
            bid.status = BidStatus.EXECUTING
        else:
            bid.status = BidStatus.WAITING

        task.bids.append(bid)

        # Lock budget when concurrent slots become full
        if task.concurrent_slots_full:
            task.budget_locked = True

        return bid.status

    def promote_from_queue(self, task_id: str) -> str | None:
        """Promote the next waiting agent to executing. Returns agent_id or None."""
        task = self.get(task_id)
        for bid in task.bids:
            if bid.status == BidStatus.WAITING:
                bid.status = BidStatus.EXECUTING
                # Unlock budget if we have room again
                if not task.concurrent_slots_full:
                    task.budget_locked = False
                return bid.agent_id
        return None

    def reject_bid(self, task_id: str, agent_id: str) -> None:
        """Mark a bid as rejected (failed execution or explicit rejection)."""
        task = self.get(task_id)
        for bid in task.bids:
            if bid.agent_id == agent_id:
                bid.status = BidStatus.REJECTED
                return
        raise TaskError(f"Bid from {agent_id} not found on task {task_id}")

    def accept_bid(self, task_id: str, agent_id: str) -> None:
        """Explicitly accept a bid (used during result selection)."""
        task = self.get(task_id)
        for bid in task.bids:
            if bid.agent_id == agent_id:
                bid.status = BidStatus.ACCEPTED
                return
        raise TaskError(f"Bid from {agent_id} not found on task {task_id}")

    # ── Results ──────────────────────────────────────────────────────

    def add_result(self, task_id: str, result: Result) -> None:
        """Submit a result for a task."""
        task = self.get(task_id)
        if task.status not in (TaskStatus.BIDDING, TaskStatus.AWAITING_RETRIEVAL):
            raise TaskError(f"Cannot submit result in status {task.status}")
        task.results.append(result)

    def select_result(self, task_id: str, agent_id: str) -> Result:
        """Select a result. Marks it as selected and transitions task."""
        task = self.get(task_id)
        selected = None
        for r in task.results:
            if r.agent_id == agent_id:
                r.selected = True
                selected = r
                break
        if not selected:
            raise TaskError(f"No result from {agent_id} on task {task_id}")
        # Accept the winning bid, reject others
        for bid in task.bids:
            if bid.agent_id == agent_id:
                bid.status = BidStatus.ACCEPTED
            elif bid.status in (BidStatus.EXECUTING, BidStatus.WAITING):
                bid.status = BidStatus.REJECTED
        return selected

    # ── Subtask creation ─────────────────────────────────────────────

    def create_subtask(
        self,
        parent_task_id: str,
        content: dict[str, Any],
        domains: list[str],
        budget: float,
        initiator_id: str,
        deadline: str | None = None,
        level: str | None = None,
    ) -> Task:
        """Create a child task, inheriting max_concurrent_bidders and depth guard."""
        parent = self.get(parent_task_id)
        new_depth = parent.depth + 1

        if new_depth >= parent.max_depth:
            raise TaskError(
                f"Max depth {parent.max_depth} exceeded (current: {new_depth})"
            )

        if parent.remaining_budget is not None and budget > parent.remaining_budget:
            raise BudgetError(
                f"Subtask budget {budget} exceeds parent remaining {parent.remaining_budget}"
            )

        subtask = Task(
            id=f"sub-{parent_task_id}-{uuid4().hex[:8]}",
            content=content,
            type=parent.type,
            initiator_id=initiator_id,
            domains=domains,
            parent_id=parent_task_id,
            depth=new_depth,
            max_depth=parent.max_depth,
            budget=budget,
            remaining_budget=budget,
            deadline=deadline or parent.deadline,
            max_concurrent_bidders=parent.max_concurrent_bidders,
            level=TaskLevel(level) if level else parent.level,
        )

        # Deduct from parent remaining budget
        if parent.remaining_budget is not None:
            parent.remaining_budget -= budget

        return self.create(subtask)

    # ── Task control ─────────────────────────────────────────────────

    def close_task(self, task_id: str) -> Task:
        """Initiator manually closes a task → AWAITING_RETRIEVAL or NO_ONE_ABLE.

        Precondition: status must be UNCLAIMED, BIDDING, or AWAITING_RETRIEVAL.
        """
        task = self.get(task_id)
        if task.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
            raise TaskError(
                f"Cannot close task in status {task.status.value}"
            )
        if task.results:
            task.status = TaskStatus.AWAITING_RETRIEVAL
        else:
            task.status = TaskStatus.NO_ONE_ABLE
        return task

    def collect_results(self, task_id: str) -> list[Result]:
        """Collect results. First call transitions AWAITING_RETRIEVAL → COMPLETED.

        Precondition: status must be AWAITING_RETRIEVAL or COMPLETED.
        """
        task = self.get(task_id)
        if task.status not in (TaskStatus.AWAITING_RETRIEVAL, TaskStatus.COMPLETED):
            raise TaskError(
                f"Cannot collect results in status {task.status.value}; "
                "task must be in awaiting_retrieval or completed"
            )
        if task.status == TaskStatus.AWAITING_RETRIEVAL:
            task.status = TaskStatus.COMPLETED
        return list(task.results)

    def update_deadline(self, task_id: str, deadline: str) -> Task:
        task = self.get(task_id)
        if task.status in (
            TaskStatus.COMPLETED,
            TaskStatus.NO_ONE_ABLE,
        ):
            raise TaskError(f"Cannot update deadline in status {task.status}")
        task.deadline = deadline
        return task

    def update_discussions(self, task_id: str, message: str, author: str = "") -> Task:
        """Append a discussion message to task content."""
        task = self.get(task_id)
        discussions = task.content.setdefault("discussions", [])
        discussions.append({
            "message": message,
            "author": author,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return task

    # ── Deadline scanning ────────────────────────────────────────────

    @staticmethod
    def _parse_datetime(s: str) -> datetime:
        """Parse ISO 8601 datetime, normalizing Z suffix (#19)."""
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    def scan_expired(self, now: str | None = None) -> list[Task]:
        """Find tasks whose deadline has passed. Returns list of expired tasks."""
        now_dt = (
            self._parse_datetime(now)
            if now is not None
            else datetime.now(timezone.utc)
        )
        expired = []
        for task in self._tasks.values():
            if task.status in (TaskStatus.COMPLETED, TaskStatus.NO_ONE_ABLE):
                continue
            if task.deadline:
                try:
                    deadline_dt = self._parse_datetime(task.deadline)
                    if deadline_dt <= now_dt:
                        expired.append(task)
                except ValueError:
                    continue
        return expired

    def handle_expired(self, task_id: str) -> TaskStatus:
        """Handle an expired task: transition based on whether results exist."""
        task = self.get(task_id)
        if task.results:
            task.status = TaskStatus.AWAITING_RETRIEVAL
        else:
            task.status = TaskStatus.NO_ONE_ABLE
        return task.status

    # ── Auto-collection check ────────────────────────────────────────

    def check_auto_collect(self, task_id: str) -> bool:
        """Check if task should auto-transition to AWAITING_RETRIEVAL.

        Triggers:
        - All concurrent slots have submitted results
        - max_concurrent_bidders results collected
        """
        task = self.get(task_id)
        if task.status != TaskStatus.BIDDING:
            return False
        if len(task.results) >= task.max_concurrent_bidders:
            task.status = TaskStatus.AWAITING_RETRIEVAL
            return True
        return False

    # ── Tree operations ──────────────────────────────────────────────

    def get_subtree(self, task_id: str, _visited: set[str] | None = None) -> list[Task]:
        """Return all tasks in the subtree rooted at task_id."""
        if _visited is None:
            _visited = set()
        if task_id in _visited:
            return []  # Cycle detected (#27)
        _visited.add(task_id)
        task = self.get(task_id)
        subtree = [task]
        for child_id in task.child_ids:
            subtree.extend(self.get_subtree(child_id, _visited))
        return subtree

    def get_root(self, task_id: str) -> Task:
        """Walk up to find the root task."""
        task = self.get(task_id)
        while task.parent_id:
            task = self.get(task.parent_id)
        return task
