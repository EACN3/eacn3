"""Escrow: budget freezing, subtask allocation, release, and refund.

Financial flows:
- Task creation: freeze initiator's budget → escrow
- Subtask creation: transfer from parent escrow → child escrow
- Settlement: deduct from escrow → pay executor + platform fee
- No-one-capable: refund entire escrow → initiator

All mutations are persisted to the database when a db reference is provided.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from eacn.core.exceptions import BudgetError
from eacn.network.economy.account import Account

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class EscrowService:
    """Manages fund locking during task lifecycle."""

    def __init__(self, db: "Database | None" = None) -> None:
        self._db = db
        self._accounts: dict[str, Account] = {}
        # task_id → (initiator_id, escrowed_amount)
        self._task_escrows: dict[str, tuple[str, float]] = {}

    async def load_from_db(self) -> None:
        """Restore accounts and escrows from the database."""
        if not self._db:
            return
        for row in await self._db.list_all_accounts():
            acct = Account(row["agent_id"], row["available"])
            acct.frozen = row["frozen"]
            self._accounts[row["agent_id"]] = acct
        for task_id, initiator_id, amount in await self._db.list_all_escrows():
            self._task_escrows[task_id] = (initiator_id, amount)
        _log.info(
            "Loaded %d accounts, %d escrows from DB",
            len(self._accounts), len(self._task_escrows),
        )

    # ── DB sync helpers ───────────────────────────────────────────────

    async def _persist_account(self, agent_id: str) -> None:
        if not self._db:
            return
        acct = self._accounts.get(agent_id)
        if acct:
            try:
                await self._db.upsert_account(agent_id, acct.available, acct.frozen)
            except Exception:
                _log.error(
                    "Failed to persist account %s (available=%s, frozen=%s)",
                    agent_id, acct.available, acct.frozen,
                    exc_info=True,
                )
                raise

    async def _persist_escrow(self, task_id: str) -> None:
        if not self._db:
            return
        entry = self._task_escrows.get(task_id)
        if entry:
            await self._db.save_escrow(task_id, entry[0], entry[1])
        else:
            await self._db.delete_escrow(task_id)

    # ── Public API ────────────────────────────────────────────────────

    def get_or_create_account(
        self, agent_id: str, initial_balance: float = 0.0
    ) -> Account:
        if agent_id not in self._accounts:
            self._accounts[agent_id] = Account(agent_id, initial_balance)
        return self._accounts[agent_id]

    def get_account(self, agent_id: str) -> Account | None:
        return self._accounts.get(agent_id)

    # ── Task creation: freeze budget ─────────────────────────────────

    async def freeze_budget(
        self, initiator_id: str, task_id: str, amount: float
    ) -> None:
        """Freeze budget to escrow on task creation.

        Raises BudgetError if insufficient balance.
        """
        account = self.get_or_create_account(initiator_id)
        account.freeze(amount)
        self._task_escrows[task_id] = (initiator_id, amount)
        await self._persist_account(initiator_id)
        await self._persist_escrow(task_id)

    def get_escrowed_amount(self, task_id: str) -> float:
        """Get the amount held in escrow for a task."""
        entry = self._task_escrows.get(task_id)
        return entry[1] if entry else 0.0

    # ── Subtask budget allocation ────────────────────────────────────

    async def allocate_subtask_budget(
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
        # Child escrow retains the original payer (parent initiator) so
        # release() refunds the correct account (#16)
        self._task_escrows[parent_task_id] = (
            parent_initiator,
            parent_amount - amount,
        )
        self._task_escrows[subtask_id] = (parent_initiator, amount)
        await self._persist_escrow(parent_task_id)
        await self._persist_escrow(subtask_id)

    async def reclaim_to_parent(
        self, child_task_id: str, parent_task_id: str
    ) -> float:
        """Return remaining child escrow back to the parent escrow.

        Used when a parent task terminates and needs to reclaim budget
        that was allocated to child tasks via allocate_subtask_budget.
        Returns the amount reclaimed.
        """
        child_entry = self._task_escrows.pop(child_task_id, None)
        if not child_entry:
            return 0.0

        _, child_amount = child_entry
        if child_amount <= 0:
            await self._persist_escrow(child_task_id)  # delete from DB
            return 0.0

        parent_entry = self._task_escrows.get(parent_task_id)
        if parent_entry:
            parent_initiator, parent_amount = parent_entry
            self._task_escrows[parent_task_id] = (
                parent_initiator,
                parent_amount + child_amount,
            )
        else:
            # Parent escrow missing — fall back to release to initiator account
            initiator_id = child_entry[0]
            account = self.get_or_create_account(initiator_id)
            account.unfreeze(child_amount)
            await self._persist_account(initiator_id)

        await self._persist_escrow(child_task_id)   # delete child
        await self._persist_escrow(parent_task_id)   # update parent
        return child_amount

    # ── Budget confirmation (initiator approves over-budget bid) ─────

    async def confirm_budget_increase(
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
        await self._persist_account(initiator_id)
        await self._persist_escrow(task_id)

    # ── Release / refund ─────────────────────────────────────────────

    async def release(self, task_id: str) -> float:
        """Release frozen funds back to initiator (no-one-capable refund).

        Returns the refunded amount.
        """
        entry = self._task_escrows.pop(task_id, None)
        if not entry:
            return 0.0

        initiator_id, amount = entry
        if amount > 0:
            # Use get_or_create to handle missing account edge case
            account = self.get_or_create_account(initiator_id)
            account.unfreeze(amount)
            await self._persist_account(initiator_id)
        await self._persist_escrow(task_id)  # deletes from DB
        return amount

    async def deduct_for_settlement(
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

        # Use get_or_create to handle missing account edge case
        account = self.get_or_create_account(initiator_id)
        account.deduct_frozen(amount)
        await self._persist_account(initiator_id)

        self._task_escrows[task_id] = (initiator_id, escrowed - amount)
        await self._persist_escrow(task_id)
        return initiator_id
