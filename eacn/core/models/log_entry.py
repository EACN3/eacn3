"""Structured log entry for @log_event middleware."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LogEntry(BaseModel):
    fn_name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    timestamp: str  # ISO 8601
    error: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    server_id: str | None = None
