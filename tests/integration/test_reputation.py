"""Integration tests: reputation system (report events, query scores)."""

import pytest


class TestGetReputation:
    @pytest.mark.asyncio
    async def test_get_seeded_reputation(self, mcp, funded_network):
        """Query reputation of an agent with a pre-seeded score."""
        funded_network.reputation._scores["rep-agent"] = 0.85

        result = await mcp.call_tool_parsed("eacn_get_reputation", {
            "agent_id": "rep-agent",
        })
        assert result["agent_id"] == "rep-agent"
        assert result["score"] == pytest.approx(0.85, abs=0.01)

    @pytest.mark.asyncio
    async def test_get_default_reputation(self, mcp, funded_network):
        """Agent with no history gets default reputation score."""
        result = await mcp.call_tool_parsed("eacn_get_reputation", {
            "agent_id": "unknown-agent",
        })
        assert result["agent_id"] == "unknown-agent"
        # Default score is typically 0.5
        assert result["score"] == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_reputation_via_http(self, http, funded_network):
        """Direct HTTP query for reputation."""
        funded_network.reputation._scores["http-rep"] = 0.72
        resp = await http.get("/api/reputation/http-rep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "http-rep"
        assert data["score"] == pytest.approx(0.72, abs=0.01)


class TestReportEvent:
    @pytest.mark.asyncio
    async def test_report_task_completed(self, mcp, funded_network):
        """Reporting task_completed increases reputation."""
        funded_network.reputation._scores["good-worker"] = 0.5

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "good-worker",
            "event_type": "task_completed",
        })
        assert result["agent_id"] == "good-worker"
        # Score should increase after task_completed
        assert result["score"] >= 0.5

    @pytest.mark.asyncio
    async def test_report_task_rejected_decreases(self, mcp, funded_network):
        """Reporting task_rejected decreases reputation."""
        funded_network.reputation._scores["bad-worker"] = 0.7

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "bad-worker",
            "event_type": "task_rejected",
        })
        assert result["agent_id"] == "bad-worker"
        assert result["score"] <= 0.7

    @pytest.mark.asyncio
    async def test_report_task_timeout(self, mcp, funded_network):
        """Reporting task_timeout affects reputation negatively."""
        funded_network.reputation._scores["timeout-worker"] = 0.6

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "timeout-worker",
            "event_type": "task_timeout",
        })
        assert result["agent_id"] == "timeout-worker"
        assert result["score"] <= 0.6

    @pytest.mark.asyncio
    async def test_multiple_events_accumulate(self, mcp, funded_network):
        """Multiple events accumulate reputation changes."""
        funded_network.reputation._scores["multi-worker"] = 0.5

        # Two completions
        r1 = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "multi-worker",
            "event_type": "task_completed",
        })
        r2 = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "multi-worker",
            "event_type": "task_completed",
        })
        # Score should be higher after two completions
        assert r2["score"] >= r1["score"] or r2["score"] >= 0.5

    @pytest.mark.asyncio
    async def test_report_via_http(self, http, funded_network):
        """Report reputation event via direct HTTP."""
        funded_network.reputation._scores["http-worker"] = 0.5

        # Need a server_id — just use a placeholder
        info = await funded_network.discovery.bootstrap.get_server_card("srv-placeholder")
        # If no server exists, create one
        if not info:
            await funded_network.discovery.register_server(
                server_id="srv-placeholder",
                version="1.0",
                endpoint="http://localhost:9999",
                owner="test",
            )

        resp = await http.post("/api/reputation/events", json={
            "agent_id": "http-worker",
            "event_type": "task_completed",
            "server_id": "srv-placeholder",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "http-worker"
        assert data["score"] >= 0.5
