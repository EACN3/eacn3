"""Tests for ClusterRouter."""

import pytest
from eacn.network.cluster.router import ClusterRouter


class TestClusterRouter:
    async def test_is_local_when_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        # No route = assume local
        assert router.is_local("task-1")

    async def test_is_local_when_route_is_self(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("task-1", "local-node")
        assert router.is_local("task-1")

    async def test_not_local_when_route_is_remote(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("task-1", "remote-node")
        assert not router.is_local("task-1")

    async def test_get_route(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "origin-1")
        assert router.get_route("t1") == "origin-1"

    async def test_get_route_nonexistent(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.get_route("nonexistent") is None

    async def test_remove_route(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "origin-1")
        router.remove_route("t1")
        assert router.get_route("t1") is None
        assert router.is_local("t1")

    async def test_set_and_get_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("remote", "http://remote:8000")
        assert router.get_endpoint("remote") == "http://remote:8000"

    async def test_get_endpoint_nonexistent(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.get_endpoint("nonexistent") is None

    async def test_add_and_get_participants(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.add_participant("t1", "node-b")
        router.add_participant("t1", "node-a")  # Duplicate
        participants = router.get_participants("t1")
        assert participants == {"node-a", "node-b"}

    async def test_get_participants_empty(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.get_participants("nonexistent") == set()

    async def test_remove_participants(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.remove_participants("t1")
        assert router.get_participants("t1") == set()

    async def test_forward_bid_raises_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route"):
            await router.forward_bid("unknown", "agent", None, 0.9, 80.0)

    async def test_forward_bid_raises_no_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        # No endpoint set for remote-node
        with pytest.raises(ValueError, match="No endpoint"):
            await router.forward_bid("t1", "agent", None, 0.9, 80.0)

    async def test_forward_result_raises_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route"):
            await router.forward_result("unknown", "agent", "content")

    async def test_forward_reject_raises_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route"):
            await router.forward_reject("unknown", "agent")

    async def test_forward_subtask_raises_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route"):
            await router.forward_subtask("unknown", {})

    async def test_forward_subtask_raises_no_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote")
        with pytest.raises(ValueError, match="No endpoint"):
            await router.forward_subtask("t1", {})
