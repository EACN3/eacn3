"""AgentCard, AgentType, and Skill data models."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class AgentType(str, Enum):
    EXECUTOR = "executor"
    PLANNER = "planner"


class Skill(BaseModel):
    name: str
    description: str = ""
    parameters: dict = Field(default_factory=dict)


class AgentCard(BaseModel):
    agent_id: str
    name: str
    agent_type: AgentType
    domains: list[str] = Field(min_length=1)
    skills: list[Skill] = Field(min_length=1)
    url: str
    server_id: str
    network_id: str = ""
    description: str = ""
