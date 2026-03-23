"""Tests: Long-term production scenario problems.

Targets issues that only surface after sustained operation:
- State accumulation / memory growth (tasks, bids, escrows never cleaned)
- Budget drift after many freeze/release/settlement cycles
- Reputation score boundary violations after hundreds of events
- Escrow orphans (stuck tasks → funds locked forever)
- Double-settlement / double-select idempotency
- Zombie tasks (bidding forever, never resolved)
- Gossip table unbounded growth
- Subtask depth bomb / budget drain via recursive subtasks
- Concurrent slot exhaustion with queue that never drains
- Race: result submitted after task already completed/closed
- Stale DHT entries for unregistered agents
"""

import pytest
from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
    setup_task_with_result,
)


# ── Budget drift after many cycles ──────────────────────────────────


class TestBudgetDriftOverManyCycles:
    """After N create→settle→refund cycles, does the initiator's balance stay accurate?"""

    @pytest.mark.asyncio
    async def test_budget_conservation_after_many_settlements(self, client, funded_network):
        """Run 20 task cycles and verify total funds are conserved (no leak/gain)."""
        net = funded_network
        initiator = "user1"
        initial_balance = net.escrow.get_or_create_account(initiator).total

        for i in range(20):
            tid = f"drift-{i}"
            await create_task(client, task_id=tid, budget=100.0, initiator_id=initiator)
            await bid(client, task_id=tid, agent_id="a1", price=50.0)
            await submit_result(client, task_id=tid, agent_id="a1")
            await close_task(client, task_id=tid, initiator_id=initiator)
            await select_result(client, task_id=tid, agent_id="a1", initiator_id=initiator)

        acct = net.escrow.get_or_create_account(initiator)
        a1_acct = net.escrow.get_or_create_account("a1")

        # Total money in system = initiator remaining + executor earned + platform fees
        fee_rate = net.settlement.platform_fee_rate  # 0.05
        expected_paid_per_task = 50.0  # bid price
        expected_fee_per_task = 50.0 * fee_rate
        total_paid = 20 * expected_paid_per_task
        total_fees = 20 * expected_fee_per_task

        # Initiator should have lost: 20 * (bid_price + fee) = 20 * 52.5 = 1050
        expected_initiator = initial_balance - total_paid - total_fees
        assert abs(acct.total - expected_initiator) < 1e-9, (
            f"Initiator balance drifted: expected {expected_initiator}, got {acct.total}"
        )
        # Executor should have earned exactly 20 * 50
        assert abs(a1_acct.available - total_paid) < 1e-9, (
            f"Executor balance drifted: expected {total_paid}, got {a1_acct.available}"
        )
        # Platform fees should sum correctly
        assert abs(net.settlement.total_fees_collected - total_fees) < 1e-9

    @pytest.mark.asyncio
    async def test_budget_conservation_after_many_refunds(self, client, funded_network):
        """Create and cancel 20 tasks — initiator balance must return to original."""
        net = funded_network
        initiator = "user1"
        initial_balance = net.escrow.get_or_create_account(initiator).total

        for i in range(20):
            tid = f"refund-{i}"
            await create_task(client, task_id=tid, budget=200.0, initiator_id=initiator)
            # Nobody bids → close → no_one_able → refund
            await close_task(client, task_id=tid, initiator_id=initiator)

        acct = net.escrow.get_or_create_account(initiator)
        assert abs(acct.total - initial_balance) < 1e-9, (
            f"After 20 refunds, balance should be {initial_balance}, got {acct.total}"
        )
        assert acct.frozen == 0.0, "No funds should be frozen after all refunds"

    @pytest.mark.asyncio
    async def test_mixed_settle_and_refund_cycles(self, client, funded_network):
        """Alternate between settlements and refunds — balance must stay consistent."""
        net = funded_network
        initiator = "user1"
        initial_balance = net.escrow.get_or_create_account(initiator).total
        fee_rate = net.settlement.platform_fee_rate

        settled_count = 0
        for i in range(20):
            tid = f"mixed-{i}"
            await create_task(client, task_id=tid, budget=100.0, initiator_id=initiator)
            if i % 2 == 0:
                # Settle
                await bid(client, task_id=tid, agent_id="a1", price=40.0)
                await submit_result(client, task_id=tid, agent_id="a1")
                await close_task(client, task_id=tid, initiator_id=initiator)
                await select_result(client, task_id=tid, agent_id="a1", initiator_id=initiator)
                settled_count += 1
            else:
                # Refund
                await close_task(client, task_id=tid, initiator_id=initiator)

        acct = net.escrow.get_or_create_account(initiator)
        expected_loss = settled_count * (40.0 + 40.0 * fee_rate)
        expected_balance = initial_balance - expected_loss
        assert abs(acct.total - expected_balance) < 1e-9


