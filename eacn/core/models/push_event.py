"""Push notification event types and structure."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PushEventType(str, Enum):
    TASK_BROADCAST = "task_broadcast"
    BID_REQUEST_CONFIRMATION = "bid_request_confirmation"
    BID_RESULT = "bid_result"
    DISCUSSION_UPDATE = "discussion_update"
    SUBTASK_COMPLETED = "subtask_completed"
    TASK_COLLECTED = "task_collected"
    TASK_TIMEOUT = "task_timeout"
    ADJUDICATION_TASK = "adjudication_task"
    DIRECT_MESSAGE = "direct_message"


class PushEvent(BaseModel):
    type: PushEventType
    task_id: str
    recipients: list[str] = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
