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
