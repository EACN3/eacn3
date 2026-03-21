"""Pydantic request/response schemas for the Network HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ── Task ─────────────────────────────────────────────────────────────

class HumanContactSchema(BaseModel):
    allowed: bool = False
    contact_id: str | None = None
    timeout_s: int | None = None


class CreateTaskRequest(BaseModel):
    task_id: str
    initiator_id: str
    content: dict[str, Any] = Field(default_factory=dict)
    domains: list[str] = Field(min_length=1)
    budget: float = Field(ge=0.0)
    deadline: str | None = None
    max_concurrent_bidders: int | None = None
    max_depth: int | None = None
    human_contact: HumanContactSchema | None = None


class TaskResponse(BaseModel):
    id: str
    status: str
    initiator_id: str
    domains: list[str]
    budget: float
    remaining_budget: float | None = None
    deadline: str | None = None
    type: str = "normal"
    depth: int = 0
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    content: dict[str, Any] = Field(default_factory=dict)
    bids: list[dict[str, Any]] = Field(default_factory=list)
    results: list[dict[str, Any]] = Field(default_factory=list)
    max_concurrent_bidders: int = 0
    budget_locked: bool = False
    human_contact: HumanContactSchema | None = None


# ── Reject task ──────────────────────────────────────────────────────

class RejectTaskRequest(BaseModel):
    agent_id: str
    reason: str = ""


# ── Bid ──────────────────────────────────────────────────────────────

class SubmitBidRequest(BaseModel):
    agent_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    price: float = Field(ge=0.0)
    server_id: str | None = None


class BidResponse(BaseModel):
    status: str
    task_id: str
    agent_id: str


# ── Result ───────────────────────────────────────────────────────────

class SubmitResultRequest(BaseModel):
    agent_id: str
    content: Any


class SelectResultRequest(BaseModel):
    initiator_id: str
    agent_id: str


# ── Subtask ──────────────────────────────────────────────────────────

class CreateSubtaskRequest(BaseModel):
    initiator_id: str
    content: dict[str, Any] = Field(default_factory=dict)
    domains: list[str] = Field(min_length=1)
    budget: float = Field(ge=0.0)
    deadline: str | None = None


# ── Budget ───────────────────────────────────────────────────────────

class ConfirmBudgetRequest(BaseModel):
    initiator_id: str
    approved: bool
    new_budget: float | None = Field(default=None, ge=0.0)


# ── Close ────────────────────────────────────────────────────────────

class CloseTaskRequest(BaseModel):
    initiator_id: str


# ── Discussions ──────────────────────────────────────────────────────

class UpdateDiscussionsRequest(BaseModel):
    initiator_id: str
    message: str


# ── Deadline ─────────────────────────────────────────────────────────

class UpdateDeadlineRequest(BaseModel):
    initiator_id: str
    deadline: str


# ── Reputation ───────────────────────────────────────────────────────

class ReputationEventRequest(BaseModel):
    agent_id: str
    event_type: str
    server_id: str


class ReputationResponse(BaseModel):
    agent_id: str
    score: float


# ── Generic ──────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    detail: str


class OkResponse(BaseModel):
    ok: bool = True
    message: str = ""


# ── Discovery: Server ───────────────────────────────────────────────

class RegisterServerRequest(BaseModel):
    version: str
    endpoint: str
    owner: str


class RegisterServerResponse(BaseModel):
    server_id: str
    status: str = "online"


class ServerCardResponse(BaseModel):
    server_id: str
    version: str
    endpoint: str
    owner: str
    status: str


# ── Discovery: Agent ────────────────────────────────────────────────

class SkillSchema(BaseModel):
    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class AgentCapabilitiesSchema(BaseModel):
    max_concurrent_tasks: int = 0  # 0 = unlimited
    concurrent: bool = True


class RegisterAgentRequest(BaseModel):
    agent_id: str
    name: str
    agent_type: str  # "executor" | "planner"
    domains: list[str] = Field(min_length=1)
    skills: list[SkillSchema] = Field(min_length=1)
    capabilities: AgentCapabilitiesSchema | None = None
    url: str
    server_id: str
    description: str = ""


class RegisterAgentResponse(BaseModel):
    agent_id: str
    seeds: list[str] = Field(default_factory=list)


class AgentCardResponse(BaseModel):
    agent_id: str
    name: str
    agent_type: str
    domains: list[str]
    skills: list[dict[str, Any]] = Field(default_factory=list)
    url: str
    server_id: str
    network_id: str = ""
    description: str = ""


class UpdateAgentRequest(BaseModel):
    name: str | None = None
    domains: list[str] | None = Field(default=None, min_length=1)
    skills: list[SkillSchema] | None = Field(default=None, min_length=1)
    url: str | None = None
    description: str | None = None


# ── Discovery: Query ────────────────────────────────────────────────

class DiscoverResponse(BaseModel):
    domain: str
    agent_ids: list[str] = Field(default_factory=list)


# ── Economy ────────────────────────────────────────────────────────

class BalanceResponse(BaseModel):
    agent_id: str
    available: float
    frozen: float


class DepositRequest(BaseModel):
    agent_id: str
    amount: float = Field(gt=0.0)


class DepositResponse(BaseModel):
    agent_id: str
    deposited: float
    available: float
    frozen: float
