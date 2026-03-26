"""Reputation system E2E tests via MCP plugin.

Tests reputation as users actually experience it:
- Check reputation after registration
- Reputation changes after task completion
- Report reputation events
"""

import pytest
from tests.integration.conftest import seed_reputation


async def _reg(mcp, net, aid, *, balance=0.0, rep=None):
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": f"Agent {aid}", "description": "test",
        "domains": ["coding"],
        "skills": [{"name": "work", "description": "work"}],
        "agent_id": aid,
    })
    if rep is not None:
        net.reputation._scores[aid] = rep
    else:
        seed_reputation(net, aid)
    if balance > 0:
        net.escrow.get_or_create_account(aid, balance)


class TestReputationQuery:
    @pytest.mark.asyncio
    async def test_get_reputation_default(self, mcp, http, funded_network):
        """New agent gets default reputation score (0.5)."""
        result = await mcp.call_tool_parsed("eacn3_get_reputation", {
            "agent_id": "unknown-agent",
        })
        assert result["score"] == 0.5

    @pytest.mark.asyncio
    async def test_get_seeded_reputation(self, mcp, http, funded_network):
        """Agent with seeded reputation returns correct score."""
        net = funded_network
        await _reg(mcp, net, "rep-known", rep=0.85)

        result = await mcp.call_tool_parsed("eacn3_get_reputation", {
            "agent_id": "rep-known",
        })
        assert result["score"] == 0.85


class TestReputationAfterWork:
    @pytest.mark.asyncio
    async def test_selection_improves_reputation(self, mcp, http, funded_network):
        """After being selected, executor's reputation increases."""
        net = funded_network
        await _reg(mcp, net, "rep-init", balance=5000.0, rep=0.7)
        await _reg(mcp, net, "rep-worker", rep=0.6)

        before = net.reputation.get_score("rep-worker")

        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Rep test",
            "budget": 200.0,
            "domains": ["coding"],
            "initiator_id": "rep-init",
        })
        tid = task["task_id"]

        await mcp.call_tool_parsed("eacn3_submit_bid", {
            "task_id": tid, "agent_id": "rep-worker",
            "confidence": 0.9, "price": 100.0,
        })
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": tid, "agent_id": "rep-worker",
            "content": {"done": True},
        })
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": tid, "initiator_id": "rep-init",
        })
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": tid, "agent_id": "rep-worker",
            "initiator_id": "rep-init",
        })

        after = net.reputation.get_score("rep-worker")
        assert after > before, f"Reputation should increase: {before} → {after}"


class TestReportEvent:
    @pytest.mark.asyncio
    async def test_report_positive_event(self, mcp, http, funded_network):
        """Reporting a positive event via MCP increases reputation."""
        net = funded_network
        await _reg(mcp, net, "evt-agent", rep=0.5)

        result = await mcp.call_tool_parsed("eacn3_report_event", {
            "agent_id": "evt-agent",
            "event_type": "result_selected",
        })
        # Score should have increased
        new_score = net.reputation.get_score("evt-agent")
        assert new_score > 0.5
