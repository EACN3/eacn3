"""Tests for ClusterRouter."""

import pytest
from eacn3.network.cluster.router import ClusterRouter


class TestRouting:
    async def test_is_local_no_route_means_local(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.is_local("task-1") is True
        assert router.get_route("task-1") is None

    async def test_is_local_when_route_is_self(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("task-1", "local-node")
        assert router.is_local("task-1") is True
        assert router.get_route("task-1") == "local-node"

    async def test_not_local_when_route_is_remote(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("task-1", "remote-node")
        assert router.is_local("task-1") is False
        assert router.get_route("task-1") == "remote-node"

    async def test_remove_route_makes_local_again(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote")
        assert router.is_local("t1") is False
        router.remove_route("t1")
        assert router.is_local("t1") is True
        assert router.get_route("t1") is None


class TestEndpoints:
    async def test_set_and_get_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("remote", "http://remote:8000")
        assert router.get_endpoint("remote") == "http://remote:8000"

    async def test_get_endpoint_nonexistent_returns_none(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.get_endpoint("nonexistent") is None

    async def test_overwrite_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("remote", "http://old:8000")
        router.set_endpoint("remote", "http://new:8000")
        assert router.get_endpoint("remote") == "http://new:8000"


class TestParticipants:
    async def test_add_and_get(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.add_participant("t1", "node-b")
        participants = router.get_participants("t1")
        assert participants == {"node-a", "node-b"}

    async def test_duplicate_add_is_idempotent(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.add_participant("t1", "node-a")
        assert router.get_participants("t1") == {"node-a"}

    async def test_get_participants_empty(self, db):
        router = ClusterRouter(db, "local-node")
        assert router.get_participants("nonexistent") == set()

    async def test_remove_participants(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.add_participant("t1", "node-b")
        router.remove_participants("t1")
        assert router.get_participants("t1") == set()

    async def test_participants_per_task_isolated(self, db):
        router = ClusterRouter(db, "local-node")
        router.add_participant("t1", "node-a")
        router.add_participant("t2", "node-b")
        assert router.get_participants("t1") == {"node-a"}
        assert router.get_participants("t2") == {"node-b"}


class TestForwardErrors:
    async def test_forward_bid_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route for task unknown"):
            await router.forward_bid("unknown", "agent", None, 0.9, 80.0)

    async def test_forward_bid_no_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        with pytest.raises(ValueError, match="No endpoint for node remote-node"):
            await router.forward_bid("t1", "agent", None, 0.9, 80.0)

    async def test_forward_result_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route for task unknown"):
            await router.forward_result("unknown", "agent", "content")

    async def test_forward_result_no_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        with pytest.raises(ValueError, match="No endpoint for node remote-node"):
            await router.forward_result("t1", "agent", "content")

    async def test_forward_reject_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route for task unknown"):
            await router.forward_reject("unknown", "agent")

    async def test_forward_subtask_no_route(self, db):
        router = ClusterRouter(db, "local-node")
        with pytest.raises(ValueError, match="No route for task unknown"):
            await router.forward_subtask("unknown", {})

    async def test_forward_subtask_no_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote")
        with pytest.raises(ValueError, match="No endpoint for node remote"):
            await router.forward_subtask("t1", {})
