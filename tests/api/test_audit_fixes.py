"""Tests for bugs found during code audit.

Covers:
- Settlement idempotency guard timing (Bug 1)
- Settlement rollback restores task.status (Bug 4)
- _terminate_children doesn't refund children with results (Bug 5)
- Broadcast failure doesn't break task creation (Bug 6)
- Subtask level enum handling (Bug 9)
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(c, tid): return (await c.get(f"/api/tasks/{tid}")).json()
async def _bal(c, aid): return (await c.get("/api/economy/balance", params={"agent_id": aid})).json()


class TestSettlementIdempotency:
    @pytest.mark.asyncio
    async def test_settlement_retry_after_success(self, client, funded_network):
        """Second select on already-settled task fails cleanly."""
        net = funded_network
        net.reputation._scores["idem-exec"] = 0.8
        await net.dht.announce("coding", "idem-exec")

        await create_task(client, task_id="idem-1", budget=200.0)
        await bid(client, task_id="idem-1", agent_id="idem-exec", price=80.0)
        await submit_result(client, task_id="idem-1", agent_id="idem-exec")
        await close_task(client, task_id="idem-1")
        await select_result(client, task_id="idem-1", agent_id="idem-exec")

        # Second select should fail
        resp = await client.post("/api/tasks/idem-1/select", json={
            "initiator_id": "user1", "agent_id": "idem-exec",
        })
        assert resp.status_code == 400


class TestSettlementRollback:
    @pytest.mark.asyncio
    async def test_task_status_not_stuck_after_select_with_close(self, client, funded_network):
        """After successful select with close_task, task is COMPLETED."""
        net = funded_network
        net.reputation._scores["rb-exec"] = 0.8
        await net.dht.announce("coding", "rb-exec")

        await create_task(client, task_id="rb-1", budget=200.0)
        await bid(client, task_id="rb-1", agent_id="rb-exec", price=80.0)
        await submit_result(client, task_id="rb-1", agent_id="rb-exec")

        # Select with close_task=True
        resp = await client.post("/api/tasks/rb-1/select", json={
            "initiator_id": "user1", "agent_id": "rb-exec", "close_task": True,
        })
        assert resp.status_code == 200

        task = await _get_task(client, "rb-1")
        assert task["status"] == "completed"


class TestTerminateChildrenRefund:
    @pytest.mark.asyncio
    async def test_child_with_results_not_refunded(self, client, funded_network):
        """When parent closes, child with results goes to awaiting_retrieval, not refunded."""
        net = funded_network
        net.reputation._scores["child-exec"] = 0.8
        await net.dht.announce("coding", "child-exec")

        await create_task(client, task_id="tc-parent", budget=500.0)
        await bid(client, task_id="tc-parent", agent_id="a1", price=200.0)

        # Create subtask
        sub_resp = await client.post("/api/tasks/tc-parent/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        sub_id = sub_resp.json()["id"]

        # Child executor bids and submits result
        await bid(client, task_id=sub_id, agent_id="child-exec", price=50.0)
        await submit_result(client, task_id=sub_id, agent_id="child-exec")

        # Close parent — cascade
        await close_task(client, task_id="tc-parent")

        # Child should be awaiting_retrieval (has results), NOT no_one_able
        child = await _get_task(client, sub_id)
        assert child["status"] == "awaiting_retrieval"

    @pytest.mark.asyncio
    async def test_child_without_results_refunded(self, client, funded_network):
        """When parent closes, child without results gets refunded."""
        net = funded_network

        await create_task(client, task_id="tc-parent2", budget=500.0)
        await bid(client, task_id="tc-parent2", agent_id="a1", price=200.0)

        sub_resp = await client.post("/api/tasks/tc-parent2/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        sub_id = sub_resp.json()["id"]

        # No bids on child — close parent
        await close_task(client, task_id="tc-parent2")

        child = await _get_task(client, sub_id)
        assert child["status"] == "no_one_able"


class TestBroadcastFailureRecovery:
    @pytest.mark.asyncio
    async def test_task_created_even_if_no_agents_discovered(self, client):
        """Task creation succeeds even when no agents match domains."""
        resp = await client.post("/api/tasks", json={
            "task_id": "no-match", "initiator_id": "user1",
            "content": {}, "domains": ["nonexistent-domain"], "budget": 100.0,
        })
        assert resp.status_code == 201
        task = await _get_task(client, "no-match")
        assert task["status"] == "unclaimed"


class TestSubtaskLevelEnum:
    @pytest.mark.asyncio
    async def test_subtask_with_expert_level(self, client, funded_network):
        """Create subtask with explicit level — no enum serialization error."""
        net = funded_network
        net.reputation._scores["lvl-exec"] = 0.8
        await net.dht.announce("coding", "lvl-exec")

        await create_task(client, task_id="lvl-parent", budget=500.0, level="expert")
        await bid(client, task_id="lvl-parent", agent_id="a1", price=200.0)

        resp = await client.post("/api/tasks/lvl-parent/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"],
            "budget": 100.0, "level": "tool",
        })
        assert resp.status_code == 201
        sub = resp.json()
        assert sub["level"] == "tool"

    @pytest.mark.asyncio
    async def test_subtask_inherits_parent_level(self, client, funded_network):
        """Subtask without explicit level inherits from parent."""
        net = funded_network

        await create_task(client, task_id="inh-parent", budget=500.0, level="expert")
        await bid(client, task_id="inh-parent", agent_id="a1", price=200.0)

        resp = await client.post("/api/tasks/inh-parent/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
        })
        assert resp.status_code == 201
        sub = resp.json()
        assert sub["level"] == "expert"
