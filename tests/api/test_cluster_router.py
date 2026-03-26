"""Cluster router unit tests.

Tests the routing layer in isolation:
- Route set/get/remove
- Participant tracking
- is_local determination
- Endpoint management
"""

import pytest
from eacn3.network.db.database import Database
from eacn3.network.cluster.router import ClusterRouter


@pytest.fixture
async def router():
    db = Database()
    await db.connect()
    r = ClusterRouter(db, "local-node-1")
    yield r
    await db.close()


class TestRouteManagement:
    @pytest.mark.asyncio
    async def test_set_and_get_route(self, router):
        router.set_route("task-1", "node-a")
        assert router.get_route("task-1") == "node-a"

    @pytest.mark.asyncio
    async def test_get_nonexistent_route(self, router):
        assert router.get_route("nonexistent") is None

    @pytest.mark.asyncio
    async def test_remove_route(self, router):
        router.set_route("task-2", "node-b")
        router.remove_route("task-2")
        assert router.get_route("task-2") is None

    @pytest.mark.asyncio
    async def test_overwrite_route(self, router):
        router.set_route("task-3", "node-a")
        router.set_route("task-3", "node-b")
        assert router.get_route("task-3") == "node-b"


class TestIsLocal:
    @pytest.mark.asyncio
    async def test_no_route_is_local(self, router):
        """Tasks without a route are treated as local."""
        assert router.is_local("unknown-task") is True

    @pytest.mark.asyncio
    async def test_local_route(self, router):
        router.set_route("task-l", "local-node-1")
        assert router.is_local("task-l") is True

    @pytest.mark.asyncio
    async def test_remote_route(self, router):
        router.set_route("task-r", "remote-node")
        assert router.is_local("task-r") is False


class TestParticipants:
    @pytest.mark.asyncio
    async def test_add_and_get_participants(self, router):
        router.add_participant("task-p1", "node-a")
        router.add_participant("task-p1", "node-b")
        participants = router.get_participants("task-p1")
        assert participants == {"node-a", "node-b"}

    @pytest.mark.asyncio
    async def test_duplicate_participant(self, router):
        router.add_participant("task-p2", "node-a")
        router.add_participant("task-p2", "node-a")
        assert router.get_participants("task-p2") == {"node-a"}

    @pytest.mark.asyncio
    async def test_remove_participants(self, router):
        router.add_participant("task-p3", "node-a")
        router.add_participant("task-p3", "node-b")
        router.remove_participants("task-p3")
        assert router.get_participants("task-p3") == set()

    @pytest.mark.asyncio
    async def test_remove_task_clears_both(self, router):
        router.set_route("task-p4", "node-a")
        router.add_participant("task-p4", "node-b")
        router.remove_task("task-p4")
        assert router.get_route("task-p4") is None
        assert router.get_participants("task-p4") == set()


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_set_and_get_endpoint(self, router):
        router.set_endpoint("node-x", "http://10.0.0.1:8000")
        assert router.get_endpoint("node-x") == "http://10.0.0.1:8000"

    @pytest.mark.asyncio
    async def test_get_nonexistent_endpoint(self, router):
        assert router.get_endpoint("ghost") is None


class TestManyRoutes:
    @pytest.mark.asyncio
    async def test_100_routes(self, router):
        """Set 100 routes, verify all retrievable."""
        for i in range(100):
            router.set_route(f"t-{i}", f"n-{i % 5}")

        for i in range(100):
            assert router.get_route(f"t-{i}") == f"n-{i % 5}"

    @pytest.mark.asyncio
    async def test_remove_half_routes(self, router):
        for i in range(50):
            router.set_route(f"rh-{i}", "node-a")
        for i in range(25):
            router.remove_route(f"rh-{i}")
        remaining = sum(1 for i in range(50) if router.get_route(f"rh-{i}"))
        assert remaining == 25
