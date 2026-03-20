"""Tests: aiosqlite database layer (persistence interface).

The Database class IS an external interface — it's the persistence contract.
"""

import pytest

from eacn.network.db import Database

@pytest.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    yield d
    await d.close()

class TestTaskStore:
    @pytest.mark.asyncio
    async def test_save_and_load(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1"})
        data = await db.load_task("t1")
        assert data["id"] == "t1"
        assert data["status"] == "unclaimed"

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, db):
        assert await db.load_task("ghost") is None

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1"})
        await db.save_task("t1", {"id": "t1", "status": "bidding", "initiator_id": "u1"})
        data = await db.load_task("t1")
        assert data["status"] == "bidding"

    @pytest.mark.asyncio
    async def test_list_tasks(self, db):
        for i in range(3):
            await db.save_task(f"t{i}", {"id": f"t{i}", "status": "unclaimed", "initiator_id": "u1"})
        tasks = await db.list_tasks()
        assert len(tasks) == 3

    @pytest.mark.asyncio
    async def test_list_filter_status(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1"})
        await db.save_task("t2", {"id": "t2", "status": "bidding", "initiator_id": "u1"})
        tasks = await db.list_tasks(status="bidding")
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_list_filter_initiator(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1"})
        await db.save_task("t2", {"id": "t2", "status": "unclaimed", "initiator_id": "u2"})
        tasks = await db.list_tasks(initiator_id="u1")
        assert len(tasks) == 1

    @pytest.mark.asyncio
    async def test_find_expired(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1", "deadline": "2020-01-01"})
        await db.save_task("t2", {"id": "t2", "status": "completed", "initiator_id": "u1", "deadline": "2020-01-01"})
        expired = await db.find_expired_tasks("2025-01-01")
        assert len(expired) == 1
        assert expired[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_delete_task(self, db):
        await db.save_task("t1", {"id": "t1", "status": "unclaimed", "initiator_id": "u1"})
        await db.delete_task("t1")
        assert await db.load_task("t1") is None

    @pytest.mark.asyncio
    async def test_pagination(self, db):
        for i in range(10):
            await db.save_task(f"t{i}", {"id": f"t{i}", "status": "unclaimed", "initiator_id": "u1"})
        page1 = await db.list_tasks(limit=3, offset=0)
        page2 = await db.list_tasks(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0]["id"] != page2[0]["id"]

class TestAccountStore:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, db):
        await db.upsert_account("a1", 100.0, 0.0)
        acc = await db.get_account("a1")
        assert acc["available"] == 100.0
        assert acc["frozen"] == 0.0

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db):
        assert await db.get_account("ghost") is None

    @pytest.mark.asyncio
    async def test_update_values(self, db):
        await db.upsert_account("a1", 100.0, 0.0)
        await db.upsert_account("a1", 80.0, 20.0)
        acc = await db.get_account("a1")
        assert acc["available"] == 80.0
        assert acc["frozen"] == 20.0

class TestEscrowStore:
    @pytest.mark.asyncio
    async def test_save_and_get(self, db):
        await db.save_escrow("t1", "u1", 100.0)
        result = await db.get_escrow("t1")
        assert result == ("u1", 100.0)

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db):
        assert await db.get_escrow("ghost") is None

    @pytest.mark.asyncio
    async def test_delete(self, db):
        await db.save_escrow("t1", "u1", 100.0)
        await db.delete_escrow("t1")
        assert await db.get_escrow("t1") is None

    @pytest.mark.asyncio
    async def test_upsert(self, db):
        await db.save_escrow("t1", "u1", 100.0)
        await db.save_escrow("t1", "u1", 200.0)
        result = await db.get_escrow("t1")
        assert result == ("u1", 200.0)

class TestReputationStore:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, db):
        await db.upsert_reputation("a1", 0.8, {"capped_gain": 3})
        score, caps = await db.get_reputation("a1")
        assert score == 0.8
        assert caps["capped_gain"] == 3

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, db):
        assert await db.get_reputation("ghost") is None

    @pytest.mark.asyncio
    async def test_server_reputation(self, db):
        await db.upsert_server_reputation("s1", 0.9, 50)
        result = await db.get_server_reputation("s1")
        assert result == (0.9, 50)

    @pytest.mark.asyncio
    async def test_server_reputation_nonexistent(self, db):
        assert await db.get_server_reputation("ghost") is None

class TestLogStore:
    @pytest.mark.asyncio
    async def test_insert_and_query(self, db):
        await db.insert_log("create_task", "2025-01-01", task_id="t1")
        logs = await db.query_logs(task_id="t1")
        assert len(logs) == 1
        assert logs[0]["fn_name"] == "create_task"

    @pytest.mark.asyncio
    async def test_query_by_agent(self, db):
        await db.insert_log("submit_bid", "2025-01-01", task_id="t1", agent_id="a1")
        await db.insert_log("submit_result", "2025-01-01", task_id="t1", agent_id="a2")
        logs = await db.query_logs(agent_id="a1")
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_query_by_fn_name(self, db):
        await db.insert_log("create_task", "2025-01-01")
        await db.insert_log("submit_bid", "2025-01-01")
        logs = await db.query_logs(fn_name="create_task")
        assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_query_with_limit(self, db):
        for i in range(10):
            await db.insert_log(f"fn_{i}", "2025-01-01")
        logs = await db.query_logs(limit=3)
        assert len(logs) == 3

    @pytest.mark.asyncio
    async def test_log_with_result_and_error(self, db):
        await db.insert_log(
            "fail_op", "2025-01-01",
            result={"ok": False}, error="something broke",
        )
        logs = await db.query_logs(fn_name="fail_op")
        assert logs[0]["error"] == "something broke"
        assert logs[0]["result"] == {"ok": False}

class TestDHTStore:
    @pytest.mark.asyncio
    async def test_announce_and_lookup(self, db):
        await db.dht_announce("coding", "a1")
        result = await db.dht_lookup("coding")
        assert result == ["a1"]

    @pytest.mark.asyncio
    async def test_revoke(self, db):
        await db.dht_announce("coding", "a1")
        await db.dht_revoke("coding", "a1")
        assert await db.dht_lookup("coding") == []

    @pytest.mark.asyncio
    async def test_idempotent_announce(self, db):
        await db.dht_announce("coding", "a1")
        await db.dht_announce("coding", "a1")
        result = await db.dht_lookup("coding")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_multiple_agents_per_domain(self, db):
        await db.dht_announce("coding", "a1")
        await db.dht_announce("coding", "a2")
        result = await db.dht_lookup("coding")
        assert set(result) == {"a1", "a2"}

    @pytest.mark.asyncio
    async def test_empty_lookup(self, db):
        assert await db.dht_lookup("nonexistent") == []

class TestPushStore:
    @pytest.mark.asyncio
    async def test_insert_and_query(self, db):
        await db.insert_push("task_broadcast", "t1", ["a1"], {"budget": 100})
        history = await db.get_push_history("t1")
        assert len(history) == 1
        assert history[0]["type"] == "task_broadcast"
        assert history[0]["recipients"] == ["a1"]

    @pytest.mark.asyncio
    async def test_query_all(self, db):
        await db.insert_push("task_broadcast", "t1", ["a1"], {})
        await db.insert_push("bid_result", "t2", ["a2"], {})
        history = await db.get_push_history()
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_query_limit(self, db):
        for i in range(10):
            await db.insert_push("event", f"t{i}", ["a1"], {})
        history = await db.get_push_history(limit=3)
        assert len(history) == 3
