"""Push notification event types and structure."""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


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


def _gen_msg_id() -> str:
    return uuid.uuid4().hex


class PushEvent(BaseModel):
    msg_id: str = Field(default_factory=_gen_msg_id)
    type: PushEventType
    task_id: str
    recipients: list[str] = Field(min_length=1)

    @field_validator("recipients")
    @classmethod
    def _recipients_no_empty(cls, v: list[str]) -> list[str]:
        if any(not r.strip() for r in v):
            raise ValueError("Recipient IDs must be non-empty strings")
        return v

    payload: dict[str, Any] = Field(default_factory=dict)
