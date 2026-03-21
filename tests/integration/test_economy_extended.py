"""Integration tests: economy edge cases and admin operations."""

import pytest

from tests.integration.conftest import is_error


class TestBalanceEdgeCases:
    @pytest.mark.asyncio
    async def test_nonexistent_agent_returns_error(self, mcp):
        """Querying balance for non-existent agent returns error with 404."""
        result = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "no-such-agent",
        })
        assert is_error(result), f"Expected error for non-existent agent, got: {result}"

    @pytest.mark.asyncio
    async def test_zero_balance_account(self, mcp, funded_network):
        """Account with zero balance reports exact 0.0 for both fields."""
        funded_network.escrow.get_or_create_account("zero-agent", 0.0)

        result = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "zero-agent",
        })
        assert result["agent_id"] == "zero-agent"
        assert result["available"] == 0.0
        assert result["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_multiple_deposits_accumulate_exactly(self, mcp, funded_network):
        """Multiple deposits sum to exact expected total."""
        funded_network.escrow.get_or_create_account("multi-dep", 100.0)

        r1 = await mcp.call_tool_parsed("eacn3_deposit", {
            "agent_id": "multi-dep", "amount": 200.0,
        })
        assert r1["deposited"] == 200.0
        assert r1["available"] == 300.0
        assert r1["frozen"] == 0.0

        r2 = await mcp.call_tool_parsed("eacn3_deposit", {
            "agent_id": "multi-dep", "amount": 300.0,
        })
        assert r2["deposited"] == 300.0
        assert r2["available"] == 600.0
        assert r2["frozen"] == 0.0


class TestFreezeAccounting:
    @pytest.mark.asyncio
    async def test_multiple_tasks_freeze_exactly(self, mcp, funded_network):
        """Creating 3 tasks × 200 budget → 600 frozen, 400 available."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Multi Freeze",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "mf-init",
        })
        funded_network.escrow.get_or_create_account("mf-init", 1000.0)

        for i in range(3):
            task = await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Freeze test {i}",
                "budget": 200.0,
                "domains": ["coding"],
                "initiator_id": "mf-init",
            })
            assert "task_id" in task

        result = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "mf-init"})
        assert result["available"] == 400.0
        assert result["frozen"] == 600.0


class TestSettlementAccounting:
    @pytest.mark.asyncio
    async def test_settlement_exact_amounts(self, mcp, funded_network):
        """After settlement: executor gets bid_price, initiator's frozen clears, refund correct."""
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Payer",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "sett-payer",
        })
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "Earner",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "sett-earner",
        })
        funded_network.escrow.get_or_create_account("sett-payer", 5000.0)
        funded_network.escrow.get_or_create_account("sett-earner", 0.0)
        funded_network.reputation._scores["sett-payer"] = 0.8
        funded_network.reputation._scores["sett-earner"] = 0.8

        # Budget = 200, bid price = 150
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Settlement amount test",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "sett-payer",
        })
        task_id = task["task_id"]

        # After creation: payer has 4800 available, 200 frozen
        payer_mid = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "sett-payer"})
        assert payer_mid["available"] == 4800.0
        assert payer_mid["frozen"] == 200.0

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": task_id, "agent_id": "sett-earner",
            "confidence": 0.9, "price": 150.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id, "agent_id": "sett-earner",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id, "initiator_id": "sett-payer",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id, "agent_id": "sett-earner",
            "initiator_id": "sett-payer",
        })

        # Settlement: deduct 150 + fee(5%) = 157.5 from escrow(200)
        # Refund: 200 - 157.5 = 42.5 back to payer
        earner_bal = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "sett-earner"})
        assert earner_bal["available"] == 150.0  # Earner gets bid_price
        assert earner_bal["frozen"] == 0.0

        payer_bal = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "sett-payer"})
        assert payer_bal["frozen"] == 0.0  # All unfrozen
        # 5000 - 200(frozen) + 42.5(refund) = 4842.5
        assert payer_bal["available"] == pytest.approx(4842.5, abs=0.01)


class TestAdminFund:
    @pytest.mark.asyncio
    async def test_admin_fund_new_account(self, http, funded_network):
        """Admin fund creates account and credits exact amount."""
        resp = await http.post("/api/admin/fund", json={
            "agent_id": "admin-funded",
            "amount": 1000.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "admin-funded"
        assert data["available"] == 1000.0
        assert data["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_admin_fund_adds_to_existing(self, http, funded_network):
        """Admin fund adds to existing balance."""
        funded_network.escrow.get_or_create_account("existing-fund", 500.0)

        resp = await http.post("/api/admin/fund", json={
            "agent_id": "existing-fund",
            "amount": 300.0,
        })
        assert resp.status_code == 200
        assert resp.json()["available"] == 800.0
        assert resp.json()["frozen"] == 0.0
