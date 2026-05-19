"""AgentCard and Skill data models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AgentTier(str, Enum):
    GENERAL = "general"
    EXPERT = "expert"
    EXPERT_GENERAL = "expert_general"
    TOOL = "tool"


class Skill(BaseModel):
    name: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)


class AgentCapabilities(BaseModel):
    max_concurrent_tasks: int = 0  # 0 = unlimited
    concurrent: bool = True


class TeamMembership(BaseModel):
    """Agent's membership in a collaborative team.

    `role` is an optional free-form label naming what this agent does within
    the team (e.g. "lead", "critic", "biology_expert"). `None` means plain
    membership with no declared responsibility. The network does not interpret
    role values; orchestrators and task creators decide their semantics.
    """
    team_id: str
    role: str | None = None


class AgentCard(BaseModel):
    agent_id: str
    name: str
    domains: list[str] = Field(min_length=1)
    skills: list[Skill] = Field(min_length=1)
    capabilities: AgentCapabilities | None = None
    url: str
    server_id: str
    network_id: str = ""
    description: str = ""
    tier: AgentTier = AgentTier.GENERAL
    teams: list[TeamMembership] = Field(default_factory=list)
