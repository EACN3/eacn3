"""Escrow accounting precision tests.

Tests that money is never created, destroyed, or misattributed:
- Freeze → settle → refund arithmetic
- Subtask escrow chain accounting
- Multi-settlement with fee tracking
- Edge cases: zero amounts, exact budget bids
"""

import pytest
from eacn.network.economy.account import Account
from eacn.network.economy.escrow import EscrowService
from eacn.network.economy.settlement import SettlementService
from eacn.core.exceptions import BudgetError
from eacn.network.db.database import Database


@pytest.fixture
async def econ():
    db = Database()
    await db.connect()
    escrow = EscrowService(db=db)
    settlement = SettlementService(escrow, platform_fee_rate=0.05)

    # Fund accounts
    escrow.get_or_create_account("user-a", 10_000.0)
    escrow.get_or_create_account("user-b", 5_000.0)
    escrow.get_or_create_account("exec-1", 0.0)
    escrow.get_or_create_account("exec-2", 0.0)

    yield {"escrow": escrow, "settlement": settlement}
    await db.close()


class TestFreezeAndRelease:
    @pytest.mark.asyncio
    async def test_freeze_reduces_available(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-a", "t1", 500.0)
        acct = e.get_account("user-a")
        assert acct.available == 9500.0
        assert acct.frozen == 500.0

    @pytest.mark.asyncio
    async def test_release_restores_available(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-a", "t1", 500.0)
        refund = await e.release("t1")
        assert refund == 500.0
        acct = e.get_account("user-a")
        assert acct.available == 10_000.0
        assert acct.frozen == 0.0

    @pytest.mark.asyncio
    async def test_freeze_insufficient_raises(self, econ):
        e = econ["escrow"]
        with pytest.raises(BudgetError):
            await e.freeze_budget("user-a", "t1", 20_000.0)

    @pytest.mark.asyncio
    async def test_release_nonexistent_returns_zero(self, econ):
        e = econ["escrow"]
        assert await e.release("ghost") == 0.0


class TestSettlementAccounting:
    @pytest.mark.asyncio
    async def test_basic_settlement(self, econ):
        e, s = econ["escrow"], econ["settlement"]
        await e.freeze_budget("user-a", "t1", 1000.0)

        result = await s.settle("t1", "exec-1", 500.0)

        # Executor gets bid_price
        assert e.get_account("exec-1").available == 500.0
        # Fee = 500 × 5% = 25
        assert result.platform_fee == 25.0
        # Refund = 1000 - 500 - 25 = 475
        assert result.refund == 475.0
        # User: 10000 - 1000 + 475 = 9475
        acct = e.get_account("user-a")
        assert acct.available == 9475.0
        assert acct.frozen == 0.0

    @pytest.mark.asyncio
    async def test_settlement_conserves_money(self, econ):
        """Total money in system decreases by exactly platform_fee."""
        e, s = econ["escrow"], econ["settlement"]

        total_before = sum(a.total for a in e._accounts.values())
        await e.freeze_budget("user-a", "t1", 1000.0)
        result = await s.settle("t1", "exec-1", 500.0)
        total_after = sum(a.total for a in e._accounts.values())

        assert abs(total_after - (total_before - result.platform_fee)) < 0.001

    @pytest.mark.asyncio
    async def test_double_settlement_blocked(self, econ):
        e, s = econ["escrow"], econ["settlement"]
        await e.freeze_budget("user-a", "t1", 1000.0)
        await s.settle("t1", "exec-1", 500.0)

        with pytest.raises(BudgetError, match="already settled"):
            await s.settle("t1", "exec-1", 500.0)

    @pytest.mark.asyncio
    async def test_multiple_settlements(self, econ):
        """3 tasks settled correctly."""
        e, s = econ["escrow"], econ["settlement"]

        for i in range(3):
            await e.freeze_budget("user-a", f"ms-{i}", 500.0)
            await s.settle(f"ms-{i}", "exec-1", 200.0)

        # exec-1: 3 × 200 = 600
        assert e.get_account("exec-1").available == 600.0
        # Total fees: 3 × 200 × 0.05 = 30
        assert s.total_fees_collected == 30.0
        # user-a: 10000 - 3×500 + 3×(500 - 200 - 10) = 10000 - 1500 + 870 = 9370
        assert e.get_account("user-a").available == 9370.0


class TestSubtaskEscrowAccounting:
    @pytest.mark.asyncio
    async def test_subtask_escrow_from_parent(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-a", "parent", 1000.0)
        await e.allocate_subtask_budget("parent", "sub1", "exec-1", 300.0)

        # Parent escrow reduced
        assert e.get_escrowed_amount("parent") == 700.0
        # Subtask escrow created (attributed to parent initiator)
        assert e.get_escrowed_amount("sub1") == 300.0

    @pytest.mark.asyncio
    async def test_subtask_release_refunds_parent_initiator(self, econ):
        """Subtask refund goes to the original initiator, not the subtask creator (#16)."""
        e = econ["escrow"]
        await e.freeze_budget("user-a", "parent", 1000.0)
        await e.allocate_subtask_budget("parent", "sub1", "exec-1", 300.0)

        refund = await e.release("sub1")
        assert refund == 300.0
        # user-a gets the refund (not exec-1)
        acct = e.get_account("user-a")
        assert acct.available == 9000.0 + 300.0  # 9000 (after freeze 1000) + 300 refund

    @pytest.mark.asyncio
    async def test_subtask_over_parent_escrow_fails(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-a", "parent", 500.0)
        with pytest.raises(BudgetError, match="exceeds parent"):
            await e.allocate_subtask_budget("parent", "sub1", "exec-1", 600.0)


class TestBudgetConfirmation:
    @pytest.mark.asyncio
    async def test_confirm_increase(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-a", "t1", 500.0)
        await e.confirm_budget_increase("user-a", "t1", 200.0)

        assert e.get_escrowed_amount("t1") == 700.0
        acct = e.get_account("user-a")
        assert acct.frozen == 700.0
        assert acct.available == 9300.0

    @pytest.mark.asyncio
    async def test_confirm_increase_insufficient(self, econ):
        e = econ["escrow"]
        await e.freeze_budget("user-b", "t1", 4000.0)
        with pytest.raises(BudgetError):
            await e.confirm_budget_increase("user-b", "t1", 2000.0)  # Only 1000 available
