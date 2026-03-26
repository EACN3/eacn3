"""Heavy load and scale tests.

Simulates scenarios with high task/agent counts and rapid operations:
- 50 tasks created rapidly
- 20 agents bidding across multiple tasks
- Rapid bid→result→select cycles
- Large subtask trees
- Repeated deadline scans
- Offline store under heavy write load
"""

import asyncio
import pytest

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _get_task(client, tid: str) -> dict:
    return (await client.get(f"/api/tasks/{tid}")).json()

async def _get_balance(client, aid: str) -> dict:
    return (await client.get("/api/economy/balance", params={"agent_id": aid})).json()


class TestHighTaskVolume:
    """Many tasks at once."""

    @pytest.mark.asyncio
    async def test_create_50_tasks_sequentially(self, client):
        """Create 50 tasks rapidly — no crashes, all succeed."""
        for i in range(50):
            resp = await client.post("/api/tasks", json={
                "task_id": f"vol-{i}", "initiator_id": "user1",
                "content": {"n": i}, "domains": ["coding"],
                "budget": 10.0,
            })
            assert resp.status_code == 201, f"Task vol-{i} failed: {resp.text}"

        # All should be listable
        resp = await client.get("/api/tasks", params={"limit": 100})
        assert len(resp.json()) >= 50

    @pytest.mark.asyncio
    async def test_create_20_tasks_concurrently(self, client):
        """Create 20 tasks in parallel."""
        results = await asyncio.gather(*[
            client.post("/api/tasks", json={
                "task_id": f"par-{i}", "initiator_id": "user1",
                "content": {}, "domains": ["coding"], "budget": 10.0,
            })
            for i in range(20)
        ])
        successes = [r for r in results if r.status_code == 201]
        assert len(successes) == 20


class TestHighAgentVolume:
    """Many agents interacting with the system."""

    @pytest.mark.asyncio
    async def test_20_agents_bid_on_5_tasks(self, client, funded_network):
        """20 agents each bid on 5 different tasks — 100 bids total."""
        net = funded_network
        agents = [f"ha-{i}" for i in range(20)]
        for a in agents:
            net.reputation._scores[a] = 0.8
            await net.dht.announce("coding", a)

        for i in range(5):
            await create_task(client, task_id=f"hat-{i}", budget=500.0,
                            max_concurrent_bidders=10)

        # All agents bid on all tasks concurrently
        bid_coros = []
        for agent in agents:
            for i in range(5):
                bid_coros.append(
                    client.post(f"/api/tasks/hat-{i}/bid", json={
                        "agent_id": agent, "confidence": 0.9, "price": 50.0,
                    })
                )

        results = await asyncio.gather(*bid_coros)
        ok_count = sum(1 for r in results if r.status_code == 200)
        # All 100 bids should succeed (no duplicates, enough slots)
        assert ok_count == 100, f"Only {ok_count}/100 bids succeeded"


class TestRapidLifecycle:
    """Rapid full lifecycles."""

    @pytest.mark.asyncio
    async def test_20_rapid_cycles(self, client, funded_network):
        """20 complete create→bid→result→close→select cycles as fast as possible."""
        net = funded_network
        net.reputation._scores["rapid-agent"] = 0.8
        await net.dht.announce("coding", "rapid-agent")

        async def cycle(i: int):
            tid = f"rapid-{i}"
            net.escrow.get_or_create_account(f"rapid-init-{i}", 500.0)
            await create_task(client, task_id=tid, initiator_id=f"rapid-init-{i}",
                            budget=100.0)
            await bid(client, task_id=tid, agent_id="rapid-agent", price=50.0)
            await submit_result(client, task_id=tid, agent_id="rapid-agent")
            await close_task(client, task_id=tid, initiator_id=f"rapid-init-{i}")
            await select_result(client, task_id=tid, agent_id="rapid-agent",
                              initiator_id=f"rapid-init-{i}")
            return (await _get_task(client, tid))["status"]

        statuses = await asyncio.gather(*[cycle(i) for i in range(20)])
        assert all(s == "completed" for s in statuses), f"Not all completed: {statuses}"


class TestDeepSubtaskTree:
    """Deeply nested subtask chains."""

    @pytest.mark.asyncio
    async def test_chain_of_5_subtasks(self, client, funded_network):
        """Create a chain: parent → sub1 → sub2 → sub3 → sub4 (depth 4, max_depth 6)."""
        net = funded_network
        for i in range(5):
            net.reputation._scores[f"chain-{i}"] = 0.8
            await net.dht.announce("coding", f"chain-{i}")

        await create_task(client, task_id="deep-root", budget=1000.0, max_depth=6)

        # First agent bids and creates a subtask chain
        await bid(client, task_id="deep-root", agent_id="chain-0", price=800.0)

        current_task = "deep-root"
        for depth in range(4):
            resp = await client.post(f"/api/tasks/{current_task}/subtask", json={
                "initiator_id": f"chain-{depth}",
                "content": {"depth": depth + 1},
                "domains": ["coding"],
                "budget": 100.0,
            })
            assert resp.status_code == 201, f"Subtask at depth {depth+1} failed: {resp.text}"
            sub = resp.json()
            assert sub["depth"] == depth + 1

            # Next agent bids on the subtask
            await bid(client, task_id=sub["id"], agent_id=f"chain-{depth+1}", price=50.0)
            current_task = sub["id"]

        # Verify the tree structure
        root = await _get_task(client, "deep-root")
        assert len(root["child_ids"]) == 1


