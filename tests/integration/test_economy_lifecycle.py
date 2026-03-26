"""Economy lifecycle E2E tests via MCP plugin.

Tests the full money flow as a real user experiences it:
- Deposit → create task → agent works → payment → verify balances
- Multiple settlements accumulate correctly
- Escrow detail query shows per-task breakdown
- Refund on cancellation
"""

import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, aid, *, balance=0.0):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {aid}", "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": aid,
    })
    seed_reputation(net, aid)
    if balance > 0:
        net.escrow.get_or_create_account(aid, balance)


class TestDepositAndSpend:
    @pytest.mark.asyncio
    async def test_deposit_create_settle_balance_correct(self, mcp, http, funded_network):
        """Full money flow: deposit → create → bid → result → select → verify."""
        net = funded_network
        await _reg(mcp, net, "eco-init", balance=0.0)
        await _reg(mcp, net, "eco-worker")

        # Deposit funds
        dep = await mcp.call_tool_parsed("eacn3_deposit", {
            "agent_id": "eco-init", "amount": 1000.0,
        })
        assert dep["available"] == 1000.0

        # Create task (freezes 300 from available)
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Paid work",
            "budget": 300.0,
            "domains": ["coding"],
            "initiator_id": "eco-init",
        })
        tid = task["task_id"]

        # Check balance: 700 available, 300 frozen
        bal = await mcp.call_tool_parsed("eacn3_get_balance", {
            "agent_id": "eco-init",
        })
        assert bal["available"] == 700.0
        assert bal["frozen"] == 300.0

        # Worker bids, submits, gets selected
        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "eco-worker",
            "confidence": 0.9, "price": 200.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "eco-worker",
            "content": {"done": True},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "eco-init",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "eco-worker",
            "initiator_id": "eco-init",
        })

        # Worker should have 200
        worker_bal = (await http.get("/api/economy/balance",
                                     params={"agent_id": "eco-worker"})).json()
        assert worker_bal["available"] == 200.0

        # Initiator: 1000 - 300 frozen + refund
        # Settlement deducts 200 + 10 (5% fee) = 210
        # Refund = 300 - 210 = 90
        # Final: 700 + 90 = 790
        init_bal = (await http.get("/api/economy/balance",
                                   params={"agent_id": "eco-init"})).json()
        assert init_bal["available"] == 790.0
        assert init_bal["frozen"] == 0.0


class TestCancelRefund:
    @pytest.mark.asyncio
    async def test_cancel_returns_full_budget(self, mcp, http, funded_network):
        """Cancelling a task without results refunds the full budget."""
        net = funded_network
        await _reg(mcp, net, "ref-init", balance=1000.0)

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Will cancel",
            "budget": 400.0,
            "domains": ["coding"],
            "initiator_id": "ref-init",
        })
        tid = task["task_id"]

        # Before close: 600 available, 400 frozen
        bal1 = (await http.get("/api/economy/balance",
                               params={"agent_id": "ref-init"})).json()
        assert bal1["available"] == 600.0
        assert bal1["frozen"] == 400.0

        # Close without any bids/results → full refund
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "ref-init",
        })

        # After close: 1000 available, 0 frozen
        bal2 = (await http.get("/api/economy/balance",
                               params={"agent_id": "ref-init"})).json()
        assert bal2["available"] == 1000.0
        assert bal2["frozen"] == 0.0


class TestMultipleSettlements:
    @pytest.mark.asyncio
    async def test_3_tasks_settled_earnings_accumulate(self, mcp, http, funded_network):
        """Worker completes 3 tasks — earnings add up correctly."""
        net = funded_network
        await _reg(mcp, net, "ms-init", balance=5000.0)
        await _reg(mcp, net, "ms-worker")

        total_earned = 0.0
        for i in range(3):
            price = 100.0 + i * 50  # 100, 150, 200
            task = await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Task {i}",
                "budget": price + 100,
                "domains": ["coding"],
                "initiator_id": "ms-init",
            })
            tid = task["task_id"]

            await mcp.call_tool_parsed("eacn3_submit_bid", {
                "task_id": tid, "agent_id": "ms-worker",
                "confidence": 0.9, "price": price,
            })
            await mcp.call_tool_parsed("eacn3_submit_result", {
                "task_id": tid, "agent_id": "ms-worker",
                "content": {"task": i},
            })
            await mcp.call_tool_parsed("eacn3_close_task", {
                "task_id": tid, "initiator_id": "ms-init",
            })
            await mcp.call_tool_parsed("eacn3_select_result", {
                "task_id": tid, "agent_id": "ms-worker",
                "initiator_id": "ms-init",
            })
            total_earned += price

        # Worker should have exactly 100 + 150 + 200 = 450
        worker_bal = (await http.get("/api/economy/balance",
                                     params={"agent_id": "ms-worker"})).json()
        assert worker_bal["available"] == total_earned
