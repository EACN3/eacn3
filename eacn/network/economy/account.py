"""Agent accounts: available and frozen balance."""

from __future__ import annotations

from eacn.core.exceptions import BudgetError


class Account:
    """Each agent's financial account."""

    def __init__(self, agent_id: str, initial_balance: float = 0.0) -> None:
        self.agent_id = agent_id
        self.available: float = initial_balance
        self.frozen: float = 0.0

    def freeze(self, amount: float) -> None:
        if amount > self.available:
            raise BudgetError(f"Insufficient balance: {self.available} < {amount}")
        self.available -= amount
        self.frozen += amount

    def unfreeze(self, amount: float) -> None:
        if amount > self.frozen:
            amount = self.frozen
        self.frozen -= amount
        self.available += amount

    def deduct_frozen(self, amount: float) -> None:
        if amount > self.frozen:
            raise BudgetError(f"Insufficient frozen funds: {self.frozen} < {amount}")
        self.frozen -= amount

    def credit(self, amount: float) -> None:
        self.available += amount

    @property
    def total(self) -> float:
        return self.available + self.frozen
