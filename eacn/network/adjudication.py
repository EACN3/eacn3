"""Automatic adjudication task publishing and result collection.

Design:
- Auto-created when result submitted (one adjudication per result)
- Special bidding: no budget, no price, ability-only check
- Auto-collection: all results directly written to adjudications list
- Recursion termination: adjudication tasks don't spawn further adjudications
- Reputation-only reward (no monetary compensation)
"""

from __future__ import annotations

from uuid import uuid4

from eacn.core.models import (
    Task, TaskType, TaskStatus, Result, Adjudication,
)


class AdjudicationService:
    """Manages adjudication task lifecycle."""

    def should_create_adjudication(self, task: Task) -> bool:
        """Check if a task's results should trigger adjudication.

        Returns False for adjudication tasks (recursion termination).
        """
        return task.type != TaskType.ADJUDICATION

    def create_adjudication_task(
        self,
        parent_task: Task,
        result_agent_id: str,
    ) -> Task:
        """Create an adjudication task for a submitted result.

        Adjudication tasks:
        - type = ADJUDICATION (prevents further adjudication spawning)
        - budget = 0 (no monetary compensation)
        - Inherit parent's domains and deadline
        - parent_id links to original task
        """
        return Task(
            id=f"adj-{parent_task.id}-{result_agent_id}-{uuid4().hex[:6]}",
            content={
                "description": f"Adjudicate result from {result_agent_id}",
                "parent_task_id": parent_task.id,
                "target_result_agent_id": result_agent_id,
            },
            type=TaskType.ADJUDICATION,
            initiator_id="system",
            domains=parent_task.domains,
            status=TaskStatus.UNCLAIMED,
            parent_id=parent_task.id,
            depth=parent_task.depth + 1,
            max_depth=parent_task.max_depth,
            budget=0.0,
            remaining_budget=0.0,
            deadline=parent_task.deadline,
            max_concurrent_bidders=parent_task.max_concurrent_bidders,
        )

    def collect_adjudication_result(
        self,
        parent_task: Task,
        target_result_agent_id: str,
        adjudicator_id: str,
        verdict: str,
        score: float,
    ) -> Adjudication:
        """Auto-collect: directly append adjudication to the target result.

        No select_result needed — all adjudication results are accepted.
        """
        adjudication = Adjudication(
            adjudicator_id=adjudicator_id,
            verdict=verdict,
            score=score,
        )

        # Find the target result and append adjudication
        for result in parent_task.results:
            if result.agent_id == target_result_agent_id:
                result.adjudications.append(adjudication)
                break

        return adjudication

    def compute_adjudication_summary(
        self, result: Result
    ) -> dict[str, float]:
        """Compute summary statistics from all adjudications on a result."""
        if not result.adjudications:
            return {"count": 0, "avg_score": 0.0, "min_score": 0.0, "max_score": 0.0}

        scores = [a.score for a in result.adjudications]
        return {
            "count": len(scores),
            "avg_score": sum(scores) / len(scores),
            "min_score": min(scores),
            "max_score": max(scores),
        }
