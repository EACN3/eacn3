"""Integration tests: admin operations (config, scan-deadlines, logs)."""

import pytest


class TestAdminConfig:
    @pytest.mark.asyncio
    async def test_get_config(self, http):
        """Admin can read current config."""
        resp = await http.get("/api/admin/config")
        assert resp.status_code == 200
        config = resp.json()
        # Should have standard config sections
        assert "reputation" in config or "economy" in config or "matcher" in config

    @pytest.mark.asyncio
    async def test_update_config_unknown_key(self, http):
        """Updating unknown config key returns 400."""
        resp = await http.put("/api/admin/config", json={
            "nonexistent_section": {"key": "value"},
        })
        assert resp.status_code == 400


class TestScanDeadlines:
    @pytest.mark.asyncio
    async def test_scan_no_expired(self, http, funded_network):
        """Scanning with no expired tasks returns empty list."""
        resp = await http.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        assert resp.json()["expired"] == []

    @pytest.mark.asyncio
    async def test_scan_expired_task(self, http, funded_network):
        """Task past deadline gets expired by scan."""
        funded_network.escrow.get_or_create_account("scan-init", 5000.0)

        # Create task with past deadline
        resp = await http.post("/api/tasks", json={
            "task_id": "expired-task",
            "initiator_id": "scan-init",
            "content": {"description": "Will expire"},
            "domains": ["coding"],
            "budget": 50.0,
            "deadline": "2020-01-01T00:00:00Z",  # Already past
        })
        assert resp.status_code == 201

        # Scan
        resp = await http.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        expired = resp.json()["expired"]
        assert "expired-task" in expired


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
        """Logs are generated after task operations."""
        funded_network.escrow.get_or_create_account("log-init", 5000.0)

        resp = await http.post("/api/tasks", json={
            "task_id": "logged-task",
            "initiator_id": "log-init",
            "content": {"description": "Log test"},
            "domains": ["coding"],
            "budget": 50.0,
        })
        assert resp.status_code == 201

        # Query logs for this task
        resp = await http.get("/api/admin/logs", params={
            "task_id": "logged-task",
        })
        assert resp.status_code == 200
        logs = resp.json()
        # Should have at least one log entry for task creation
        assert len(logs) >= 1
