"""Network-side global matcher: agent matching and bid validation.

Design:
- Stateless: pure functions, no stored state
- Two-sided: used by both Network (global) and Server (local)
- Stage 1: static label + keyword matching
- Stage 2 (planned): semantic embedding
- Stage 3 (planned): semantic routing + combination
"""

from __future__ import annotations

from datetime import datetime, timezone

from eacn.core.models import Task, AgentCard


class GlobalMatcher:
    """Global matching from aggregated events across all servers.

    match_agents: Find candidate agents from list matching task
    check_bid: Validate bid admissions (ability gate + price gate)
    """

    def __init__(self, config: "MatcherConfig | None" = None) -> None:
        from eacn.network.config import MatcherConfig
        cfg = config or MatcherConfig()
        self._w_rep: float = cfg.weight_reputation
        self._w_domain: float = cfg.weight_domain
        self._w_keyword: float = cfg.weight_keyword
        self._default_rep: float = cfg.default_reputation
        self._ability_threshold: float = cfg.ability_threshold
        self._price_tolerance: float = cfg.price_tolerance
        self._target_min_rep: float = cfg.target_min_reputation

    # ── Agent matching ───────────────────────────────────────────────

    def match_agents(
        self,
        task: Task,
        agents: list[AgentCard],
        scores: dict[str, float],
    ) -> list[AgentCard]:
        """Stage 1: Static label + keyword matching.

        1. Domain tag intersection
        2. Description keyword matching (task.content.description ↔ agent.description)
        3. Sort by reputation score
        """
        task_domains = set(task.domains)
        task_desc = (task.content.get("description") or "").lower()
        task_keywords = set(task_desc.split()) if task_desc else set()

        candidates: list[tuple[float, AgentCard]] = []

        for agent in agents:
            # Domain intersection score
            domain_overlap = len(set(agent.domains) & task_domains)
            if domain_overlap == 0:
                continue

            # Keyword matching score (bonus)
            keyword_score = 0.0
            if task_keywords and agent.description:
                agent_words = set(agent.description.lower().split())
                keyword_overlap = len(task_keywords & agent_words)
                keyword_score = keyword_overlap / max(len(task_keywords), 1)

            reputation = scores.get(agent.agent_id, self._default_rep)
            composite = (
                reputation * self._w_rep
                + (domain_overlap / max(len(task_domains), 1)) * self._w_domain
                + keyword_score * self._w_keyword
            )
            candidates.append((composite, agent))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [agent for _, agent in candidates]

    # ── Tier eligibility ────────────────────────────────────────────

    TIER_HIERARCHY = ["general", "expert", "expert_general", "tool"]

    def is_tier_eligible(self, agent_tier: str, task_level: str) -> bool:
        """Check whether an agent tier is eligible to bid on a task level.

        Rule: tool-tier agents can ONLY bid on tool-level tasks.
        All other tiers (general, expert, expert_general) can bid on ANY task level.
        The tier is a self-declaration of specialization breadth, not a hard gate —
        an expert should still be able to take general tasks.
        """
        if agent_tier == "tool":
            return task_level == "tool"
        return True

    # ── Bid validation ───────────────────────────────────────────────

    def _is_past_half_deadline(
        self,
        task_deadline: str | None,
        task_created_at: str | None,
    ) -> bool:
        """Check if the task is past half its lifetime (fallback window).
        Falls back to checking if <50% of time remains when created_at is unavailable."""
        if not task_deadline:
            return False
        try:
            now = datetime.now(timezone.utc)
            deadline_dt = datetime.fromisoformat(task_deadline.replace("Z", "+00:00"))
            if task_created_at:
                created_dt = datetime.fromisoformat(task_created_at.replace("Z", "+00:00"))
                total = (deadline_dt - created_dt).total_seconds()
                elapsed = (now - created_dt).total_seconds()
                return total > 0 and elapsed > total * 0.5
            else:
                # No created_at — check if less than half the remaining time
                # Use a heuristic: if deadline is within 1 hour, treat as fallback
                remaining = (deadline_dt - now).total_seconds()
                return remaining < 3600
        except Exception:
            return False

    def check_bid(
        self,
        agent_id: str,
        confidence: float,
        price: float,
        budget: float,
        scores: dict[str, float],
        negotiation_gain: float = 0.0,
        is_adjudication: bool = False,
        threshold: float | None = None,
        tolerance: float | None = None,
        agent_tier: str | None = None,
        task_level: str | None = None,
        is_invited: bool = False,
        has_bids: bool = True,
        task_deadline: str | None = None,
        task_created_at: str | None = None,
    ) -> BidCheckResult:
        """Validate bid: ability gate + price gate.

        Ability: confidence × reputation ≥ threshold
        Price: price ≤ budget × (1 + tolerance + negotiation_gain)
               (skip price check for adjudication tasks)

        Fallback: if the task has no bids and is past half its deadline,
        ability and tier gates are relaxed — better to let someone try
        than to let the task expire with no one.

        Returns BidCheckResult with pass/fail and reason.
        """
        # Fallback mode: task has no bids AND past half deadline → relax gates
        fallback = (
            not has_bids
            and self._is_past_half_deadline(task_deadline, task_created_at)
        )

        # Tier eligibility check (skip if invited or fallback)
        if agent_tier and task_level and not is_invited and not fallback:
            if not self.is_tier_eligible(agent_tier, task_level):
                return BidCheckResult(
                    passed=False,
                    reason=f"Tier {agent_tier} not eligible for level {task_level}",
                    needs_budget_confirmation=False,
                )

        if threshold is None:
            threshold = self._ability_threshold
        if tolerance is None:
            tolerance = self._price_tolerance
        reputation = scores.get(agent_id, self._default_rep)
        ability = confidence * reputation

        # Skip ability check if invited or fallback
        if is_invited or fallback:
            # Still do price check below
            pass
        elif ability < threshold:
            return BidCheckResult(
                passed=False,
                reason=f"Ability check failed: {ability:.3f} < {threshold}",
                needs_budget_confirmation=False,
            )

        # Adjudication: no price check
        if is_adjudication:
            return BidCheckResult(passed=True)

        max_price = budget * (1 + tolerance + negotiation_gain)

        if price <= max_price:
            return BidCheckResult(passed=True)

        # Price exceeds tolerance → may need budget confirmation
        return BidCheckResult(
            passed=False,
            reason=f"Price {price:.2f} exceeds max {max_price:.2f}",
            needs_budget_confirmation=True,
            excess_amount=price - max_price,
        )

    # ── Server-side explicit target validation ───────────────────────

    def validate_target(
        self,
        target: AgentCard,
        task: Task,
        scores: dict[str, float],
        threshold: float | None = None,
    ) -> bool:
        """Validate an explicitly-specified target agent.

        Checks: domain overlap + minimum reputation.
        """
        if threshold is None:
            threshold = self._target_min_rep
        if not (set(target.domains) & set(task.domains)):
            return False
        reputation = scores.get(target.agent_id, self._default_rep)
        return reputation >= threshold


class BidCheckResult:
    """Result of bid validation check."""

    __slots__ = ("passed", "reason", "needs_budget_confirmation", "excess_amount")

    def __init__(
        self,
        passed: bool = True,
        reason: str = "",
        needs_budget_confirmation: bool = False,
        excess_amount: float = 0.0,
    ) -> None:
        self.passed = passed
        self.reason = reason
        self.needs_budget_confirmation = needs_budget_confirmation
        self.excess_amount = excess_amount