# ── Escrow orphans ──────────────────────────────────────────────────


class TestEscrowOrphans:
    """Escrow entries that never get released = permanent fund lock."""

    @pytest.mark.asyncio
    async def test_escrow_cleaned_after_settlement(self, client, funded_network):
        """After select_result, escrow entry for task should be gone."""
        net = funded_network
        await create_task(client, task_id="esc1", budget=500.0)
        await bid(client, task_id="esc1", agent_id="a1", price=200.0)
        await submit_result(client, task_id="esc1", agent_id="a1")
        await close_task(client, task_id="esc1")
        await select_result(client, task_id="esc1", agent_id="a1")

        # Escrow should be empty for this task
        assert net.escrow.get_escrowed_amount("esc1") == 0.0
        assert "esc1" not in net.escrow._task_escrows

    @pytest.mark.asyncio
    async def test_escrow_cleaned_after_no_one_able(self, client, funded_network):
        """After no_one_able, escrow should be released."""
        net = funded_network
        await create_task(client, task_id="esc2", budget=500.0)
        await close_task(client, task_id="esc2")

        assert net.escrow.get_escrowed_amount("esc2") == 0.0
        assert "esc2" not in net.escrow._task_escrows

    @pytest.mark.asyncio
    async def test_subtask_escrow_not_orphaned(self, client, funded_network):
        """Subtask escrow should be independently tracked and releasable."""
        net = funded_network
        await create_task(client, task_id="esc-parent", budget=1000.0)
        await bid(client, task_id="esc-parent", agent_id="a1", price=200.0)

        # Create subtask
        resp = await client.post("/api/tasks/esc-parent/subtask", json={
            "initiator_id": "a1", "content": {"desc": "sub"},
            "domains": ["coding"], "budget": 300.0,
        })
        sub = resp.json()
        sub_id = sub["id"]

        # Parent escrow reduced, subtask escrow exists
        assert net.escrow.get_escrowed_amount("esc-parent") == 700.0
        assert net.escrow.get_escrowed_amount(sub_id) == 300.0

        # Close subtask with no bids → refund subtask escrow
        await close_task(client, task_id=sub_id, initiator_id="a1")

        # Subtask escrow should be gone, but parent escrow still has its portion
        assert net.escrow.get_escrowed_amount(sub_id) == 0.0


# ── Double operations (idempotency) ─────────────────────────────────


