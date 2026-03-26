"""Network-side global reputation aggregation.

Design:
- Weighted PageRank: event impact scaled by source server's reputation
- Cap mechanism: single adjudication gain/penalty capped, counts tracked
- Cold-start: new servers' events initially discounted
- Anti-gaming: anomaly detection for rapid success clusters

All score mutations are persisted to the database when a db reference is provided.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, TYPE_CHECKING

from eacn.core.models import LogEntry

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_logger = logging.getLogger(__name__)


class GlobalReputation:
    """Aggregates reputation from all server events with weighted PageRank."""

    def __init__(
        self,
        config: "ReputationConfig | None" = None,
        db: "Database | None" = None,
    ) -> None:
        from eacn.network.config import ReputationConfig
        cfg = config or ReputationConfig()
        self._db = db
        self.MAX_GAIN: float = cfg.max_gain
        self.MAX_PENALTY: float = cfg.max_penalty
        self.DEFAULT_SCORE: float = cfg.default_score
        self.COLD_START_THRESHOLD: int = cfg.cold_start_threshold
        self._cold_start_floor: float = cfg.cold_start_floor
        self._cold_start_ramp: float = cfg.cold_start_ramp
        self.EVENT_WEIGHTS: dict[str, float] = dict(cfg.event_weights)
        self._selection_boost_multiplier: float = cfg.selection_boost_multiplier
        self._selector_judgment_boost: float = cfg.selector_judgment_boost
        self.BURST_WINDOW: int = cfg.burst_window
        self.BURST_THRESHOLD: int = cfg.burst_threshold
        self._neg_gain_multiplier: float = cfg.negotiation_gain_multiplier
        self._neg_gain_min: float = cfg.negotiation_gain_min
        self._neg_gain_max: float = cfg.negotiation_gain_max
        self._scores: dict[str, float] = {}
        self._cap_counts: dict[str, dict[str, int]] = {}
        self._server_event_counts: dict[str, int] = {}
        self._server_reputation: dict[str, float] = {}
        # Anomaly tracking: recent events per agent for burst detection (#106)
        self._recent_events: dict[str, deque[str]] = {}

    async def load_from_db(self) -> None:
        """Restore reputation state from the database."""
        if not self._db:
            return
        for agent_id, score, cap_counts in await self._db.list_all_reputations():
            self._scores[agent_id] = score
            self._cap_counts[agent_id] = cap_counts
        for server_id, score, event_count in await self._db.list_all_server_reputations():
            self._server_reputation[server_id] = score
            self._server_event_counts[server_id] = event_count
        _logger.info(
            "Loaded %d agent reputations, %d server reputations from DB",
            len(self._scores), len(self._server_reputation),
        )

    # ── DB sync helpers ───────────────────────────────────────────────

    async def _persist_agent(self, agent_id: str) -> None:
        if not self._db:
            return
        await self._db.upsert_reputation(
            agent_id,
            self._scores.get(agent_id, self.DEFAULT_SCORE),
            self._cap_counts.get(agent_id, {}),
        )

    async def _persist_server(self, server_id: str) -> None:
        if not self._db:
            return
        await self._db.upsert_server_reputation(
            server_id,
            self._server_reputation.get(server_id, self.DEFAULT_SCORE),
            self._server_event_counts.get(server_id, 0),
        )

    def update_config(self, config: "ReputationConfig") -> None:
        """Update config parameters without resetting accumulated state."""
        self.MAX_GAIN = config.max_gain
        self.MAX_PENALTY = config.max_penalty
        self.DEFAULT_SCORE = config.default_score
        self.COLD_START_THRESHOLD = config.cold_start_threshold
        self._cold_start_floor = config.cold_start_floor
        self._cold_start_ramp = config.cold_start_ramp
        self.EVENT_WEIGHTS = dict(config.event_weights)
        self._selection_boost_multiplier = config.selection_boost_multiplier
        self._selector_judgment_boost = config.selector_judgment_boost
        self.BURST_WINDOW = config.burst_window
        self.BURST_THRESHOLD = config.burst_threshold
        self._neg_gain_multiplier = config.negotiation_gain_multiplier
        self._neg_gain_min = config.negotiation_gain_min
        self._neg_gain_max = config.negotiation_gain_max

    # ── Core API ─────────────────────────────────────────────────────

    async def aggregate(
        self,
        agent_id: str,
        events: list[dict[str, Any] | LogEntry],
        server_id: str | None = None,
    ) -> float:
        """Aggregate events with server reputation weight.

        The source server's own reputation scales event impact (PageRank-like).
        Cold-start servers have reduced weight.
        """
        server_weight = self._get_server_weight(server_id) if server_id else 1.0
        score = self._scores.get(agent_id, self.DEFAULT_SCORE)

        for event in events:
            event_dict = event.model_dump() if isinstance(event, LogEntry) else event
            event_type = event_dict.get("type") or event_dict.get("fn_name", "")

            # Anomaly check: burst detection
            if self._detect_anomaly(agent_id, event_type):
                _logger.warning(
                    "Anomaly detected for agent %s: burst of %s events",
                    agent_id, event_type,
                )
                continue

            raw_delta = self.EVENT_WEIGHTS.get(event_type, 0.0)
            if raw_delta == 0.0:
                continue

            # Apply server weight (PageRank-like propagation)
            delta = raw_delta * server_weight

            # Apply cap
            delta = max(self.MAX_PENALTY, min(self.MAX_GAIN, delta))

            # Track cap counts
            if delta == self.MAX_GAIN and raw_delta * server_weight > self.MAX_GAIN:
                counts = self._cap_counts.setdefault(agent_id, {})
                counts["capped_gain"] = counts.get("capped_gain", 0) + 1
            if delta == self.MAX_PENALTY and raw_delta * server_weight < self.MAX_PENALTY:
                counts = self._cap_counts.setdefault(agent_id, {})
                counts["capped_penalty"] = counts.get("capped_penalty", 0) + 1

            score = max(0.0, min(1.0, score + delta))

            # Update server event count
            if server_id:
                self._server_event_counts[server_id] = (
                    self._server_event_counts.get(server_id, 0) + 1
                )

        self._scores[agent_id] = score
        await self._persist_agent(agent_id)
        if server_id:
            await self._persist_server(server_id)
        return score

    def get_score(self, agent_id: str) -> float:
        return self._scores.get(agent_id, self.DEFAULT_SCORE)

    def get_scores(self, agent_ids: list[str]) -> dict[str, float]:
        return {aid: self.get_score(aid) for aid in agent_ids}

    def get_cap_counts(self, agent_id: str) -> dict[str, int]:
        return dict(self._cap_counts.get(agent_id, {}))

    def get_all_scores(self) -> dict[str, float]:
        return dict(self._scores)

    # ── PageRank: result selection propagation ───────────────────────

    async def propagate_selection(
        self,
        selector_id: str,
        selected_id: str,
    ) -> None:
        """When selector picks selected's result, both gain reputation.

        The boost for selected scales with selector's reputation (PageRank).
        """
        selector_rep = self.get_score(selector_id)
        # Selected agent gets a boost scaled by selector's reputation
        selected_score = self.get_score(selected_id)
        boost = self._selection_boost_multiplier * selector_rep
        self._scores[selected_id] = min(1.0, selected_score + boost)

        # Selector also gets a small boost for "good judgment"
        selector_score = self.get_score(selector_id)
        self._scores[selector_id] = min(1.0, selector_score + self._selector_judgment_boost)

        await self._persist_agent(selected_id)
        await self._persist_agent(selector_id)

    # ── Server reputation ────────────────────────────────────────────

    async def set_server_reputation(self, server_id: str, score: float) -> None:
        """Set a server's own reputation (derived from its agents' aggregate)."""
        self._server_reputation[server_id] = max(0.0, min(1.0, score))
        await self._persist_server(server_id)

    def get_server_reputation(self, server_id: str) -> float:
        return self._server_reputation.get(server_id, self.DEFAULT_SCORE)

    def _get_server_weight(self, server_id: str | None) -> float:
        """Server's weight for event aggregation.

        Cold-start: linearly ramp from 0.1 to 1.0 based on event count.
        Then scaled by server's own reputation.
        """
        if not server_id:
            return 1.0

        event_count = self._server_event_counts.get(server_id, 0)
        cold_start_factor = min(
            1.0,
            self._cold_start_floor + self._cold_start_ramp * (event_count / self.COLD_START_THRESHOLD),
        )

        server_rep = self._server_reputation.get(server_id, self.DEFAULT_SCORE)
        return cold_start_factor * server_rep

    # ── Anti-gaming: anomaly detection ───────────────────────────────

    def _detect_anomaly(self, agent_id: str, event_type: str) -> bool:
        """Simple burst detection: too many same-type events in recent window."""
        if agent_id not in self._recent_events:
            self._recent_events[agent_id] = deque(maxlen=self.BURST_WINDOW)
        recent = self._recent_events[agent_id]
        recent.append(event_type)

        same_type_count = sum(1 for e in recent if e == event_type)
        return same_type_count >= self.BURST_THRESHOLD

    # ── Negotiation gain (for Matcher) ───────────────────────────────

    def negotiation_gain(self, agent_id: str) -> float:
        """Calculate price negotiation gain from cap counts.

        Formula: multiplier × (capped_gain - capped_penalty), clamped.
        """
        counts = self._cap_counts.get(agent_id, {})
        gain = counts.get("capped_gain", 0)
        penalty = counts.get("capped_penalty", 0)
        return max(self._neg_gain_min, min(self._neg_gain_max, self._neg_gain_multiplier * (gain - penalty)))
