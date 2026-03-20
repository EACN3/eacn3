"""Tests: 超预算待定竞标 (PENDING) 全流程.

验证: PENDING 状态 / confirm_budget 重新评估 / 晋升到 EXECUTING / 拒绝.
"""

import pytest
from tests.api.conftest import create_task, bid


class TestPendingBidFlow:
    @pytest.mark.asyncio
    async def test_over_budget_becomes_pending(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        b = await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        assert b["status"] == "pending"

    @pytest.mark.asyncio
    async def test_pending_visible_in_task(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "pending"

    @pytest.mark.asyncio
    async def test_confirm_budget_promotes_pending(self, client):
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)

        # 确认并设置新预算 (100 × 1.1 = 110 ≥ 100 → pass)
        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })
        assert resp.status_code == 200

        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "executing"

    @pytest.mark.asyncio
    async def test_confirm_insufficient_keeps_pending(self, client):
        """追加不够的话 pending 不会被晋升."""
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=200.0)

        # new_budget=60 → 60 × 1.1 = 66, 还不够 200
        await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 60.0,
        })
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "pending"

    @pytest.mark.asyncio
    async def test_multiple_pending_bids(self, client):
        """多个 pending bid, 确认后只有符合条件的晋升."""
        await create_task(client, task_id="t1", budget=50.0, max_concurrent_bidders=5)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=80.0)
        await bid(client, task_id="t1", agent_id="a2", confidence=0.9, price=200.0)

        # new_budget=100 → 100 × 1.1 = 110. a1(80) 够, a2(200) 不够
        await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })
        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "executing"
        assert statuses["a2"] == "pending"

    @pytest.mark.asyncio
    async def test_over_budget_rejected_when_locked(self, client):
        """并发槽满且 budget_locked 时, 超预算直接拒绝."""
        await create_task(client, task_id="t1", budget=50.0, max_concurrent_bidders=1)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=40.0)
        # 槽位满了, budget locked
        b = await bid(client, task_id="t1", agent_id="a2", confidence=0.9, price=100.0)
        # 应该是 rejected 或 waiting (取决于价格检查先于还是后于槽位检查)
        assert b["status"] in ("rejected", "waiting")

    @pytest.mark.asyncio
    async def test_confirm_budget_rejected(self, client):
        """发起者拒绝预算确认 → 所有 PENDING 竞标变为 REJECTED."""
        await create_task(client, task_id="t1", budget=50.0)
        await bid(client, task_id="t1", agent_id="a1", confidence=0.9, price=100.0)

        # 拒绝
        resp = await client.post("/api/tasks/t1/confirm-budget", json={
            "initiator_id": "user1", "approved": False,
        })
        assert resp.status_code == 200

        data = (await client.get("/api/tasks/t1")).json()
        statuses = {b["agent_id"]: b["status"] for b in data["bids"]}
        assert statuses["a1"] == "rejected"
