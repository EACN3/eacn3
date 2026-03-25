"""Deep economy system tests.

Tests complex financial flows that arise in real multi-agent scenarios:
- Settlement math correctness (fee deduction, refund)
- Multi-subtask escrow accounting
- Budget freeze/unfreeze through lifecycle
- Platform fee accumulation
- Concurrent deposits
- Balance recovery after failed operations
"""

import asyncio
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network, set_offline_store
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
)


async def _bal(client, aid: str) -> dict:
    return (await client.get("/api/economy/balance", params={"agent_id": aid})).json()


class TestSettlementMath:
    """Verify settlement calculations are correct."""

    @pytest.mark.asyncio
    async def test_fee_deduction(self, client, funded_network):
        """Platform fee (5%) deducted correctly from escrow."""
        net = funded_network
        net.reputation._scores["math-exec"] = 0.8
        await net.dht.announce("coding", "math-exec")

        await create_task(client, task_id="math-1", budget=200.0)
        await bid(client, task_id="math-1", agent_id="math-exec", price=100.0)
        await submit_result(client, task_id="math-1", agent_id="math-exec")
        await close_task(client, task_id="math-1")
        await select_result(client, task_id="math-1", agent_id="math-exec")

        # Executor gets bid_price = 100
        bal = await _bal(client, "math-exec")
        assert bal["available"] == 100.0

        # Initiator: 10000 - 200 (frozen) + refund
        # Escrow deduction = 100 + 5% fee = 105
        # Refund = 200 - 105 = 95
        user_bal = await _bal(client, "user1")
        assert user_bal["available"] == 10000.0 - 200.0 + 95.0  # 9895

    @pytest.mark.asyncio
    async def test_small_price_settlement(self, client, funded_network):
        """Settlement with very small price — almost full refund."""
        net = funded_network
        net.reputation._scores["cheap-exec"] = 0.8
        await net.dht.announce("coding", "cheap-exec")

        user_before = await _bal(client, "user1")
        await create_task(client, task_id="cheap-1", budget=100.0)
        await bid(client, task_id="cheap-1", agent_id="cheap-exec", price=1.0)
        await submit_result(client, task_id="cheap-1", agent_id="cheap-exec")
        await close_task(client, task_id="cheap-1")
        await select_result(client, task_id="cheap-1", agent_id="cheap-exec")

        # Executor gets 1.0
        exec_bal = await _bal(client, "cheap-exec")
        assert exec_bal["available"] == 1.0

        # Initiator gets most back (100 - 1 - 0.05 fee = 98.95 refund)
        user_after = await _bal(client, "user1")
        assert user_after["available"] > user_before["available"] - 10  # Nearly full refund


class TestMultiSubtaskEscrow:
    """Complex escrow flows with multiple subtasks."""

    @pytest.mark.asyncio
    async def test_subtask_escrow_deduction_and_parent_tracking(self, client, funded_network):
        """Parent 500 → sub1(100) + sub2(150) → parent remaining = 250."""
        net = funded_network
        net.reputation._scores["sub-exec"] = 0.8
        await net.dht.announce("coding", "sub-exec")

        await create_task(client, task_id="msub-parent", budget=500.0)
        await bid(client, task_id="msub-parent", agent_id="sub-exec", price=300.0)

        # Create two subtasks
        resp1 = await client.post("/api/tasks/msub-parent/subtask", json={
            "initiator_id": "sub-exec", "content": {},
            "domains": ["coding"], "budget": 100.0,
        })
        assert resp1.status_code == 201

        resp2 = await client.post("/api/tasks/msub-parent/subtask", json={
            "initiator_id": "sub-exec", "content": {},
            "domains": ["coding"], "budget": 150.0,
        })
        assert resp2.status_code == 201

        # Check escrow detail
        esc = (await client.get("/api/economy/escrows",
                               params={"agent_id": "user1"})).json()
        # Should have parent + 2 subtask escrows
        assert len(esc["escrows"]) >= 1

        # Parent remaining
        parent = (await client.get("/api/tasks/msub-parent")).json()
        assert parent["remaining_budget"] == 250.0


