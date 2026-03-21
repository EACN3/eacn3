"""Tests: Economy endpoints via Network HTTP API.

Covers:
  GET  /api/economy/balance  — query account balance
  POST /api/economy/deposit  — top-up funds
  Integration with task lifecycle (deposit → create task → verify frozen)
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task, select_result


# ══════════════════════════════════════════════════════════════════════
# GET /api/economy/balance
# ══════════════════════════════════════════════════════════════════════

class TestGetBalance:
    @pytest.mark.asyncio
    async def test_funded_account(self, client):
        """user1 has 10 000 pre-funded credits."""
        resp = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "user1"
        assert data["available"] == 10_000.0
        assert data["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_unknown_agent_404(self, client):
        resp = await client.get("/api/economy/balance", params={"agent_id": "nobody"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_param_422(self, client):
        resp = await client.get("/api/economy/balance")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_second_funded_account(self, client):
        """user2 has 5 000 pre-funded credits."""
        resp = await client.get("/api/economy/balance", params={"agent_id": "user2"})
        assert resp.status_code == 200
        assert resp.json()["available"] == 5_000.0


# ══════════════════════════════════════════════════════════════════════
# POST /api/economy/deposit
# ══════════════════════════════════════════════════════════════════════

class TestDeposit:
    @pytest.mark.asyncio
    async def test_deposit_existing_account(self, client):
        resp = await client.post(
            "/api/economy/deposit",
            json={"agent_id": "user1", "amount": 500.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "user1"
        assert data["deposited"] == 500.0
        assert data["available"] == 10_500.0
        assert data["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_deposit_creates_new_account(self, client):
        """Deposit to an unknown agent_id creates the account."""
        resp = await client.post(
            "/api/economy/deposit",
            json={"agent_id": "new_user", "amount": 300.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "new_user"
        assert data["deposited"] == 300.0
        assert data["available"] == 300.0

        # Verify via balance endpoint
        bal = await client.get("/api/economy/balance", params={"agent_id": "new_user"})
        assert bal.status_code == 200
        assert bal.json()["available"] == 300.0

    @pytest.mark.asyncio
    async def test_deposit_zero_rejected(self, client):
        """amount must be > 0."""
        resp = await client.post(
            "/api/economy/deposit",
            json={"agent_id": "user1", "amount": 0.0},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_deposit_negative_rejected(self, client):
        resp = await client.post(
            "/api/economy/deposit",
            json={"agent_id": "user1", "amount": -100.0},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_multiple_deposits_accumulate(self, client):
        await client.post("/api/economy/deposit", json={"agent_id": "user2", "amount": 100.0})
        await client.post("/api/economy/deposit", json={"agent_id": "user2", "amount": 200.0})
        bal = await client.get("/api/economy/balance", params={"agent_id": "user2"})
        assert bal.json()["available"] == 5_300.0  # 5000 + 100 + 200


# ══════════════════════════════════════════════════════════════════════
# Integration: Economy + Task lifecycle
# ══════════════════════════════════════════════════════════════════════

class TestEconomyTaskIntegration:
    @pytest.mark.asyncio
    async def test_balance_frozen_after_task_creation(self, client):
        """Creating a task freezes the budget from available balance."""
        await create_task(client, task_id="t1", budget=500.0)

        resp = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        data = resp.json()
        assert data["available"] == 9_500.0  # 10000 - 500
        assert data["frozen"] == 500.0

    @pytest.mark.asyncio
    async def test_deposit_then_create_task(self, client):
        """Deposit funds, then create task — full plugin workflow."""
        # Deposit
        dep = await client.post(
            "/api/economy/deposit",
            json={"agent_id": "user1", "amount": 1_000.0},
        )
        assert dep.status_code == 200

        # Check balance after deposit
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert bal.json()["available"] == 11_000.0

        # Create task
        await create_task(client, task_id="t1", budget=2_000.0)

        # Verify frozen
        bal2 = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert bal2.json()["available"] == 9_000.0
        assert bal2.json()["frozen"] == 2_000.0

    @pytest.mark.asyncio
    async def test_full_lifecycle_balance_settlement(self, client):
        """Full flow: check balance → create task → bid → result → close → select → verify settlement."""
        # 1. Check initial balance
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert bal.json()["available"] == 10_000.0

        # 2. Create task (freezes 200)
        await create_task(client, task_id="t1", budget=200.0)
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert bal.json()["available"] == 9_800.0
        assert bal.json()["frozen"] == 200.0

        # 3. Bid + submit result
        await bid(client, task_id="t1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="t1", agent_id="a1")

        # 4. Close and select
        await close_task(client, task_id="t1")
        await select_result(client, task_id="t1", agent_id="a1")

        # 5. After settlement, frozen should decrease
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        data = bal.json()
        # Settlement deducts bid price (80) from frozen, refunds remainder
        # Frozen should be less than 200 now
        assert data["frozen"] < 200.0

    @pytest.mark.asyncio
    async def test_insufficient_balance_blocks_task(self, api):
        """Task creation fails when balance is insufficient (unfunded client)."""
        # api fixture has unfunded network — no accounts
        resp = await api.post("/api/tasks", json={
            "task_id": "t1",
            "initiator_id": "broke_user",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        # Should fail with 402 (BudgetError → insufficient funds)
        assert resp.status_code == 402

    @pytest.mark.asyncio
    async def test_deposit_then_retry_task(self, api):
        """Deposit after insufficient balance, then retry task creation."""
        # First attempt fails
        resp = await api.post("/api/tasks", json={
            "task_id": "t1",
            "initiator_id": "new_agent",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 402

        # Deposit funds
        dep = await api.post(
            "/api/economy/deposit",
            json={"agent_id": "new_agent", "amount": 500.0},
        )
        assert dep.status_code == 200

        # Retry task creation
        resp = await api.post("/api/tasks", json={
            "task_id": "t1",
            "initiator_id": "new_agent",
            "content": {"desc": "test"},
            "domains": ["coding"],
            "budget": 100.0,
        })
        assert resp.status_code == 201

        # Verify balance reflects freeze
        bal = await api.get("/api/economy/balance", params={"agent_id": "new_agent"})
        assert bal.json()["available"] == 400.0
        assert bal.json()["frozen"] == 100.0