class TestRepeatedDeadlineScans:
    """Deadline scan under various conditions."""

    @pytest.mark.asyncio
    async def test_scan_10_times_rapidly(self, client):
        """Rapid repeated scans don't cause issues."""
        await create_task(client, task_id="scan-rep", budget=50.0,
                        deadline="2020-01-01T00:00:00Z")

        for _ in range(10):
            resp = await client.post("/api/admin/scan-deadlines")
            assert resp.status_code == 200

        # Task should be expired after first scan
        task = await _get_task(client, "scan-rep")
        assert task["status"] == "no_one_able"

    @pytest.mark.asyncio
    async def test_concurrent_scans(self, client):
        """Multiple concurrent deadline scans — no crashes."""
        for i in range(5):
            await create_task(client, task_id=f"cscan-{i}", budget=10.0,
                            deadline="2020-01-01T00:00:00Z")

        results = await asyncio.gather(*[
            client.post("/api/admin/scan-deadlines")
            for _ in range(5)
        ])
        assert all(r.status_code == 200 for r in results)


class TestListingEndpoints:
    """Query and listing under load."""

    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, client):
        """Create 30 tasks and paginate through them."""
        for i in range(30):
            await create_task(client, task_id=f"page-{i}", budget=10.0)

        # Page 1
        resp = await client.get("/api/tasks", params={"limit": 10, "offset": 0})
        assert len(resp.json()) == 10

        # Page 2
        resp = await client.get("/api/tasks", params={"limit": 10, "offset": 10})
        assert len(resp.json()) == 10

        # Page 3
        resp = await client.get("/api/tasks", params={"limit": 10, "offset": 20})
        assert len(resp.json()) == 10

    @pytest.mark.asyncio
    async def test_list_open_tasks(self, client, funded_network):
        """Open tasks listing only shows unclaimed/bidding with available slots."""
        net = funded_network
        net.reputation._scores["lister"] = 0.8
        await net.dht.announce("coding", "lister")

        # Create 5 tasks, bid on 2, close 1
        for i in range(5):
            await create_task(client, task_id=f"open-{i}", budget=50.0)

        await bid(client, task_id="open-0", agent_id="lister", price=30.0)
        await bid(client, task_id="open-1", agent_id="lister", price=30.0)
        await submit_result(client, task_id="open-1", agent_id="lister")
        await close_task(client, task_id="open-1")

        resp = await client.get("/api/tasks/open")
        open_tasks = resp.json()
        open_ids = [t["id"] for t in open_tasks]

        # open-1 is closed, should not appear as open
        assert "open-1" not in open_ids
        assert len(open_ids) >= 3  # open-0 (bidding), open-2,3,4 (unclaimed)


class TestBalanceConsistency:
    """Verify balance stays consistent across complex operations."""

    @pytest.mark.asyncio
    async def test_balance_after_multiple_tasks_and_settlements(self, client, funded_network):
        """Create 5 tasks, settle 3, close 2 → verify final balance."""
        net = funded_network
        net.reputation._scores["settler"] = 0.8
        await net.dht.announce("coding", "settler")

        initial_bal = (await _get_balance(client, "user1"))["available"]

        # Create 5 tasks (100 each = 500 frozen)
        for i in range(5):
            await create_task(client, task_id=f"bal-{i}", budget=100.0)

        # Bid and submit on all
        for i in range(5):
            await bid(client, task_id=f"bal-{i}", agent_id="settler", price=50.0)
            await submit_result(client, task_id=f"bal-{i}", agent_id="settler")

        # Close all
        for i in range(5):
            await close_task(client, task_id=f"bal-{i}")

        # Select 3, leave 2 just closed
        for i in range(3):
            await select_result(client, task_id=f"bal-{i}", agent_id="settler")

        # Settler should have 3 × 50 = 150 credits
        settler_bal = await _get_balance(client, "settler")
        assert settler_bal["available"] == 150.0

        # User1: started with 10000, froze 500
        # Settled 3 tasks: each deducts 50 + fee from escrow, refund remainder
        # Left 2 tasks in awaiting_retrieval (not settled, escrow still frozen)
        user_bal = await _get_balance(client, "user1")
        assert user_bal["available"] + user_bal["frozen"] < initial_bal  # Some went to fees
        assert user_bal["frozen"] > 0  # 2 unsettled tasks still frozen
