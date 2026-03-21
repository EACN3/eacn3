"""Integration tests: economy system (balance, deposit, freeze, settle)."""

import pytest


class TestEconomy:
    @pytest.mark.asyncio
    async def test_get_balance(self, mcp, funded_network):
        """Query balance of a funded account."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Balance Agent",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "bal-agent",
        })
        funded_network.escrow.get_or_create_account("bal-agent", 1000.0)

        result = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "bal-agent",
        })
        assert result["agent_id"] == "bal-agent"
        assert result["available"] == 1000.0
        assert result["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_deposit(self, mcp, funded_network):
        """Deposit increases available balance."""
        funded_network.escrow.get_or_create_account("dep-agent", 100.0)

        result = await mcp.call_tool_parsed("eacn3_deposit", {
            "agent_id": "dep-agent",
            "amount": 500.0,
        })
        assert result["deposited"] == 500.0
        assert result["available"] == 600.0

    @pytest.mark.asyncio
    async def test_budget_freeze_on_task_creation(self, mcp, http, funded_network):
        """Creating a task freezes budget from initiator's account."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Freeze Test",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "freeze-init",
        })
        funded_network.escrow.get_or_create_account("freeze-init", 1000.0)

        # Check balance before
        before = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "freeze-init",
        })
        assert before["available"] == 1000.0
        assert before["frozen"] == 0.0

        # Create task with budget=200
        await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Freeze test task",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "freeze-init",
        })

        # Check balance after — 200 should be frozen
        after = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "freeze-init",
        })
        assert after["available"] == 800.0
        assert after["frozen"] == 200.0

    @pytest.mark.asyncio
    async def test_settlement_after_select(self, mcp, http, funded_network):
        """After select_result, executor gets paid, initiator's frozen decreases."""
        # Setup agents
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Payer", "description": "test", "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "payer",
        })
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Worker", "description": "test", "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "worker",
        })
        funded_network.escrow.get_or_create_account("payer", 5000.0)
        funded_network.escrow.get_or_create_account("worker", 0.0)
        funded_network.reputation._scores["payer"] = 0.8
        funded_network.reputation._scores["worker"] = 0.8

        # Full cycle
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Settlement test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "payer",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "worker",
            "confidence": 0.9, "price": 80.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "worker",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "payer",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id, "agent_id": "worker",
            "initiator_id": "payer",
        })

        # Worker should have gotten paid
        worker_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "worker",
        })
        assert worker_bal["available"] > 0

        # Payer's frozen should have decreased
        payer_bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "payer",
        })
        assert payer_bal["frozen"] == 0.0
