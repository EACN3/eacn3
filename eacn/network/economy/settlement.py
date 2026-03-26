"""Settlement: pay winning bid, deduct platform fee, refund remainder.

Flow:
1. Initiator calls select_result(task_id, result_id, agent_id)
2. Economy: pay selected executor their bid price
3. Deduct platform fee (fixed % of payment)
4. Refund remaining budget to initiator
"""

from __future__ import annotations

from collections import OrderedDict

from eacn.core.exceptions import BudgetError
from eacn.network.economy.escrow import EscrowService

# Max settled task IDs to remember for idempotency
_MAX_SETTLED = 10_000


class SettlementService:
    """Handles payment settlement upon result selection."""

    def __init__(
        self,
        escrow: EscrowService,
        platform_fee_rate: float = 0.05,
    ) -> None:
        self.escrow = escrow
        self.platform_fee_rate = platform_fee_rate
        self.total_fees_collected: float = 0.0
        # Idempotency: track settled task_ids to prevent double payment (#18)
        # Uses OrderedDict as bounded LRU to prevent unbounded memory growth
        self._settled: OrderedDict[str, bool] = OrderedDict()

    async def settle(
        self,
        task_id: str,
        executor_id: str,
        bid_price: float,
    ) -> "SettlementResult":
        """Full settlement flow for a task.

        1. Deduct bid_price from escrow (always ≤ budget, so always fits)
        2. Platform fee is taken FROM bid_price (not on top)
        3. Credit executor with bid_price - fee
        4. Refund remainder to initiator
        """
        # Idempotency guard: prevent double settlement
        if task_id in self._settled:  # O(1) lookup in OrderedDict
            raise BudgetError(f"Task {task_id} already settled")

        # When the executor created subtasks, their budgets were carved
        # out of this task's escrow.  Cap settlement at what remains.
        escrowed = self.escrow.get_escrowed_amount(task_id)
        effective_price = min(bid_price, escrowed)

        platform_fee = effective_price * self.platform_fee_rate
        executor_payout = effective_price - platform_fee

        # Deduct from escrow (fee is taken from within the price)
        initiator_id = await self.escrow.deduct_for_settlement(task_id, effective_price)

        # Credit executor — wrap in try to rollback on failure
        try:
            executor_account = self.escrow.get_or_create_account(executor_id)
            executor_account.credit(executor_payout)
            await self.escrow._persist_account(executor_id)

            # Refund remainder
            refund = await self.escrow.release(task_id)
        except Exception:
            # Rollback executor credit if release or persist failed
            if executor_account:
                executor_account.available -= executor_payout
            raise

        # Track platform fees and mark as settled AFTER all mutations succeed
        self.total_fees_collected += platform_fee
        self._settled[task_id] = True
        # Evict oldest entries to prevent unbounded growth
        while len(self._settled) > _MAX_SETTLED:
            self._settled.popitem(last=False)

        return SettlementResult(
            task_id=task_id,
            executor_id=executor_id,
            initiator_id=initiator_id,
            bid_price=bid_price,
            platform_fee=platform_fee,
            refund=refund,
        )

    async def refund_no_one_capable(self, task_id: str) -> float:
        """Refund entire escrow when no one can complete the task."""
        return await self.escrow.release(task_id)


class SettlementResult:
    """Settlement outcome details."""

    __slots__ = (
        "task_id", "executor_id", "initiator_id",
        "bid_price", "platform_fee", "refund",
    )

    def __init__(
        self,
        task_id: str,
        executor_id: str,
        initiator_id: str,
        bid_price: float,
        platform_fee: float,
        refund: float,
    ) -> None:
        self.task_id = task_id
        self.executor_id = executor_id
        self.initiator_id = initiator_id
        self.bid_price = bid_price
        self.platform_fee = platform_fee
        self.refund = refund
