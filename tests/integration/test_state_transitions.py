"""Integration tests: state transitions, terminal states, NO_ONE_ABLE refund."""

import pytest

from tests.integration.conftest import is_error


async def _setup(mcp, funded_network):
    """Register agent and fund. Returns nothing."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "State Init",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "plan", "description": "plan"}],
        "agent_id": "st-init",
    })
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": "State Worker",
        "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": "st-worker",
    })
    funded_network.escrow.get_or_create_account("st-init", 10000.0)
    funded_network.reputation._scores["st-init"] = 0.8
    funded_network.reputation._scores["st-worker"] = 0.8


async def _complete_task(mcp, task_id):
    """Drive a task all the way to COMPLETED status."""
    await mcp.call_tool_parsed("eacn3_submit_bid", {
        "task_id": task_id, "agent_id": "st-worker",
        "confidence": 0.9, "price": 80.0,
    })
    await mcp.call_tool_parsed("eacn3_submit_result", {
        "task_id": task_id, "agent_id": "st-worker",
        "content": {"answer": "done"},
    })
    await mcp.call_tool_parsed("eacn3_close_task", {
        "task_id": task_id, "initiator_id": "st-init",
    })
    # collect_results transitions AWAITING_RETRIEVAL → COMPLETED
    await mcp.call_tool_parsed("eacn3_get_task_results", {
        "task_id": task_id, "initiator_id": "st-init",
    })
    await mcp.call_tool_parsed("eacn3_select_result", {
        "task_id": task_id, "agent_id": "st-worker",
        "initiator_id": "st-init",
    })


class TestNoOneAbleRefund:
    @pytest.mark.asyncio
    async def test_close_without_results_refunds_budget(self, mcp, funded_network):
        """Close task with no results → NO_ONE_ABLE, full budget refunded."""
        await _setup(mcp, funded_network)

        before = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "st-init"})
        assert before["available"] == 10000.0

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Will have no results",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Budget frozen
        mid = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "st-init"})
        assert mid["available"] == 9800.0
        assert mid["frozen"] == 200.0

        # Close without any bids or results
        close_result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert close_result["status"] == "no_one_able"

        # Full refund: frozen→available
        after = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "st-init"})
        assert after["available"] == 10000.0
        assert after["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_close_with_results_awaits_retrieval(self, mcp, http, funded_network):
        """Close task with results → AWAITING_RETRIEVAL (not NO_ONE_ABLE)."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Has a result",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "st-worker",
            "content": {"answer": "done"},
        })

        close_result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert close_result["status"] == "awaiting_retrieval"

        # Budget still frozen (not refunded yet, needs select)
        bal = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "st-init"})
        assert bal["frozen"] == 200.0


class TestTerminalStates:
    @pytest.mark.asyncio
    async def test_cannot_bid_on_completed_task(self, mcp, http, funded_network):
        """Bidding on a completed task returns error."""
        await _setup(mcp, funded_network)
        funded_network.escrow.get_or_create_account("st-worker", 0.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Will complete",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]
        await _complete_task(mcp, task_id)

        # Verify completed
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "completed"

        # Try to bid → should fail
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Late Bidder",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "late-bidder",
        })
        funded_network.reputation._scores["late-bidder"] = 0.8

        result = await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id,
            "agent_id": "late-bidder",
            "confidence": 0.9,
            "price": 50.0,
        })
        assert is_error(result), f"Expected error bidding on completed task, got: {result}"

    @pytest.mark.asyncio
    async def test_cannot_close_completed_task(self, mcp, http, funded_network):
        """Closing a completed task returns error."""
        await _setup(mcp, funded_network)
        funded_network.escrow.get_or_create_account("st-worker", 0.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Complete then close",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]
        await _complete_task(mcp, task_id)

        # Try to close again → error
        result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert is_error(result), f"Expected error closing completed task, got: {result}"

    @pytest.mark.asyncio
    async def test_cannot_close_no_one_able_task(self, mcp, funded_network):
        """Closing a NO_ONE_ABLE task returns error."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Will be no_one_able",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        r1 = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert r1["status"] == "no_one_able"

        r2 = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert is_error(r2), f"Expected error closing no_one_able task, got: {r2}"

    @pytest.mark.asyncio
    async def test_cannot_submit_result_on_no_one_able(self, mcp, funded_network):
        """Submitting result to NO_ONE_ABLE task fails."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Dead task",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        # Bid while alive
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 50.0,
        })

        # Close (has bids but no results → no_one_able)
        close = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })
        assert close["status"] == "no_one_able"

        # Submit result on terminal task → fail
        result = await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "st-worker",
            "content": {"answer": "too late"},
        })
        assert is_error(result), f"Expected error submitting to no_one_able task, got: {result}"


class TestCollectResultsTransition:
    @pytest.mark.asyncio
    async def test_collect_transitions_awaiting_to_completed(self, mcp, http, funded_network):
        """First collect_results transitions AWAITING_RETRIEVAL → COMPLETED."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Collect transition",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "st-worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "st-worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "st-init",
        })

        # Before collect: awaiting_retrieval
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "awaiting_retrieval"

        # Collect transitions to completed
        collected = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "st-init",
        })
        assert len(collected["results"]) == 1

        # After collect: completed
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_collect_before_ready_fails(self, mcp, http, funded_network):
        """Collecting results while task is still unclaimed returns 400."""
        await _setup(mcp, funded_network)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Too early",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "st-init",
        })
        task_id = task["task_id"]

        resp = await http.get(
            f"/api/tasks/{task_id}/results",
            params={"initiator_id": "st-init"},
        )
        assert resp.status_code == 400
