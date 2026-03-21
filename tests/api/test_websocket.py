"""Tests: WebSocket push notification delivery.

Covers: WS /ws/{agent_id}
        ConnectionManager: connect, disconnect, send_to, broadcast_event
"""

import asyncio

import pytest

from eacn3.core.models import PushEvent, PushEventType
from eacn3.network.api.websocket import ConnectionManager

class TestConnectionManager:
    @pytest.fixture
    def mgr(self):
        return ConnectionManager()

    def test_initial_state(self, mgr):
        assert mgr.connected_count == 0
        assert mgr.is_connected("a1") is False

    @pytest.mark.asyncio
    async def test_send_to_disconnected_returns_false(self, mgr):
        result = await mgr.send_to("a1", {"test": True})
        assert result is False

    @pytest.mark.asyncio
    async def test_broadcast_no_connections(self, mgr):
        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["a1", "a2"],
            payload={"budget": 100},
        )
        delivered = await mgr.broadcast_event(event)
        assert delivered == 0

    @pytest.mark.asyncio
    async def test_broadcast_single_recipient_not_connected(self, mgr):
        event = PushEvent(
            type=PushEventType.TASK_BROADCAST,
            task_id="t1",
            recipients=["nobody"],
            payload={},
        )
        delivered = await mgr.broadcast_event(event)
        assert delivered == 0

class TestPushEventTypes:
    """Verify all event types can be serialized for WS delivery."""

    @pytest.mark.parametrize("event_type", list(PushEventType))
    def test_event_type_serializable(self, event_type):
        event = PushEvent(
            type=event_type,
            task_id="t1",
            recipients=["a1"],
            payload={"key": "value"},
        )
        payload = {
            "type": event.type.value,
            "task_id": event.task_id,
            "payload": event.payload,
        }
        assert isinstance(payload["type"], str)
        assert payload["task_id"] == "t1"

    def test_all_push_event_types_exist(self):
        expected = {
            "task_broadcast", "bid_request_confirmation", "bid_result",
            "discussion_update", "subtask_completed", "task_collected",
            "task_timeout", "adjudication_task",
        }
        actual = {e.value for e in PushEventType}
        assert expected == actual
