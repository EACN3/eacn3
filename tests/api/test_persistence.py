"""Tests: production-style persistence — disk-based SQLite survives restart.

These tests use a real file-based database (tmp_path), NOT :memory:.
They verify that after a full Network shutdown + restart, all accounts,
escrows, and reputation scores are recovered from disk.
"""

import pytest

from eacn.network.app import Network
from eacn.network.config import NetworkConfig
from eacn.network.db.database import Database


async def _make_network(db_path: str) -> tuple[Network, Database]:
    """Create a Network backed by a file-based SQLite database."""
    db = Database(db_path)
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    await net.start()
    return net, db


class TestAccountPersistence:
    """Account balances survive a full restart."""

    @pytest.mark.asyncio
    async def test_balance_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: create account, deposit funds ──────────────
        net, db = await _make_network(db_path)
        net.escrow.get_or_create_account("user1", 5000.0)
        # persist the newly created account
        await net.escrow._persist_account("user1")
        await db.close()

        # ── Session 2: restart from same DB file ──────────────────
        net2, db2 = await _make_network(db_path)
        acct = net2.escrow.get_account("user1")
        assert acct is not None, "Account should survive restart"
        assert acct.available == 5000.0
        assert acct.frozen == 0.0
        await db2.close()

    @pytest.mark.asyncio
    async def test_frozen_balance_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: freeze budget via task creation ────────────
        net, db = await _make_network(db_path)
        net.escrow.get_or_create_account("user1", 5000.0)
        await net.escrow._persist_account("user1")
        await net.escrow.freeze_budget("user1", "task-1", 2000.0)
        await db.close()

        # ── Session 2: verify frozen state persisted ──────────────
        net2, db2 = await _make_network(db_path)
        acct = net2.escrow.get_account("user1")
        assert acct is not None
        assert acct.available == 3000.0
        assert acct.frozen == 2000.0
        await db2.close()


class TestEscrowPersistence:
    """Escrow records survive a full restart."""

    @pytest.mark.asyncio
    async def test_escrow_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: create escrow ──────────────────────────────
        net, db = await _make_network(db_path)
        net.escrow.get_or_create_account("user1", 10000.0)
        await net.escrow._persist_account("user1")
        await net.escrow.freeze_budget("user1", "task-1", 500.0)
        await net.escrow.freeze_budget("user1", "task-2", 300.0)
        await db.close()

        # ── Session 2: verify both escrows recovered ──────────────
        net2, db2 = await _make_network(db_path)
        assert net2.escrow.get_escrowed_amount("task-1") == 500.0
        assert net2.escrow.get_escrowed_amount("task-2") == 300.0
        acct = net2.escrow.get_account("user1")
        assert acct is not None
        assert acct.available == 9200.0
        assert acct.frozen == 800.0
        await db2.close()

    @pytest.mark.asyncio
    async def test_released_escrow_not_restored(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: create then release ────────────────────────
        net, db = await _make_network(db_path)
        net.escrow.get_or_create_account("user1", 10000.0)
        await net.escrow._persist_account("user1")
        await net.escrow.freeze_budget("user1", "task-1", 500.0)
        refund = await net.escrow.release("task-1")
        assert refund == 500.0
        await db.close()

        # ── Session 2: escrow gone, balance restored ──────────────
        net2, db2 = await _make_network(db_path)
        assert net2.escrow.get_escrowed_amount("task-1") == 0.0
        acct = net2.escrow.get_account("user1")
        assert acct is not None
        assert acct.available == 10000.0
        assert acct.frozen == 0.0
        await db2.close()


class TestReputationPersistence:
    """Reputation scores survive a full restart."""

    @pytest.mark.asyncio
    async def test_agent_reputation_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: set reputation ─────────────────────────────
        net, db = await _make_network(db_path)
        net.reputation._scores["agent-a"] = 0.85
        net.reputation._cap_counts["agent-a"] = {"capped_gain": 3}
        await net.reputation._persist_agent("agent-a")
        await db.close()

        # ── Session 2: verify recovered ───────────────────────────
        net2, db2 = await _make_network(db_path)
        assert net2.reputation.get_score("agent-a") == 0.85
        assert net2.reputation.get_cap_counts("agent-a") == {"capped_gain": 3}
        await db2.close()

    @pytest.mark.asyncio
    async def test_server_reputation_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: set server reputation ──────────────────────
        net, db = await _make_network(db_path)
        await net.reputation.set_server_reputation("server-1", 0.9)
        await db.close()

        # ── Session 2: verify recovered ───────────────────────────
        net2, db2 = await _make_network(db_path)
        assert net2.reputation.get_server_reputation("server-1") == 0.9
        await db2.close()

    @pytest.mark.asyncio
    async def test_propagate_selection_persisted(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: propagate selection ────────────────────────
        net, db = await _make_network(db_path)
        net.reputation._scores["selector"] = 0.8
        net.reputation._scores["executor"] = 0.6
        await net.reputation.propagate_selection("selector", "executor")
        selector_score = net.reputation.get_score("selector")
        executor_score = net.reputation.get_score("executor")
        await db.close()

        # ── Session 2: verify both scores persisted ───────────────
        net2, db2 = await _make_network(db_path)
        assert net2.reputation.get_score("selector") == selector_score
        assert net2.reputation.get_score("executor") == executor_score
        await db2.close()


class TestFullLifecyclePersistence:
    """End-to-end: task creation with budget freeze, reputation update,
    then restart — everything recovered."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_survives_restart(self, tmp_path):
        db_path = str(tmp_path / "eacn3.db")

        # ── Session 1: simulate production operations ─────────────
        net, db = await _make_network(db_path)

        # Fund user
        net.escrow.get_or_create_account("user1", 10000.0)
        await net.escrow._persist_account("user1")

        # Freeze budget for a task
        await net.escrow.freeze_budget("user1", "task-99", 1500.0)

        # Set agent reputation
        net.reputation._scores["agent-x"] = 0.75
        await net.reputation._persist_agent("agent-x")

        # Set server reputation
        await net.reputation.set_server_reputation("srv-1", 0.88)

        await db.close()

        # ── Session 2: full restart, verify everything ────────────
        net2, db2 = await _make_network(db_path)

        # Account
        acct = net2.escrow.get_account("user1")
        assert acct is not None
        assert acct.available == 8500.0
        assert acct.frozen == 1500.0

        # Escrow
        assert net2.escrow.get_escrowed_amount("task-99") == 1500.0

        # Agent reputation
        assert net2.reputation.get_score("agent-x") == 0.75

        # Server reputation
        assert net2.reputation.get_server_reputation("srv-1") == 0.88

        await db2.close()
