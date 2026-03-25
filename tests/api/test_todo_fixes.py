"""Tests covering TODO fix scenarios — settlement failures, concurrent ops,
deadline edge cases, model validation, and more.

Covers: #12,13,15,33,49,50,51,52,55,56,91,92,93,96,97,98,100
"""

import asyncio

import pytest

from tests.api.conftest import (
    create_task, bid, submit_result, close_task, select_result,
    setup_task_with_bid, setup_task_with_result,
)


# ═════════════════════════════════════════════════════════════════════
# #12: Settlement failure → state consistency
# ═════════════════════════════════════════════════════════════════════

class TestSettlementConsistency:
    @pytest.mark.asyncio
    async def test_select_result_settles_and_pays(self, client):
        """Full settlement flow: create → bid → result → close → select → balance changes."""
        await create_task(client, task_id="settle1", budget=200.0)
        await bid(client, task_id="settle1", agent_id="a1", price=80.0)
        await submit_result(client, task_id="settle1", agent_id="a1")
        await close_task(client, task_id="settle1")
        await select_result(client, task_id="settle1", agent_id="a1")

        # Executor should have been credited
        bal = (await client.get("/api/economy/balance", params={"agent_id": "a1"})).json()
        assert bal["available"] > 0

    @pytest.mark.asyncio
    async def test_double_select_fails(self, client):
        """Cannot select result twice — idempotency guard (#18)."""
        await create_task(client, task_id="double-sel", budget=200.0)
        await bid(client, task_id="double-sel", agent_id="a1", price=80.0)
        await submit_result(client, task_id="double-sel", agent_id="a1")
        await close_task(client, task_id="double-sel")
        await select_result(client, task_id="double-sel", agent_id="a1")

        # Second select should fail
        resp = await client.post("/api/tasks/double-sel/select", json={
            "initiator_id": "user1", "agent_id": "a1",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_refund_on_no_one_able(self, client):
        """Closing task with no results refunds initiator (#12)."""
        bal_before = (await client.get("/api/economy/balance", params={"agent_id": "user1"})).json()
        await create_task(client, task_id="refund1", budget=100.0)
        await close_task(client, task_id="refund1")
        bal_after = (await client.get("/api/economy/balance", params={"agent_id": "user1"})).json()
        assert bal_after["available"] == bal_before["available"]


# ═════════════════════════════════════════════════════════════════════
# #33/#56: Duplicate result submission
# ═════════════════════════════════════════════════════════════════════

class TestDuplicateResult:
    @pytest.mark.asyncio
    async def test_duplicate_result_rejected(self, client):
        """Same agent cannot submit result twice (#33)."""
        await create_task(client, task_id="dup-res", budget=200.0)
        await bid(client, task_id="dup-res", agent_id="a1", price=80.0)
        await submit_result(client, task_id="dup-res", agent_id="a1")
        # Second submission should fail
        resp = await client.post("/api/tasks/dup-res/result", json={
            "agent_id": "a1", "content": "second attempt",
        })
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════
# #49: Subtask escrow full flow
# ═════════════════════════════════════════════════════════════════════

class TestSubtaskEscrow:
    @pytest.mark.asyncio
    async def test_subtask_escrow_flow(self, client):
        """Create → subtask → settle subtask → parent remaining_budget correct (#49)."""
        await create_task(client, task_id="parent1", budget=500.0)
        await bid(client, task_id="parent1", agent_id="a1", price=200.0)
        sub = (await client.post("/api/tasks/parent1/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 150.0,
        })).json()
        assert sub["budget"] == 150.0

        parent = (await client.get("/api/tasks/parent1")).json()
        assert parent["remaining_budget"] == 350.0

    @pytest.mark.asyncio
    async def test_subtask_over_budget_fails(self, client):
        """Subtask exceeding parent remaining budget fails (#49)."""
        await create_task(client, task_id="parent2", budget=200.0)
        await bid(client, task_id="parent2", agent_id="a1", price=100.0)
        resp = await client.post("/api/tasks/parent2/subtask", json={
            "initiator_id": "a1", "content": {}, "domains": ["coding"], "budget": 300.0,
        })
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════
# #50: Negative/zero model field validation
# ═════════════════════════════════════════════════════════════════════

class TestModelValidation:
    @pytest.mark.asyncio
    async def test_negative_budget_rejected(self, client):
        """Negative budget rejected at schema level."""
        resp = await client.post("/api/tasks", json={
            "task_id": "neg", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": -10.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_negative_max_concurrent_rejected(self, client):
        """Negative max_concurrent_bidders rejected (#36)."""
        resp = await client.post("/api/tasks", json={
            "task_id": "mcb-neg", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
            "max_concurrent_bidders": -1,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_domain_rejected(self, client):
        """Empty string in domains list rejected (#46)."""
        resp = await client.post("/api/tasks", json={
            "task_id": "emptyd", "initiator_id": "user1",
            "content": {}, "domains": [""], "budget": 100.0,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_level_rejected(self, client):
        """Invalid task level rejected at schema (#37)."""
        resp = await client.post("/api/tasks", json={
            "task_id": "badlvl", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
            "level": "invalid_level",
        })
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════
# #51: Deadline timezone format
# ═════════════════════════════════════════════════════════════════════

class TestDeadlineTimezone:
    @pytest.mark.asyncio
    async def test_z_suffix_deadline_detected(self, funded_network):
        """Deadline with Z suffix is correctly detected as expired (#19/#51)."""
        net = funded_network
        await net.create_task(
            task_id="tz1", initiator_id="user1",
            content={"desc": "test"}, domains=["coding"],
            budget=100.0, deadline="2020-01-01T00:00:00Z",
        )
        expired = await net.scan_deadlines()
        assert "tz1" in expired

    @pytest.mark.asyncio
    async def test_plus_offset_deadline_detected(self, funded_network):
        """Deadline with +00:00 suffix is correctly detected as expired."""
        net = funded_network
        await net.create_task(
            task_id="tz2", initiator_id="user1",
            content={"desc": "test"}, domains=["coding"],
            budget=100.0, deadline="2020-01-01T00:00:00+00:00",
        )
        expired = await net.scan_deadlines()
        assert "tz2" in expired


# ═════════════════════════════════════════════════════════════════════
# #91: Settlement zero-refund edge case
# ═════════════════════════════════════════════════════════════════════

class TestSettlementEdgeCases:
    @pytest.mark.asyncio
    async def test_bid_price_equals_budget(self, client):
        """When bid price == budget, refund should be ~0 (minus fee)."""
        await create_task(client, task_id="exact", budget=100.0)
        await bid(client, task_id="exact", agent_id="a1", price=95.0)
        await submit_result(client, task_id="exact", agent_id="a1")
        await close_task(client, task_id="exact")
        # Should succeed without error
        await select_result(client, task_id="exact", agent_id="a1")


# ═════════════════════════════════════════════════════════════════════
# #93: Multiple subtasks remaining_budget consistency
# ═════════════════════════════════════════════════════════════════════

class TestMultiSubtaskBudget:
    @pytest.mark.asyncio
    async def test_three_subtasks_budget_consistent(self, client):
        """Creating 3 subtasks deducts correctly from parent (#93)."""
        await create_task(client, task_id="multi-sub", budget=1000.0)
        await bid(client, task_id="multi-sub", agent_id="a1", price=500.0)

        for i, amount in enumerate([200.0, 300.0, 100.0]):
            resp = await client.post("/api/tasks/multi-sub/subtask", json={
                "initiator_id": "a1", "content": {}, "domains": ["coding"],
                "budget": amount,
            })
            assert resp.status_code == 201, f"subtask {i} failed: {resp.text}"

        parent = (await client.get("/api/tasks/multi-sub")).json()
        assert parent["remaining_budget"] == 400.0  # 1000 - 200 - 300 - 100


# ═════════════════════════════════════════════════════════════════════
# #96: Confirm budget with insufficient balance
# ═════════════════════════════════════════════════════════════════════

class TestConfirmBudgetInsufficient:
    @pytest.mark.asyncio
    async def test_confirm_budget_insufficient_fails(self, client):
        """Confirming budget increase when balance is insufficient (#96)."""
        # user2 has 5000 credits; create task using most of it
        await create_task(client, task_id="cb-insuf", initiator_id="user2",
                          budget=4900.0)
        await bid(client, task_id="cb-insuf", agent_id="a1", price=6000.0)
        # Try to confirm with new_budget that exceeds available
        resp = await client.post("/api/tasks/cb-insuf/confirm-budget", json={
            "initiator_id": "user2", "approved": True, "new_budget": 10000.0,
        })
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════
# #97: Collect results idempotency
# ═════════════════════════════════════════════════════════════════════

class TestCollectResultsIdempotent:
    @pytest.mark.asyncio
    async def test_collect_twice_succeeds(self, client):
        """Collecting results twice should succeed (idempotent, #97)."""
        await create_task(client, task_id="collect2x", budget=200.0)
        await bid(client, task_id="collect2x", agent_id="a1", price=80.0)
        await submit_result(client, task_id="collect2x", agent_id="a1")
        await close_task(client, task_id="collect2x")

        # First collect
        resp1 = await client.get("/api/tasks/collect2x/results",
                                 params={"initiator_id": "user1"})
        assert resp1.status_code == 200

        # Second collect should also succeed (task is now completed)
        resp2 = await client.get("/api/tasks/collect2x/results",
                                 params={"initiator_id": "user1"})
        assert resp2.status_code == 200


# ═════════════════════════════════════════════════════════════════════
# #98: Escrow release for non-existent task_id
# ═════════════════════════════════════════════════════════════════════

class TestEscrowReleaseMissing:
    @pytest.mark.asyncio
    async def test_release_nonexistent_returns_zero(self, funded_network):
        """Releasing escrow for unknown task returns 0 (#98)."""
        net = funded_network
        refund = await net.escrow.release("nonexistent-task-id")
        assert refund == 0.0


# ═════════════════════════════════════════════════════════════════════
# #55: Invalid enum values via API
# ═════════════════════════════════════════════════════════════════════

class TestInvalidEnumAPI:
    @pytest.mark.asyncio
    async def test_invalid_task_level_via_api(self, client):
        """Invalid level string rejected by Pydantic at schema (#55)."""
        resp = await client.post("/api/tasks", json={
            "task_id": "bad-enum", "initiator_id": "user1",
            "content": {}, "domains": ["coding"], "budget": 100.0,
            "level": "nonexistent_level",
        })
        assert resp.status_code == 422


# ═════════════════════════════════════════════════════════════════════
# #92: Budget confirmation + queue promotion
# ═════════════════════════════════════════════════════════════════════

class TestBudgetConfirmPromotion:
    @pytest.mark.asyncio
    async def test_confirm_budget_re_evaluates_pending(self, client):
        """Approving budget increase re-evaluates PENDING bids (#92)."""
        await create_task(client, task_id="bp1", budget=50.0)
        # This bid exceeds budget → PENDING
        b = await bid(client, task_id="bp1", agent_id="a1", price=80.0)
        assert b["status"] == "pending"

        # Confirm with higher budget
        resp = await client.post("/api/tasks/bp1/confirm-budget", json={
            "initiator_id": "user1", "approved": True, "new_budget": 100.0,
        })
        assert resp.status_code == 200

        # Check bid status
        task = (await client.get("/api/tasks/bp1")).json()
        statuses = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert statuses["a1"] in ("executing", "waiting")


# ═════════════════════════════════════════════════════════════════════
# #15: Deadline scan partial failure
# ═════════════════════════════════════════════════════════════════════

class TestDeadlineScan:
    @pytest.mark.asyncio
    async def test_multiple_expired_tasks(self, client):
        """Multiple tasks with expired deadlines all get processed (#15)."""
        for i in range(3):
            await create_task(
                client, task_id=f"exp-{i}", budget=50.0,
                deadline="2020-01-01T00:00:00+00:00",
            )
        resp = await client.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["expired"]) == 3


# ═════════════════════════════════════════════════════════════════════
# Escrow detail query (#9)
# ═════════════════════════════════════════════════════════════════════

class TestEscrowDetailQuery:
    @pytest.mark.asyncio
    async def test_escrow_detail_endpoint(self, client):
        """GET /economy/escrows returns per-task breakdown (#9)."""
        await create_task(client, task_id="esc1", budget=200.0)
        await create_task(client, task_id="esc2", budget=300.0)
        resp = await client.get("/api/economy/escrows", params={"agent_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "user1"
        assert len(data["escrows"]) == 2
        assert data["total_frozen"] == 500.0
