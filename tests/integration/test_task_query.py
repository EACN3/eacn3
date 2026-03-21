"""Integration tests: task query operations (get, list, filter, open)."""

import pytest

from tests.integration.conftest import is_error


async def _setup_agent(mcp, funded_network, agent_id="query-init"):
    """Register agent + fund for task creation."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
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
        """Getting a task that doesn't exist returns error with 404."""
        result = await mcp.call_tool_parsed("eacn3_get_task", {
            "task_id": "nonexistent-task-xyz",
        })
        assert is_error(result), f"Expected error for non-existent task, got: {result}"

    @pytest.mark.asyncio
    async def test_get_task_returns_full_details(self, mcp, funded_network):
        """eacn3_get_task returns task with all fields populated correctly."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Detail test",
            "budget": 100.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]
        assert task["status"] == "unclaimed"
        assert task["budget"] == 100.0

        result = await mcp.call_tool_parsed("eacn3_get_task", {"task_id": task_id})
        assert result["id"] == task_id
        assert result["budget"] == 100.0
        assert result["domains"] == ["coding"]
        assert result["initiator_id"] == "query-init"
        assert result["status"] in ("unclaimed", "bidding")
        assert result["type"] == "normal"
        assert result["depth"] == 0
        assert result["parent_id"] is None
        assert result["child_ids"] == []
        assert isinstance(result["bids"], list)
        assert isinstance(result["results"], list)

    @pytest.mark.asyncio
    async def test_get_task_status_initiator_only(self, mcp, http, funded_network):
        """eacn3_get_task_status returns 403 for non-initiator."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Status auth test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]

        # Correct initiator — returns task status with bids
        resp = await http.get(f"/api/tasks/{task_id}/status", params={"agent_id": "query-init"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == task_id
        assert data["status"] in ("unclaimed", "bidding")
        assert data["initiator_id"] == "query-init"
        assert isinstance(data["bids"], list)

        # Wrong initiator — 403
        resp = await http.get(f"/api/tasks/{task_id}/status", params={"agent_id": "imposter"})
        assert resp.status_code == 403


class TestListTasks:
    @pytest.mark.asyncio
    async def test_list_tasks_by_status(self, mcp, http, funded_network):
        """List tasks filtered by status returns only matching tasks."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "List status test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        resp = await http.get("/api/tasks", params={"status": "unclaimed"})
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) >= 1
        # Every returned task has the filtered status
        for t in tasks:
            assert t["status"] == "unclaimed"
        # Our task is in the list
        task_ids = [t["id"] for t in tasks]
        assert task["task_id"] in task_ids

    @pytest.mark.asyncio
    async def test_list_tasks_by_initiator(self, mcp, http, funded_network):
        """List tasks by initiator_id returns only that initiator's tasks."""
        await _setup_agent(mcp, funded_network)
        await _setup_agent(mcp, funded_network, agent_id="other-init")

        t1 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "By initiator A",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })
        t2 = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "By initiator B",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "other-init",
        })

        resp = await http.get("/api/tasks", params={"initiator_id": "query-init"})
        assert resp.status_code == 200
        tasks = resp.json()
        # All returned tasks belong to query-init
        for t in tasks:
            assert t["initiator_id"] == "query-init"
        task_ids = [t["id"] for t in tasks]
        assert t1["task_id"] in task_ids
        assert t2["task_id"] not in task_ids

    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, mcp, http, funded_network):
        """Pagination: limit+offset correctly slices task list."""
        await _setup_agent(mcp, funded_network)
        created_ids = []
        for i in range(3):
            t = await mcp.call_tool_parsed("eacn3_create_task", {
                "description": f"Page test {i}",
                "budget": 10.0,
                "domains": ["coding"],
                "initiator_id": "query-init",
            })
            created_ids.append(t["task_id"])

        # limit=2 should return at most 2
        resp = await http.get("/api/tasks", params={
            "initiator_id": "query-init", "limit": 2, "offset": 0,
        })
        page1 = resp.json()
        assert len(page1) == 2

        # offset=2 gets the rest
        resp = await http.get("/api/tasks", params={
            "initiator_id": "query-init", "limit": 10, "offset": 2,
        })
        page2 = resp.json()
        assert len(page2) >= 1

        # No overlap
        ids1 = {t["id"] for t in page1}
        ids2 = {t["id"] for t in page2}
        assert ids1.isdisjoint(ids2)


class TestOpenTasks:
    @pytest.mark.asyncio
    async def test_list_open_tasks_via_plugin(self, mcp, funded_network):
        """Plugin eacn3_list_open_tasks returns {count, tasks}."""
        await _setup_agent(mcp, funded_network)
        t = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Open task test",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        result = await mcp.call_tool_parsed("eacn3_list_open_tasks", {
            "domains": "coding",
        })
        assert result["count"] >= 1
        task_ids = [task.get("task_id") or task.get("id") for task in result["tasks"]]
        assert t["task_id"] in task_ids

    @pytest.mark.asyncio
    async def test_open_tasks_domain_filter(self, mcp, http, funded_network):
        """Open tasks filtered by domain excludes other domains."""
        await _setup_agent(mcp, funded_network)
        await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Design task",
            "budget": 50.0,
            "domains": ["design"],
            "initiator_id": "query-init",
        })
        coding_task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Coding task",
            "budget": 50.0,
            "domains": ["coding"],
            "initiator_id": "query-init",
        })

        resp = await http.get("/api/tasks/open", params={"domains": "design"})
        assert resp.status_code == 200
        tasks = resp.json()
        for t in tasks:
            assert "design" in t["domains"]
        # Coding-only task should NOT be here
        task_ids = [t["id"] for t in tasks]
        assert coding_task["task_id"] not in task_ids

    @pytest.mark.asyncio
    async def test_closed_task_not_in_open(self, mcp, http, funded_network):
        """A closed task must not appear in open tasks list."""
        await _setup_agent(mcp, funded_network)
        task = await mcp.call_tool_parsed("eacn3_create_task", {
            "description": "Will close",
            "budget": 50.0,
            "domains": ["closing-test"],
            "initiator_id": "query-init",
        })
        task_id = task["task_id"]

        # Verify it IS in open tasks before closing
        resp = await http.get("/api/tasks/open", params={"domains": "closing-test"})
        assert task_id in [t["id"] for t in resp.json()]

        # Close it
        close_result = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "query-init",
        })
        assert close_result["status"] == "no_one_able"  # No results → NO_ONE_ABLE

        # Should NOT be in open anymore
        resp = await http.get("/api/tasks/open", params={"domains": "closing-test"})
        assert task_id not in [t["id"] for t in resp.json()]
