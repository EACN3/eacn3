"""Settlement: pay winning bid, deduct platform fee, refund remainder.

Flow:
1. Initiator calls select_result(task_id, result_id, agent_id)
2. Economy: pay selected executor their bid price
3. Deduct platform fee (fixed % of payment)
4. Refund remaining budget to initiator
"""

from __future__ import annotations

from eacn.core.exceptions import BudgetError
from eacn.network.economy.escrow import EscrowService


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
        self._settled: set[str] = set()

    async def settle(
        self,
        task_id: str,
        executor_id: str,
        bid_price: float,
    ) -> "SettlementResult":
        """Full settlement flow for a task.

        1. Calculate platform fee
        2. Deduct (bid_price + fee) from escrow
        3. Credit executor with bid_price
        4. Refund remainder to initiator
        """
        # Idempotency guard: prevent double settlement
        if task_id in self._settled:
            raise BudgetError(f"Task {task_id} already settled")

        platform_fee = bid_price * self.platform_fee_rate
        total_deduction = bid_price + platform_fee

        # Deduct from escrow
        initiator_id = await self.escrow.deduct_for_settlement(task_id, total_deduction)

        # Credit executor — wrap in try to rollback on failure
        try:
            executor_account = self.escrow.get_or_create_account(executor_id)
            executor_account.credit(bid_price)
            await self.escrow._persist_account(executor_id)

            # Refund remainder
            refund = await self.escrow.release(task_id)
        except Exception:
            # Rollback executor credit if release or persist failed
            if executor_account:
                executor_account.available -= bid_price
            raise

        # Track platform fees and mark as settled AFTER all mutations succeed
        self.total_fees_collected += platform_fee
        self._settled.add(task_id)

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
