"""Task, Bid, Result data models and status/type enums."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskLevel(str, Enum):
    GENERAL = "general"
    EXPERT = "expert"
    EXPERT_GENERAL = "expert_general"
    TOOL = "tool"


class TaskStatus(str, Enum):
    UNCLAIMED = "unclaimed"
    BIDDING = "bidding"
    AWAITING_RETRIEVAL = "awaiting_retrieval"
    COMPLETED = "completed"
    NO_ONE_ABLE = "no_one_able"


class TaskType(str, Enum):
    NORMAL = "normal"
    ADJUDICATION = "adjudication"


class BidStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WAITING = "waiting"       # queued for execution
    EXECUTING = "executing"   # currently executing


class Bid(BaseModel):
    agent_id: str
    server_id: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    price: float = Field(ge=0.0)
    status: BidStatus = BidStatus.PENDING


class Adjudication(BaseModel):
    adjudicator_id: str
    verdict: str
    score: float


class Result(BaseModel):
    agent_id: str
    content: Any
    selected: bool = False
    adjudications: list[Adjudication] = Field(default_factory=list)


class HumanContact(BaseModel):
    """Permission toggle for executor to contact a human.

    Agents cannot contact humans by default. The task initiator sets this field
    when creating a task, authorizing the assigned executor to contact a
    designated human when needed (e.g. for requirement clarification).

    - allowed:    whether the executor may contact a human, default False
    - contact_id: identifier of the human to contact when allowed
    - timeout_s:  seconds to wait for human response; executor should decide on its own after timeout
    """
    allowed: bool = False
    contact_id: str | None = None
    timeout_s: int | None = None


class Task(BaseModel):
    id: str
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="description, attachments, expected_output, discussions",
    )
    type: TaskType = TaskType.NORMAL
    initiator_id: str
    server_id: str = ""
    domains: list[str] = Field(min_length=1)
    status: TaskStatus = TaskStatus.UNCLAIMED
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    depth: int = 0
    max_depth: int = 10
    budget: float = Field(ge=0.0)
    remaining_budget: float | None = None  # tracked by economy; None = full budget
    deadline: str | None = None  # ISO 8601
    max_concurrent_bidders: int = 5
    bids: list[Bid] = Field(default_factory=list)
    results: list[Result] = Field(default_factory=list)
    budget_locked: bool = False  # True when concurrent slots full
    human_contact: HumanContact | None = None
    level: TaskLevel = TaskLevel.GENERAL
    invited_agent_ids: list[str] = Field(default_factory=list)

    @property
    def executing_agents(self) -> list[str]:
        """Agent IDs currently executing (accepted/executing bids)."""
        return [
            b.agent_id for b in self.bids
            if b.status in (BidStatus.ACCEPTED, BidStatus.EXECUTING)
        ]

    @property
    def waiting_agents(self) -> list[str]:
        """Agent IDs in wait queue."""
        return [b.agent_id for b in self.bids if b.status == BidStatus.WAITING]

    @property
    def concurrent_slots_full(self) -> bool:
        return len(self.executing_agents) >= self.max_concurrent_bidders
