"""Integration tests: admin operations (config, scan-deadlines, logs)."""

import pytest


class TestAdminConfig:
    @pytest.mark.asyncio
    async def test_get_config_has_sections(self, http):
        """Admin config returns all expected config sections."""
        resp = await http.get("/api/admin/config")
        assert resp.status_code == 200
        config = resp.json()
        assert "reputation" in config
        assert "economy" in config
        assert "matcher" in config
        assert "push" in config

    @pytest.mark.asyncio
    async def test_update_config_unknown_key_400(self, http):
        """Updating unknown config key returns 400 with error detail."""
        resp = await http.put("/api/admin/config", json={
            "nonexistent_section": {"key": "value"},
        })
        assert resp.status_code == 400
        assert "nonexistent_section" in resp.json()["detail"]


class TestScanDeadlines:
    @pytest.mark.asyncio
    async def test_scan_no_expired(self, http, funded_network):
        """Scanning with no expired tasks returns empty list."""
        resp = await http.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        assert resp.json()["expired"] == []

    @pytest.mark.asyncio
    async def test_scan_expired_task_transitions(self, http, funded_network):
        """Expired task transitions to NO_ONE_ABLE (no results) after scan."""
        funded_network.escrow.get_or_create_account("scan-init", 5000.0)

        resp = await http.post("/api/tasks", json={
            "task_id": "scan-expired",
            "initiator_id": "scan-init",
            "content": {"description": "Will expire"},
            "domains": ["coding"],
            "budget": 50.0,
            "deadline": "2020-01-01T00:00:00Z",
        })
        assert resp.status_code == 201

        scan = await http.post("/api/admin/scan-deadlines")
        assert scan.status_code == 200
        assert "scan-expired" in scan.json()["expired"]

        # Verify task status changed
        resp = await http.get("/api/tasks/scan-expired")
        assert resp.json()["status"] == "no_one_able"

    @pytest.mark.asyncio
    async def test_scan_expired_with_results_awaits(self, http, funded_network):
        """Expired task WITH results transitions to awaiting_retrieval."""
        funded_network.escrow.get_or_create_account("scan-init2", 5000.0)
        funded_network.reputation._scores["scan-worker"] = 0.8

        # Create task with past deadline
        resp = await http.post("/api/tasks", json={
            "task_id": "scan-has-result",
            "initiator_id": "scan-init2",
            "content": {"description": "Has result before deadline"},
            "domains": ["coding"],
            "budget": 100.0,
            "deadline": "2099-01-01T00:00:00Z",  # Future, will update below
        })
        assert resp.status_code == 201

        # Bid and submit result
        resp = await http.post("/api/tasks/scan-has-result/bid", json={
            "agent_id": "scan-worker",
            "confidence": 0.9,
            "price": 80.0,
        })
        assert resp.status_code == 200

        resp = await http.post("/api/tasks/scan-has-result/result", json={
            "agent_id": "scan-worker",
            "content": {"answer": "done before deadline"},
        })
        assert resp.status_code == 200

        # Now set deadline to past
        resp = await http.put("/api/tasks/scan-has-result/deadline", json={
            "initiator_id": "scan-init2",
            "deadline": "2020-01-01T00:00:00Z",
        })
        assert resp.status_code == 200

        # Scan
        scan = await http.post("/api/admin/scan-deadlines")
        assert "scan-has-result" in scan.json()["expired"]

        # Should be awaiting_retrieval (has results)
        resp = await http.get("/api/tasks/scan-has-result")
        assert resp.json()["status"] == "awaiting_retrieval"


class TestAdminLogs:
    @pytest.mark.asyncio
    async def test_query_logs_empty(self, http):
        """Query logs with no matches returns empty list."""
        resp = await http.get("/api/admin/logs", params={
            "task_id": "nonexistent-task",
        })
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_query_logs_after_task(self, http, funded_network):
        """Task creation generates at least one log entry."""
        funded_network.escrow.get_or_create_account("log-init", 5000.0)

        resp = await http.post("/api/tasks", json={
            "task_id": "logged-task",
            "initiator_id": "log-init",
            "content": {"description": "Log test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201

        resp = await http.get("/api/admin/logs", params={"task_id": "logged-task"})
        assert resp.status_code == 200
        logs = resp.json()
        assert len(logs) >= 1
        # Log entry should reference the task
        assert any(log.get("task_id") == "logged-task" for log in logs)
