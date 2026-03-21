"""Integration tests: task query operations (get, list, filter, open)."""

import pytest


async def _setup_agent(mcp, funded_network, agent_id="query-init"):
    """Register agent + fund for task creation."""
    await mcp.call_tool_parsed("eacn_register_agent", {
        "name": "Query Agent",
        "description": "test",
        "domains": ["coding", "design"],
        "skills": [{"name": "code", "description": "code"}],
        "agent_id": agent_id,
    })
    funded_network.escrow.get_or_create_account(agent_id, 10000.0)
    funded_network.reputation._scores[agent_id] = 0.8


class TestGetTask:
    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, mcp):
        """Getting a task that doesn't exist returns error."""
        result = await mcp.call_tool_parsed("eacn_get_task", {
            "task_id": "nonexistent-task-xyz",
        })
        err = result.get("error") or result.get("raw", "")
        assert "404" in str(err) or "not found" in str(err).lower()

    @pytest.mark.asyncio
    async def test_get_task_details(self, mcp, funded_network):
        """eacn_get_task returns full task details."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Detail test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]

        result = await mcp.call_tool_parsed("eacn_get_task", {"task_id": task_id})
        assert result["id"] == task_id
        assert result["budget"] == 100.0
        assert "coding" in result["domains"]
        assert result["initiator_id"] == "query-init"

    @pytest.mark.asyncio
    async def test_get_task_status_initiator_only(self, mcp, http, funded_network):
        """eacn_get_task_status requires initiator_id match (403 for others)."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Status auth test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]

        # Correct initiator
        resp = await http.get(f"/api/tasks/{task_id}/status", params={"agent_id": "query-init"})
        assert resp.status_code == 200
        assert resp.json()["status"] in ("unclaimed", "bidding")

        # Wrong initiator
        resp = await http.get(f"/api/tasks/{task_id}/status", params={"agent_id": "imposter"})
        assert resp.status_code == 403


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_tasks_by_status(self, mcp, http, funded_network):
        """List tasks filtered by status."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "List status test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        resp = await http.get("/api/tasks", params={"status": "unclaimed"})
        assert resp.status_code == 200
        tasks = resp.json()
        assert all(t["status"] in ("unclaimed", "bidding") for t in tasks)

    @pytest.mark.asyncio
    async def test_list_tasks_by_initiator(self, mcp, http, funded_network):
        """List tasks filtered by initiator_id."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="other-init")

        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "By initiator A",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "By initiator B",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "other-init",
        })

        resp = await http.get("/api/tasks", params={"initiator_id": "query-init"})
        assert resp.status_code == 200
        tasks = resp.json()
        assert all(t["initiator_id"] == "query-init" for t in tasks)
        assert len(tasks) >= 1

    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, mcp, http, funded_network):
        """List tasks with limit and offset."""
        await _setup_agent(mcp, funded_network)
        # Create 3 tasks
        for i in range(3):
            await mcp.call_tool_parsed("eacn_create_task", {
                "description": f"Pagination test {i}",
                "budget": 10.0,
                "domains": ["coding"],
                "initiator_id": "query-init",
            })

        # Get page 1 (limit=2)
        resp = await http.get("/api/tasks", params={
            "initiator_id": "query-init", "limit": 2, "offset": 0,
        })
        assert resp.status_code == 200
        page1 = resp.json()
        assert len(page1) <= 2

        # Get page 2 (offset=2)
        resp = await http.get("/api/tasks", params={
            "initiator_id": "query-init", "limit": 2, "offset": 2,
        })
        assert resp.status_code == 200
        page2 = resp.json()
        # Should have remaining tasks
        assert len(page1) + len(page2) >= 3


class TestOpenTasks:
    @pytest.mark.asyncio
    async def test_list_open_tasks(self, mcp, http, funded_network):
        """Open tasks endpoint returns unclaimed/bidding tasks."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Open task test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        result = await mcp.call_tool_parsed("eacn_list_open_tasks", {
            "domains": ["coding"],
        })
        # Plugin returns list or dict with tasks
        tasks = result if isinstance(result, list) else result.get("tasks", [])
        assert len(tasks) >= 1

    @pytest.mark.asyncio
    async def test_open_tasks_domain_filter(self, mcp, http, funded_network):
        """Open tasks filtered by domain."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Design task",
            "budget": 50.0,
            "domains": ["design"],
            "initiator_id": "query-init",
        })
        await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Coding task",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        # Filter by design only
        resp = await http.get("/api/tasks/open", params={"domains": "design"})
        assert resp.status_code == 200
        tasks = resp.json()
        for t in tasks:
            assert "design" in t["domains"]

    @pytest.mark.asyncio
    async def test_closed_task_not_in_open(self, mcp, http, funded_network):
        """A closed task should not appear in open tasks."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn_create_task", {
            "description": "Will close",
            "budget": 50.0,
            "domains": ["closing-test"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]

        # Close it
        await mcp.call_tool_parsed("eacn_close_task", {
            "task_id": task_id,
            "initiator_id": "query-init",
        })

        resp = await http.get("/api/tasks/open", params={"domains": "closing-test"})
        assert resp.status_code == 200
        task_ids = [t["id"] for t in resp.json()]
        assert task_id not in task_ids
