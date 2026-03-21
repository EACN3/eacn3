"""Integration tests: economy edge cases and admin operations."""

import pytest


class TestBalanceEdgeCases:
    @pytest.mark.asyncio
    async def test_balance_nonexistent_agent(self, mcp):
        """Querying balance for non-existent agent returns error."""
        result = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "no-such-agent",
        })
        err = result.get("error") or result.get("raw", "")
        assert "404" in str(err) or "not found" in str(err).lower()

    @pytest.mark.asyncio
    async def test_zero_balance_account(self, mcp, funded_network):
        """Account with zero balance reports 0 available and 0 frozen."""
        funded_network.escrow.get_or_create_account("zero-agent", 0.0)

        result = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "zero-agent",
        })
        assert result["available"] == 0.0
        assert result["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_multiple_deposits_accumulate(self, mcp, funded_network):
        """Multiple deposits sum correctly."""
        funded_network.escrow.get_or_create_account("multi-dep", 100.0)

        await mcp.call_tool_parsed("eacn_deposit", {
            "agent_id": "multi-dep",
            "amount": 200.0,
        })
        await mcp.call_tool_parsed("eacn_deposit", {
            "agent_id": "multi-dep",
            "amount": 300.0,
        })

        result = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "multi-dep",
        })
        assert result["available"] == 600.0  # 100 + 200 + 300


class TestFreezeAndSettle:
    @pytest.mark.asyncio
    async def test_multiple_tasks_freeze(self, mcp, funded_network):
        """Creating multiple tasks freezes budget for each."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Multi Freeze",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "mf-init",
        })
        funded_network.escrow.get_or_create_account("mf-init", 1000.0)

        # Create 3 tasks with 200 budget each → 600 frozen
        for i in range(3):
            await mcp.call_tool_parsed("eacn_create_task", {
                "description": f"Freeze test {i}",
                "budget": 200.0,
                "domains": ["coding"],
                "initiator_id": "mf-init",
            })

        result = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "mf-init",
        })
        assert result["available"] == 400.0  # 1000 - 600
        assert result["frozen"] == 600.0

    @pytest.mark.asyncio
    async def test_settlement_pays_correct_amount(self, mcp, funded_network):
        """After settlement, executor receives bid price (minus platform fee if any)."""
        await mcp.call_tool_parsed("eacn_register_agent", {
            "name": "Payer",
            "description": "test",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "code"}],
            "agent_id": "sett-payer",
        })
        await mcp.call_tool_parsed("eacn_register_agent", {
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

        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Settlement amount test",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "sett-payer",
        })
        task_id = task["task_id"]

        await mcp.call_tool_parsed("eacn_submit_bid", {
            "task_id": task_id,
            "agent_id": "sett-earner",
            "confidence": 0.9,
            "price": 150.0,
        })
        await mcp.call_tool_parsed("eacn_submit_result", {
            "task_id": task_id,
            "agent_id": "sett-earner",
            "content": {"answer": "done"},
        })
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "sett-payer",
        })
        await mcp.call_tool_parsed("eacn_select_result", {
            "task_id": task_id,
            "agent_id": "sett-earner",
            "initiator_id": "sett-payer",
        })

        # Earner should have ≈150 (minus possible platform fee)
        earner_bal = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "sett-earner",
        })
        assert earner_bal["available"] > 0
        # Should be close to bid price
        assert earner_bal["available"] >= 100.0  # At least most of 150

        # Payer's frozen should decrease, unused budget returned
        payer_bal = await mcp.call_tool_parsed("eacn_get_balance", {
            "agent_id": "sett-payer",
        })
        assert payer_bal["frozen"] == 0.0
        # Available should be 5000 - 200 (frozen) + 50 (refund of unused budget) - fee
        assert payer_bal["available"] > 4700.0


class TestAdminFund:
    @pytest.mark.asyncio
    async def test_admin_fund_account(self, http, funded_network):
        """Admin fund endpoint credits an account."""
        resp = await http.post("/api/admin/fund", json={
            "agent_id": "admin-funded",
            "amount": 1000.0,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "admin-funded"
        assert data["available"] == 1000.0

    @pytest.mark.asyncio
    async def test_admin_fund_existing_account(self, http, funded_network):
        """Admin fund adds to existing balance."""
        funded_network.escrow.get_or_create_account("existing-fund", 500.0)

        resp = await http.post("/api/admin/fund", json={
            "agent_id": "existing-fund",
            "amount": 300.0,
        })
        assert resp.status_code == 200
        assert resp.json()["available"] == 800.0
