"""Tests: GET /api/tasks/open — 可竞标任务发现.

用 mock 数据建立多种状态的任务集, 验证筛选逻辑.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task


@pytest.fixture
async def populated(client, funded_network):
    """创建混合状态任务集."""
    # unclaimed — 可竞标
    await create_task(client, task_id="open1", domains=["coding"], budget=100.0)
    await create_task(client, task_id="open2", domains=["design"], budget=200.0)
    await create_task(client, task_id="open3", domains=["coding", "design"], budget=50.0)

    # bidding, 有空位 — 可竞标
    await create_task(client, task_id="bidding1", domains=["coding"], budget=300.0, max_concurrent_bidders=3)
    await bid(client, task_id="bidding1", agent_id="a1")

    # bidding, 满位 — 不可竞标
    await create_task(client, task_id="full1", domains=["coding"], budget=100.0, max_concurrent_bidders=1)
    await bid(client, task_id="full1", agent_id="a2")

    # closed — 不可竞标
    await create_task(client, task_id="closed1", domains=["coding"], budget=50.0)
    await close_task(client, task_id="closed1")

    return client


class TestListOpenTasks:
    @pytest.mark.asyncio
    async def test_returns_open_tasks_only(self, populated):
        resp = await populated.get("/api/tasks/open")
        assert resp.status_code == 200
        ids = {t["id"] for t in resp.json()}
        # open1, open2, open3, bidding1 应该在
        assert {"open1", "open2", "open3", "bidding1"} <= ids
        # full1, closed1 不应该在
        assert "full1" not in ids
        assert "closed1" not in ids

    @pytest.mark.asyncio
    async def test_filter_by_domain(self, populated):
        resp = await populated.get("/api/tasks/open", params={"domains": "design"})
        ids = {t["id"] for t in resp.json()}
        assert "open2" in ids
        assert "open3" in ids
        assert "open1" not in ids  # coding only

    @pytest.mark.asyncio
    async def test_filter_by_multiple_domains(self, populated):
        resp = await populated.get("/api/tasks/open", params={"domains": "coding,design"})
        ids = {t["id"] for t in resp.json()}
        assert "open1" in ids
        assert "open2" in ids
        assert "open3" in ids

    @pytest.mark.asyncio
    async def test_pagination(self, populated):
        resp = await populated.get("/api/tasks/open", params={"limit": 2})
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_empty_when_all_closed(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await close_task(client, task_id="t1")
        resp = await client.get("/api/tasks/open")
        assert resp.json() == []
