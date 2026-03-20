"""Escrow: budget freezing, subtask allocation, release, and refund.

Financial flows:
- Task creation: freeze initiator's budget → escrow
- Subtask creation: transfer from parent escrow → child escrow
- Settlement: deduct from escrow → pay executor + platform fee
- No-one-capable: refund entire escrow → initiator
"""

from __future__ import annotations

from eacn.core.exceptions import BudgetError
from eacn.network.economy.account import Account


class EscrowService:
    """Manages fund locking during task lifecycle."""

    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        # task_id → (initiator_id, escrowed_amount)
        self._task_escrows: dict[str, tuple[str, float]] = {}

    def get_or_create_account(
        self, agent_id: str, initial_balance: float = 0.0
    ) -> Account:
        if agent_id not in self._accounts:
            self._accounts[agent_id] = Account(agent_id, initial_balance)
        return self._accounts[agent_id]

    def get_account(self, agent_id: str) -> Account | None:
        return self._accounts.get(agent_id)

    # ── Task creation: freeze budget ─────────────────────────────────

    def freeze_budget(
        self, initiator_id: str, task_id: str, amount: float
    ) -> None:
        """Freeze budget to escrow on task creation.

        Raises BudgetError if insufficient balance.
        """
        account = self.get_or_create_account(initiator_id)
        account.freeze(amount)
        self._task_escrows[task_id] = (initiator_id, amount)

    def get_escrowed_amount(self, task_id: str) -> float:
        """Get the amount held in escrow for a task."""
        entry = self._task_escrows.get(task_id)
        return entry[1] if entry else 0.0

    # ── Subtask budget allocation ────────────────────────────────────

    def allocate_subtask_budget(
        self,
        parent_task_id: str,
        subtask_id: str,
        subtask_initiator_id: str,
        amount: float,
    ) -> None:
        """Transfer budget from parent escrow → subtask escrow.

        The executor (subtask initiator) decides how to split parent budget.
        """
        parent_entry = self._task_escrows.get(parent_task_id)
        if not parent_entry:
            raise BudgetError(f"No escrow found for parent task {parent_task_id}")

        parent_initiator, parent_amount = parent_entry
        if amount > parent_amount:
            raise BudgetError(
                f"Subtask budget {amount} exceeds parent escrow {parent_amount}"
            )

        # Reduce parent escrow, create child escrow
        self._task_escrows[parent_task_id] = (
            parent_initiator,
            parent_amount - amount,
        )
        self._task_escrows[subtask_id] = (subtask_initiator_id, amount)

    # ── Budget confirmation (initiator approves over-budget bid) ─────

    def confirm_budget_increase(
        self, initiator_id: str, task_id: str, additional: float
    ) -> None:
        """Initiator confirms additional budget for an over-budget bid."""
        account = self.get_or_create_account(initiator_id)
        account.freeze(additional)

        entry = self._task_escrows.get(task_id)
        if entry:
            old_initiator, old_amount = entry
            self._task_escrows[task_id] = (old_initiator, old_amount + additional)
        else:
            self._task_escrows[task_id] = (initiator_id, additional)

    # ── Release / refund ─────────────────────────────────────────────

    def release(self, task_id: str) -> float:
        """Release frozen funds back to initiator (no-one-capable refund).

        Returns the refunded amount.
        """
        entry = self._task_escrows.pop(task_id, None)
        if not entry:
            return 0.0

        initiator_id, amount = entry
        account = self._accounts.get(initiator_id)
        if account and amount > 0:
            account.unfreeze(amount)
        return amount

    def deduct_for_settlement(
        self, task_id: str, amount: float
    ) -> str:
        """Deduct from escrow for settlement. Returns initiator_id."""
        entry = self._task_escrows.get(task_id)
        if not entry:
            raise BudgetError(f"No escrow found for task {task_id}")

        initiator_id, escrowed = entry
        if amount > escrowed:
            raise BudgetError(
                f"Settlement {amount} exceeds escrow {escrowed}"
            )

        account = self._accounts.get(initiator_id)
        if account:
            account.deduct_frozen(amount)

        self._task_escrows[task_id] = (initiator_id, escrowed - amount)
        return initiator_id
