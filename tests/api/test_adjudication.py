"""Tests: 裁决任务完整生命周期.

验证: 自动创建裁决任务 / 属性正确 / 无级联裁决 / 裁决竞标无需预算确认 / 裁决自动回收.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task


class TestAdjudicationCreation:
    @pytest.mark.asyncio
    async def test_created_on_normal_result(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = [t for t in all_tasks if t["type"] == "adjudication"]
        assert len(adj) >= 1

    @pytest.mark.asyncio
    async def test_adjudication_properties(self, client):
        await create_task(client, task_id="t1", budget=500.0, domains=["coding"])
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        assert adj["budget"] == 0.0
        assert adj["parent_id"] == "t1"
        assert adj["domains"] == ["coding"]
        assert adj["status"] == "unclaimed"

    @pytest.mark.asyncio
    async def test_no_cascading_adjudication(self, client):
        """裁决任务提交结果不应创建新的裁决任务."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # 找到裁决任务
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        adj_id = adj["id"]

        # 在裁决任务上竞标 + 提交结果
        await bid(client, task_id=adj_id, agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(client, task_id=adj_id, agent_id="a2", content="approved")

        # 不应有新的裁决任务
        all_after = (await client.get("/api/tasks")).json()
        adj_after = [t for t in all_after if t["type"] == "adjudication"]
        assert len(adj_after) == 1  # 还是只有一个

    @pytest.mark.asyncio
    async def test_adjudication_bid_no_price_check(self, client):
        """裁决任务竞标应跳过价格检查 (budget=0)."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")

        b = await bid(client, task_id=adj["id"], agent_id="a2", confidence=0.9, price=0.0)
        assert b["status"] == "executing"

    @pytest.mark.asyncio
    async def test_multiple_results_multiple_adjudications(self, client):
        """多个结果应各自产生裁决任务."""
        await create_task(client, task_id="t1", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1")
        await bid(client, task_id="t1", agent_id="a2")
        await submit_result(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a2")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = [t for t in all_tasks if t["type"] == "adjudication"]
        assert len(adj) >= 2


class TestAdjudicationAutoCollection:
    @pytest.mark.asyncio
    async def test_adjudication_result_collected_to_parent(self, client):
        """裁决结果应自动写入父任务 Result 的 adjudications."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # 找到裁决任务
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        adj_id = adj["id"]

        # 在裁决任务上竞标 + 提交裁决结果
        await bid(client, task_id=adj_id, agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(
            client, task_id=adj_id, agent_id="a2",
            content="approved",
        )

        # 检查父任务结果的 adjudications
        parent = (await client.get("/api/tasks/t1")).json()
        result = parent["results"][0]
        assert len(result["adjudications"]) >= 1
        assert result["adjudications"][0]["adjudicator_id"] == "a2"

    @pytest.mark.asyncio
    async def test_adjudication_in_collect_results_response(self, client):
        """get_task_results 应包含 adjudications 字段."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # 裁决
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        await bid(client, task_id=adj["id"], agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(client, task_id=adj["id"], agent_id="a2", content="approved")

        # 关闭并收集
        await close_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        data = resp.json()
        assert "adjudications" in data
        assert len(data["adjudications"]) >= 1
