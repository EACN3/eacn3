"""Integration tests: concurrent bidder slots (WAITING, promote, budget_locked)."""

import pytest


async def _setup(mcp, funded_network, max_concurrent=1):
    """Create task with specific max_concurrent_bidders. Returns task_id."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "Slot Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "slot-init",
        "agent_type": "planner",
    })
    funded_network.escrow.get_or_create_account("slot-init", 10000.0)
    funded_network.reputation._scores["slot-init"] = 0.8

    # Register multiple executors
    for i in range(3):
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": f"Slot Worker {i}",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": f"slot-w{i}",
        })
        funded_network.reputation._scores[f"slot-w{i}"] = 0.8

    task = await mcp.call_tool_parsed("eacn_create_task", {
        "description": "Slot test",
        "budget": 500.0,
        "domains": ["coding"],
        "initiator_id": "slot-init",
        "max_concurrent_bidders": max_concurrent,
    })
    return task["task_id"]


class TestWaitingQueue:
    @pytest.mark.asyncio
    async def test_second_bid_waits_when_slot_full(self, mcp, http, funded_network):
        """With max_concurrent=1, second bid gets WAITING status."""
        task_id = await _setup(mcp, funded_network, max_concurrent=1)

        bid1 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "slot-w0",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert bid1["status"] == "executing"

        bid2 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "slot-w1",
            "confidence": 0.85,
            "price": 70.0,
        })
        assert bid2["status"] == "waiting"

        # Verify on network: one executing, one waiting
        resp = await http.get(f"/api/tasks/{task_id}")
        bids = resp.json()["bids"]
        w0_bid = next(b for b in bids if b["agent_id"] == "slot-w0")
        w1_bid = next(b for b in bids if b["agent_id"] == "slot-w1")
        assert w0_bid["status"] == "executing"
        assert w1_bid["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_budget_locked_when_slots_full(self, mcp, http, funded_network):
        """budget_locked becomes True when all concurrent slots are filled."""
        task_id = await _setup(mcp, funded_network, max_concurrent=1)

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "slot-w0",
            "confidence": 0.9,
            "price": 80.0,
        })

        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["budget_locked"] is True

    @pytest.mark.asyncio
    async def test_reject_promotes_waiting_to_executing(self, mcp, http, funded_network):
        """When executing agent rejects, next waiting agent gets promoted."""
        task_id = await _setup(mcp, funded_network, max_concurrent=1)

        bid1 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "slot-w0",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert bid1["status"] == "executing"

        bid2 = await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "slot-w1",
            "confidence": 0.85,
            "price": 70.0,
        })
        assert bid2["status"] == "waiting"

        # slot-w0 rejects → slot-w1 should be promoted
        reject = await mcp.call_tool_parsed("eacn_reject_task", {
            "task_id": task_id,
            "agent_id": "slot-w0",
        })
        assert reject["ok"] is True

        # Verify slot-w1 is now executing
        resp = await http.get(f"/api/tasks/{task_id}")
        bids = resp.json()["bids"]
        w0_bid = next(b for b in bids if b["agent_id"] == "slot-w0")
        w1_bid = next(b for b in bids if b["agent_id"] == "slot-w1")
        assert w0_bid["status"] == "rejected"
        assert w1_bid["status"] == "executing"


class TestAutoCollect:
    @pytest.mark.asyncio
    async def test_auto_collect_triggers_at_max_results(self, mcp, http, funded_network):
        """When all slots submit results, task auto-transitions to awaiting_retrieval."""
        task_id = await _setup(mcp, funded_network, max_concurrent=2)

        # Two agents bid and get slots
        for i in range(2):
            bid = await mcp.call_tool_parsed("eacn_submit_bid", {
                "task_id": task_id,
                "agent_id": f"slot-w{i}",
                "confidence": 0.9,
                "price": 80.0,
            })
            assert bid["status"] == "executing"

        # Both submit results
        for i in range(2):
            result = await mcp.call_tool_parsed("eacn_submit_result", {
                "task_id": task_id,
                "agent_id": f"slot-w{i}",
                "content": {"answer": f"result-{i}"},
            })
            assert result["ok"] is True

        # Task should auto-transition to awaiting_retrieval
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "awaiting_retrieval"

        # Initiator can collect without closing
        collected = await mcp.call_tool_parsed("eacn_get_task_results", {
            "task_id": task_id,
            "initiator_id": "slot-init",
        })
        assert len(collected["results"]) == 2

    @pytest.mark.asyncio
    async def test_partial_results_no_auto_collect(self, mcp, http, funded_network):
        """When only some slots submit, task stays in bidding."""
        task_id = await _setup(mcp, funded_network, max_concurrent=2)

        for i in range(2):
            await mcp.call_tool_parsed("eacn_submit_bid", {
                "task_id": task_id,
                "agent_id": f"slot-w{i}",
                "confidence": 0.9,
                "price": 80.0,
            })

        # Only first submits
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "slot-w0",
            "content": {"answer": "partial"},
        })

        # Task should still be bidding (not enough results)
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "bidding"
