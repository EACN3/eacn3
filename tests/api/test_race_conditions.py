"""Advanced race condition tests.

Tests specific timing-dependent scenarios:
- Close task while agents are bidding
- Select result while another agent is submitting
- Create subtask while parent is being closed
- Deadline scan while select is in progress
- Concurrent budget confirmations
- Concurrent discussions
"""

import asyncio
import pytest

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(c, tid): return (await c.get(f"/api/tasks/{tid}")).json()
async def _safe_post(c, url, json):
    r = await c.post(url, json=json)
    return {"code": r.status_code, "body": r.json()}


class TestCloseWhileBidding:
    @pytest.mark.asyncio
    async def test_close_and_3_bids_concurrent(self, client):
        """Close fires while 3 agents are mid-bid."""
        await create_task(client, task_id="cwb-1", budget=500.0, max_concurrent_bidders=5)

        results = await asyncio.gather(
            client.post("/api/tasks/cwb-1/close", json={"initiator_id": "user1"}),
            *[client.post("/api/tasks/cwb-1/bid", json={
                "agent_id": f"a{i+1}", "confidence": 0.9, "price": 80.0,
            }) for i in range(3)],
        )

        codes = [r.status_code for r in results]
        # No 500 errors
        assert all(c in (200, 400) for c in codes), f"Server error in: {codes}"


class TestSelectWhileSubmitting:
    @pytest.mark.asyncio
    async def test_select_and_new_result_concurrent(self, client):
        """Select fires while another agent submits result."""
        await create_task(client, task_id="sws-1", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="sws-1", agent_id="a1", price=80.0)
        await bid(client, task_id="sws-1", agent_id="a2", price=70.0)
        await submit_result(client, task_id="sws-1", agent_id="a1")

        # Concurrent: select a1's result + a2 submits result
        select_resp, submit_resp = await asyncio.gather(
            _safe_post(client, "/api/tasks/sws-1/select", {
                "initiator_id": "user1", "agent_id": "a1", "close_task": True,
            }),
            _safe_post(client, "/api/tasks/sws-1/result", {
                "agent_id": "a2", "content": "late result",
            }),
        )

        # Select should succeed; submit may succeed or fail (task closing)
        assert select_resp["code"] == 200
        assert submit_resp["code"] in (200, 400)


class TestSubtaskWhileClosing:
    @pytest.mark.asyncio
    async def test_create_subtask_during_parent_close(self, client):
        """Subtask creation and parent close happen concurrently."""
        await create_task(client, task_id="swc-1", budget=500.0)
        await bid(client, task_id="swc-1", agent_id="a1", price=200.0)

        sub_resp, close_resp = await asyncio.gather(
            _safe_post(client, "/api/tasks/swc-1/subtask", {
                "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 100.0,
            }),
            _safe_post(client, "/api/tasks/swc-1/close", {"initiator_id": "user1"}),
        )

        # One should succeed, the other may fail, but no crashes
        assert sub_resp["code"] in (201, 400)
        assert close_resp["code"] in (200, 400)


class TestConcurrentBudgetConfirm:
    @pytest.mark.asyncio
    async def test_two_confirms_same_task(self, client):
        """Two concurrent budget confirmations for the same task."""
        await create_task(client, task_id="cbc-1", budget=50.0)
        await bid(client, task_id="cbc-1", agent_id="a1", price=80.0)

        r1, r2 = await asyncio.gather(
            _safe_post(client, "/api/tasks/cbc-1/confirm-budget", {
                "initiator_id": "user1", "approved": True, "new_budget": 100.0,
            }),
            _safe_post(client, "/api/tasks/cbc-1/confirm-budget", {
                "initiator_id": "user1", "approved": True, "new_budget": 120.0,
            }),
        )

        # Both should return 200 (no crash), budget ends up at one of the values
        assert r1["code"] == 200
        assert r2["code"] == 200


class TestConcurrentDiscussions:
    @pytest.mark.asyncio
    async def test_3_discussion_updates_concurrent(self, client):
        """3 discussion updates submitted simultaneously."""
        await create_task(client, task_id="disc-race", budget=200.0)
        await bid(client, task_id="disc-race", agent_id="a1", price=80.0)

        results = await asyncio.gather(*[
            _safe_post(client, "/api/tasks/disc-race/discussions", {
                "initiator_id": "user1", "message": f"Message {i}",
            })
            for i in range(3)
        ])

        assert all(r["code"] == 200 for r in results)

        task = await _get_task(client, "disc-race")
        discussions = task["content"].get("discussions", [])
        assert len(discussions) == 3


class TestDeadlineDuringSelect:
    @pytest.mark.asyncio
    async def test_scan_deadlines_and_select_concurrent(self, client):
        """Deadline scan fires while select is in progress."""
        await create_task(client, task_id="dds-1", budget=200.0,
                        deadline="2020-01-01T00:00:00Z")
        await bid(client, task_id="dds-1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dds-1", agent_id="a1")

        scan_resp, select_resp = await asyncio.gather(
            client.post("/api/admin/scan-deadlines"),
            _safe_post(client, "/api/tasks/dds-1/select", {
                "initiator_id": "user1", "agent_id": "a1", "close_task": True,
            }),
        )

        assert scan_resp.status_code == 200
        # Select may succeed or fail depending on timing
        assert select_resp["code"] in (200, 400)

        # Task should be in a terminal state
        task = await _get_task(client, "dds-1")
        assert task["status"] in ("completed", "awaiting_retrieval", "no_one_able")


class TestRejectWhileSubmitting:
    @pytest.mark.asyncio
    async def test_reject_and_submit_result_concurrent(self, client):
        """Agent rejects while also submitting result — one should win."""
        await create_task(client, task_id="rws-1", budget=200.0)
        await bid(client, task_id="rws-1", agent_id="a1", price=80.0)

        reject_resp, submit_resp = await asyncio.gather(
            _safe_post(client, "/api/tasks/rws-1/reject", {"agent_id": "a1"}),
            _safe_post(client, "/api/tasks/rws-1/result", {
                "agent_id": "a1", "content": "result",
            }),
        )

        assert reject_resp["code"] in (200, 400)
        assert submit_resp["code"] in (200, 400)
        # At least one should succeed
        assert reject_resp["code"] == 200 or submit_resp["code"] == 200


class TestConcurrentDepositsAndCreates:
    @pytest.mark.asyncio
    async def test_deposit_while_creating_tasks(self, client, funded_network):
        """Deposits happening concurrently with task creation."""
        results = await asyncio.gather(
            client.post("/api/economy/deposit", json={"agent_id": "user1", "amount": 500.0}),
            create_task(client, task_id="dc-1", budget=100.0),
            create_task(client, task_id="dc-2", budget=100.0),
            client.post("/api/economy/deposit", json={"agent_id": "user1", "amount": 500.0}),
            create_task(client, task_id="dc-3", budget=100.0),
        )

        # All should succeed (user1 has 10000 + deposits)
        for r in results:
            if hasattr(r, 'status_code'):
                assert r.status_code in (200, 201), f"Failed: {r.text}"
