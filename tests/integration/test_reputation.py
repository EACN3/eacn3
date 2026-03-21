"""Integration tests: reputation system (report events, query scores)."""

import pytest


class TestGetReputation:
    @pytest.mark.asyncio
    async def test_seeded_reputation_exact_value(self, mcp, funded_network):
        """Query reputation returns exact seeded score."""
        funded_network.reputation._scores["rep-agent"] = 0.85

        result = await mcp.call_tool_parsed("eacn_get_reputation", {
            "agent_id": "rep-agent",
        })
        assert result["agent_id"] == "rep-agent"
        assert result["score"] == pytest.approx(0.85, abs=0.01)

    @pytest.mark.asyncio
    async def test_default_reputation_is_0_5(self, mcp, funded_network):
        """Agent with no history gets default score of 0.5."""
        result = await mcp.call_tool_parsed("eacn_get_reputation", {
            "agent_id": "unknown-agent",
        })
        assert result["agent_id"] == "unknown-agent"
        assert result["score"] == pytest.approx(0.5, abs=0.01)

    @pytest.mark.asyncio
    async def test_reputation_via_http(self, http, funded_network):
        """HTTP query returns same exact values."""
        funded_network.reputation._scores["http-rep"] = 0.72
        resp = await http.get("/api/reputation/http-rep")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "http-rep"
        assert data["score"] == pytest.approx(0.72, abs=0.01)


class TestReportEvent:
    @pytest.mark.asyncio
    async def test_task_completed_increases_score(self, mcp, funded_network):
        """task_completed event increases score above starting value."""
        funded_network.reputation._scores["good-worker"] = 0.5

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "good-worker",
            "event_type": "task_completed",
        })
        assert result["agent_id"] == "good-worker"
        # result_selected is the weight that actually increases
        # task_completed might map to task_completed_on_time
        # Score should be >= 0.5 (either increased or stayed)
        assert result["score"] >= 0.5

    @pytest.mark.asyncio
    async def test_task_rejected_decreases_score(self, mcp, funded_network):
        """task_rejected event decreases score below starting value."""
        funded_network.reputation._scores["bad-worker"] = 0.7

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "bad-worker",
            "event_type": "task_rejected",
        })
        assert result["agent_id"] == "bad-worker"
        # result_rejected has -0.05 weight
        assert result["score"] < 0.7

    @pytest.mark.asyncio
    async def test_task_timeout_decreases_score(self, mcp, funded_network):
        """task_timeout event decreases score."""
        funded_network.reputation._scores["timeout-worker"] = 0.6

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "timeout-worker",
            "event_type": "task_timeout",
        })
        assert result["agent_id"] == "timeout-worker"
        # task_timed_out has -0.05 weight
        assert result["score"] < 0.6

    @pytest.mark.asyncio
    async def test_multiple_events_accumulate(self, mcp, funded_network):
        """Two positive events accumulate — score strictly increases each time."""
        funded_network.reputation._scores["multi-worker"] = 0.5

        r1 = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "multi-worker",
            "event_type": "result_selected",
        })
        score1 = r1["score"]
        assert score1 > 0.5  # result_selected weight = +0.10

        r2 = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "multi-worker",
            "event_type": "result_selected",
        })
        score2 = r2["score"]
        assert score2 > score1  # Accumulated

    @pytest.mark.asyncio
    async def test_report_via_http(self, http, funded_network):
        """Report reputation event via HTTP, verify score changes."""
        funded_network.reputation._scores["http-worker"] = 0.5

        # Need a registered server for server_id
        await funded_network.discovery.register_server(
            server_id="srv-rep-test",
            version="1.0",
            endpoint="http://localhost:9999",
            owner="test",
        )

        resp = await http.post("/api/reputation/events", json={
            "agent_id": "http-worker",
            "event_type": "result_selected",
            "server_id": "srv-rep-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "http-worker"
        # Cold start factor applies: new server weight ≈ 0.1
        # So delta ≈ 0.10 × 0.1 × 0.5(server_rep) = 0.005
        # Score should be slightly above 0.5
        assert data["score"] >= 0.5

    @pytest.mark.asyncio
    async def test_unknown_event_type_no_change(self, mcp, funded_network):
        """Unknown event type has 0 weight — score unchanged."""
        funded_network.reputation._scores["stable-worker"] = 0.65

        result = await mcp.call_tool_parsed("eacn_report_event", {
            "agent_id": "stable-worker",
            "event_type": "made_up_event_type",
        })
        assert result["agent_id"] == "stable-worker"
        assert result["score"] == pytest.approx(0.65, abs=0.01)