class TestDoubleOperations:
    """Operations that shouldn't succeed twice."""

    @pytest.mark.asyncio
    async def test_double_select_result_rejected(self, client, funded_network):
        """Selecting a result twice should fail — escrow already released."""
        await create_task(client, task_id="dbl1", budget=500.0)
        await bid(client, task_id="dbl1", agent_id="a1", price=100.0)
        await bid(client, task_id="dbl1", agent_id="a2", price=120.0)
        await submit_result(client, task_id="dbl1", agent_id="a1")
        await submit_result(client, task_id="dbl1", agent_id="a2")
        await close_task(client, task_id="dbl1")

        # First select succeeds
        await select_result(client, task_id="dbl1", agent_id="a1")

        # Second select should fail (escrow gone after settlement)
        resp = await client.post("/api/tasks/dbl1/select", json={
            "initiator_id": "user1", "agent_id": "a2",
        })
        assert resp.status_code == 400, "Double select should return 400"

    @pytest.mark.asyncio
    async def test_bid_after_task_completed(self, client, funded_network):
        """Cannot bid on a completed task."""
        await create_task(client, task_id="dbl2", budget=200.0)
        await bid(client, task_id="dbl2", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dbl2", agent_id="a1")
        await close_task(client, task_id="dbl2")
        await select_result(client, task_id="dbl2", agent_id="a1")

        # Try to bid on completed task
        resp = await client.post("/api/tasks/dbl2/bid", json={
            "agent_id": "a2", "confidence": 0.9, "price": 50.0,
        })
        assert resp.status_code != 200, "Bid on completed task should fail"

    @pytest.mark.asyncio
    async def test_result_after_task_completed(self, client, funded_network):
        """Cannot submit result to a completed task."""
        await create_task(client, task_id="dbl3", budget=200.0)
        await bid(client, task_id="dbl3", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dbl3", agent_id="a1")
        await close_task(client, task_id="dbl3")
        await select_result(client, task_id="dbl3", agent_id="a1")

        # Try to submit result on completed task
        resp = await client.post("/api/tasks/dbl3/result", json={
            "agent_id": "a2", "content": "late result",
        })
        assert resp.status_code != 200, "Result on completed task should fail"

    @pytest.mark.asyncio
    async def test_close_already_closed_task(self, client, funded_network):
        """Cannot close a task that's already in a terminal state."""
        await create_task(client, task_id="dbl4", budget=200.0)
        await close_task(client, task_id="dbl4")

        data = (await client.get("/api/tasks/dbl4")).json()
        assert data["status"] == "no_one_able"

        # Try to close again
        resp = await client.post("/api/tasks/dbl4/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code != 200, "Closing terminal task should fail"


# ── Reputation boundary drift ───────────────────────────────────────


class TestReputationLongTermDrift:
    """Reputation scores must stay in [0.0, 1.0] after hundreds of events."""

    @pytest.mark.asyncio
    async def test_score_never_exceeds_bounds_after_many_positive_events(self, funded_network):
        """Hundreds of positive events must not push score above 1.0."""
        rep = funded_network.reputation
        agent = "rep-pos"
        for _ in range(200):
            await rep.aggregate(agent, [{"type": "task_completed"}], server_id="s1")
        score = rep.get_score(agent)
        assert 0.0 <= score <= 1.0, f"Score out of bounds: {score}"

    @pytest.mark.asyncio
    async def test_score_never_goes_below_zero_after_many_negative_events(self, funded_network):
        """Hundreds of negative events must not push score below 0.0."""
        rep = funded_network.reputation
        agent = "rep-neg"
        rep._scores[agent] = 0.5
        for _ in range(200):
            await rep.aggregate(agent, [{"type": "task_failed"}], server_id="s1")
        score = rep.get_score(agent)
        assert 0.0 <= score <= 1.0, f"Score out of bounds: {score}"

    @pytest.mark.asyncio
    async def test_selection_propagation_never_exceeds_1(self, funded_network):
        """Repeated propagate_selection must not push score above 1.0."""
        rep = funded_network.reputation
        rep._scores["selector"] = 0.99
        rep._scores["selected"] = 0.99

        for _ in range(100):
            await rep.propagate_selection("selector", "selected")

        assert rep.get_score("selector") <= 1.0
        assert rep.get_score("selected") <= 1.0

    @pytest.mark.asyncio
    async def test_cap_counts_accumulate_correctly(self, funded_network):
        """Cap counts should grow monotonically, never reset unexpectedly."""
        rep = funded_network.reputation
        agent = "rep-cap"
        rep._scores[agent] = 0.5
        # Set high server reputation to make events exceed cap
        await rep.set_server_reputation("s-hi", 1.0)
        rep._server_event_counts["s-hi"] = 1000  # past cold start

        prev_gain = 0
        for i in range(50):
            await rep.aggregate(agent, [{"type": "task_completed"}], server_id="s-hi")
            counts = rep.get_cap_counts(agent)
            current_gain = counts.get("capped_gain", 0)
            assert current_gain >= prev_gain, (
                f"Cap count decreased at iteration {i}: {current_gain} < {prev_gain}"
            )
            prev_gain = current_gain


# ── State accumulation / zombie tasks ───────────────────────────────


class TestStateAccumulation:
    """Tasks and bids pile up over time — check for unbounded growth issues."""

    @pytest.mark.asyncio
    async def test_completed_tasks_still_queryable(self, client, funded_network):
        """All 30 completed tasks should remain in storage and be queryable."""
        for i in range(30):
            tid = f"acc-{i}"
            await create_task(client, task_id=tid, budget=50.0)
            await bid(client, task_id=tid, agent_id="a1", price=20.0)
            await submit_result(client, task_id=tid, agent_id="a1")
            await close_task(client, task_id=tid)
            await select_result(client, task_id=tid, agent_id="a1")
            # Collect results to transition awaiting_retrieval → completed
            await client.get(f"/api/tasks/{tid}/results", params={"initiator_id": "user1"})

        # All tasks should be queryable
        for i in range(30):
            resp = await client.get(f"/api/tasks/acc-{i}")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_many_bids_on_single_task(self, client, funded_network):
        """Many agents bid on one task — bid list grows but task still works."""
        await create_task(
            client, task_id="many-bids", budget=500.0, max_concurrent_bidders=50,
        )
        for i in range(50):
            aid = f"bidder-{i}"
            funded_network.reputation._scores[aid] = 0.8
            await bid(client, task_id="many-bids", agent_id=aid, price=10.0)

        data = (await client.get("/api/tasks/many-bids")).json()
        assert len(data["bids"]) == 50
        assert data["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_deadline_scan_with_many_tasks(self, client, funded_network):
        """Deadline scanner must handle large task sets efficiently."""
        for i in range(30):
            tid = f"ddl-{i}"
            await create_task(
                client, task_id=tid, budget=30.0,
                deadline="2020-01-01T00:00:00+00:00",
            )

        resp = await client.post(
            "/api/admin/scan-deadlines",
            params={"now": "2025-01-01T00:00:00+00:00"},
        )
        assert resp.status_code == 200

        # All should be expired → no_one_able
        for i in range(30):
            data = (await client.get(f"/api/tasks/ddl-{i}")).json()
            assert data["status"] == "no_one_able"


# ── Subtask depth bomb ──────────────────────────────────────────────


class TestSubtaskDepthBomb:
    """Recursive subtask creation must be depth-limited."""

    @pytest.mark.asyncio
    async def test_max_depth_enforced(self, client, funded_network):
        """Cannot exceed max_depth via chained subtask creation."""
        await create_task(
            client, task_id="depth-root", budget=10000.0, max_depth=3,
        )
        await bid(client, task_id="depth-root", agent_id="a1", price=100.0)

        current_id = "depth-root"
        current_agent = "a1"
        created_ids = []

        for depth in range(1, 5):  # Try to create depth 1,2,3,4 — 4 should fail
            resp = await client.post(f"/api/tasks/{current_id}/subtask", json={
                "initiator_id": current_agent,
                "content": {"desc": f"depth {depth}"},
                "domains": ["coding"],
                "budget": 1000.0,
            })
            if depth <= 3:
                assert resp.status_code == 201, f"Depth {depth} should succeed"
                sub = resp.json()
                created_ids.append(sub["id"])
                # Bid on this subtask so we can create children
                next_agent = f"d{depth}"
                funded_network.reputation._scores[next_agent] = 0.8
                await bid(client, task_id=sub["id"], agent_id=next_agent, price=50.0)
                current_id = sub["id"]
                current_agent = next_agent
            else:
                assert resp.status_code != 201, f"Depth {depth} should be rejected"

    @pytest.mark.asyncio
    async def test_subtask_budget_cannot_exceed_parent_remaining(self, client, funded_network):
        """Subtask budget is bounded by parent's remaining budget."""
        await create_task(client, task_id="bdg-parent", budget=500.0)
        await bid(client, task_id="bdg-parent", agent_id="a1", price=100.0)

        # First subtask takes 300
        resp = await client.post("/api/tasks/bdg-parent/subtask", json={
            "initiator_id": "a1", "content": {"desc": "sub1"},
            "domains": ["coding"], "budget": 300.0,
        })
        assert resp.status_code == 201

        # Second subtask tries to take 300 (only 200 remaining)
        resp = await client.post("/api/tasks/bdg-parent/subtask", json={
            "initiator_id": "a1", "content": {"desc": "sub2"},
            "domains": ["coding"], "budget": 300.0,
        })
        assert resp.status_code != 201, "Should reject: exceeds remaining budget"


# ── Concurrent slot exhaustion ──────────────────────────────────────


class TestConcurrentSlotExhaustion:
    """When all slots are full, wait queue must function correctly."""

    @pytest.mark.asyncio
    async def test_wait_queue_drains_after_rejection(self, client, funded_network):
        """Queued agents should be promoted when a slot opens via rejection."""
        net = funded_network
        await create_task(
            client, task_id="slots", budget=500.0, max_concurrent_bidders=2,
        )
        # Fill 2 slots
        b1 = await bid(client, task_id="slots", agent_id="a1", price=50.0)
        b2 = await bid(client, task_id="slots", agent_id="a2", price=60.0)
        assert b1["status"] == "executing"
        assert b2["status"] == "executing"

        # 3rd goes to wait queue
        b3 = await bid(client, task_id="slots", agent_id="a3", price=70.0)
        assert b3["status"] == "waiting"

        # Reject a1 → a3 should be promoted
        net.task_manager.reject_bid("slots", "a1")
        promoted = net.task_manager.promote_from_queue("slots")
        assert promoted == "a3"

        task = net.task_manager.get("slots")
        assert "a3" in task.executing_agents
        assert len(task.waiting_agents) == 0


# ── Stale DHT entries ──────────────────────────────────────────────


class TestStaleDHTEntries:
    """Agents that unregister should not remain discoverable."""

    @pytest.mark.asyncio
    async def test_revoked_agent_not_in_dht(self, funded_network):
        """After revoke, agent should not appear in DHT lookups."""
        net = funded_network
        await net.dht.announce("testing", "stale-agent")
        agents = await net.dht.lookup("testing")
        assert "stale-agent" in agents

        await net.dht.revoke("testing", "stale-agent")
        agents = await net.dht.lookup("testing")
        assert "stale-agent" not in agents

    @pytest.mark.asyncio
    async def test_revoke_all_cleans_all_domains(self, funded_network):
        """revoke_all should remove agent from every domain."""
        net = funded_network
        await net.dht.announce("d1", "multi-agent")
        await net.dht.announce("d2", "multi-agent")
        await net.dht.announce("d3", "multi-agent")

        await net.dht.revoke_all("multi-agent")

        for d in ("d1", "d2", "d3"):
            agents = await net.dht.lookup(d)
            assert "multi-agent" not in agents


# ── Gossip table growth ─────────────────────────────────────────────


class TestGossipGrowth:
    """Gossip known-agent sets grow with each exchange — verify correctness."""

    @pytest.mark.asyncio
    async def test_gossip_exchange_is_symmetric(self, funded_network):
        """After exchange, both agents should know each other and all shared contacts."""
        net = funded_network
        g = net.gossip

        # Pre-seed some knowledge
        await net.db.gossip_add_many("g1", {"g3", "g4"})
        await net.db.gossip_add_many("g2", {"g5", "g6"})

        await g.exchange("g1", "g2")

        g1_knows = await g.get_known("g1")
        g2_knows = await g.get_known("g2")

        # Both should know all agents
        expected = {"g2", "g3", "g4", "g5", "g6"}
        assert expected <= g1_knows
        expected = {"g1", "g3", "g4", "g5", "g6"}
        assert expected <= g2_knows

    @pytest.mark.asyncio
    async def test_repeated_exchanges_are_idempotent(self, funded_network):
        """Exchanging multiple times shouldn't cause issues."""
        g = funded_network.gossip

        for _ in range(20):
            await g.exchange("r1", "r2")

        r1_knows = await g.get_known("r1")
        r2_knows = await g.get_known("r2")

        # Should just know each other, nothing more
        assert r1_knows == {"r2"}
        assert r2_knows == {"r1"}


# ── Floating-point precision ────────────────────────────────────────


class TestFloatingPointPrecision:
    """Financial calculations must not accumulate floating-point errors."""

    @pytest.mark.asyncio
    async def test_many_small_transactions_no_drift(self, client, funded_network):
        """100 tiny tasks: total deducted must match sum of individual settlements."""
        net = funded_network
        initiator = "user2"
        initial = net.escrow.get_or_create_account(initiator).total  # 5000
        fee_rate = net.settlement.platform_fee_rate

        for i in range(100):
            tid = f"fp-{i}"
            await create_task(
                client, task_id=tid, budget=1.0, initiator_id=initiator,
                domains=["coding"],
            )
            await bid(client, task_id=tid, agent_id="a1", price=0.37)
            await submit_result(client, task_id=tid, agent_id="a1")
            await close_task(client, task_id=tid, initiator_id=initiator)
            await select_result(client, task_id=tid, agent_id="a1", initiator_id=initiator)

        acct = net.escrow.get_or_create_account(initiator)
        # Each task: paid 0.37 + fee 0.37*0.05 = 0.3885, refund remainder
        per_task_cost = 0.37 + 0.37 * fee_rate
        expected = initial - 100 * per_task_cost
        # Allow small float tolerance but not drift
        assert abs(acct.total - expected) < 1e-6, (
            f"Float drift detected: expected ~{expected}, got {acct.total}, "
            f"diff={abs(acct.total - expected)}"
        )

    @pytest.mark.asyncio
    async def test_account_never_negative(self, client, funded_network):
        """After spending nearly everything, balance should never go negative."""
        net = funded_network
        initiator = "user2"
        acct = net.escrow.get_or_create_account(initiator)  # 5000

        # Spend most of it
        for i in range(49):
            tid = f"neg-{i}"
            await create_task(
                client, task_id=tid, budget=100.0, initiator_id=initiator,
            )
            await bid(client, task_id=tid, agent_id="a1", price=95.0)
            await submit_result(client, task_id=tid, agent_id="a1")
            await close_task(client, task_id=tid, initiator_id=initiator)
            await select_result(client, task_id=tid, agent_id="a1", initiator_id=initiator)

        assert acct.available >= 0.0, f"Available went negative: {acct.available}"
        assert acct.frozen >= 0.0, f"Frozen went negative: {acct.frozen}"