class TestConcurrentDeposits:
    """Multiple deposits happening concurrently."""

    @pytest.mark.asyncio
    async def test_10_concurrent_deposits(self, client):
        """10 deposits of 100 each → balance = 1000."""
        results = await asyncio.gather(*[
            client.post("/api/economy/deposit", json={
                "agent_id": "dep-agent", "amount": 100.0,
            })
            for _ in range(10)
        ])
        assert all(r.status_code == 200 for r in results)

        bal = await _bal(client, "dep-agent")
        assert bal["available"] == 1000.0


class TestPlatformFeeAccumulation:
    """Verify platform fees accumulate across settlements."""

    @pytest.mark.asyncio
    async def test_fees_from_3_settlements(self, client, funded_network):
        """3 tasks settled → platform fees = 3 × price × 5%."""
        net = funded_network
        for i in range(3):
            aid = f"fee-exec-{i}"
            net.reputation._scores[aid] = 0.8
            await net.dht.announce("coding", aid)

        for i in range(3):
            await create_task(client, task_id=f"fee-{i}", budget=200.0)
            await bid(client, task_id=f"fee-{i}", agent_id=f"fee-exec-{i}", price=100.0)
            await submit_result(client, task_id=f"fee-{i}", agent_id=f"fee-exec-{i}")
            await close_task(client, task_id=f"fee-{i}")
            await select_result(client, task_id=f"fee-{i}", agent_id=f"fee-exec-{i}")

        # Platform should have collected 3 × 100 × 0.05 = 15
        assert net.settlement.total_fees_collected == 15.0


class TestReputationIntegration:
    """Reputation effects on bidding and scoring."""

    @pytest.mark.asyncio
    async def test_selection_boosts_reputation(self, client, funded_network):
        """Selecting a result boosts both selector and selected reputation."""
        net = funded_network
        net.reputation._scores["rep-exec"] = 0.7  # High enough for admission
        net.reputation._scores["user1"] = 0.7
        await net.dht.announce("coding", "rep-exec")

        before_exec = net.reputation.get_score("rep-exec")
        before_user = net.reputation.get_score("user1")

        await create_task(client, task_id="rep-t1", budget=200.0)
        await bid(client, task_id="rep-t1", agent_id="rep-exec", price=80.0)
        await submit_result(client, task_id="rep-t1", agent_id="rep-exec")
        await close_task(client, task_id="rep-t1")
        await select_result(client, task_id="rep-t1", agent_id="rep-exec")

        after_exec = net.reputation.get_score("rep-exec")
        after_user = net.reputation.get_score("user1")

        assert after_exec > before_exec, "Selected agent should gain reputation"
        assert after_user > before_user, "Selector should gain reputation"

    @pytest.mark.asyncio
    async def test_burst_detection_blocks_spam(self, funded_network):
        """Rapid same-type events trigger anomaly detection."""
        net = funded_network
        score_before = net.reputation.get_score("spammer")

        # Submit 10 identical events rapidly (burst threshold = 8)
        for _ in range(10):
            await net.reputation.aggregate(
                "spammer",
                [{"type": "result_selected"}],
                server_id="srv-1",
            )

        score_after = net.reputation.get_score("spammer")
        # After burst threshold, events should be blocked
        # The score shouldn't increase as much as 10 × weight
        max_possible = score_before + 10 * 0.10  # 10 × result_selected weight
        assert score_after < max_possible, "Burst detection should limit score increase"

    @pytest.mark.asyncio
    async def test_reputation_event_via_api(self, client, funded_network):
        """POST /reputation/events updates score correctly."""
        resp = await client.post("/api/reputation/events", json={
            "agent_id": "api-rep", "event_type": "result_selected",
            "server_id": "srv-test",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["score"] > 0.5  # Should increase from default

    @pytest.mark.asyncio
    async def test_get_reputation(self, client, funded_network):
        net = funded_network
        net.reputation._scores["known-agent"] = 0.75

        resp = await client.get("/api/reputation/known-agent")
        assert resp.status_code == 200
        assert resp.json()["score"] == 0.75
