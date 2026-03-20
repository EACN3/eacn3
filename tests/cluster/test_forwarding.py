"""Tests for cross-node forwarding: mock httpx to verify full routing chains.

Covers: forward_bid, forward_result, forward_reject, forward_subtask,
notify_status, forward_push, and the API-layer routing logic.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import httpx

from eacn.network.cluster.router import ClusterRouter
from eacn.network.cluster.service import ClusterService
from eacn.network.config import ClusterConfig


class TestForwardBidHTTP:
    """forward_bid makes correct HTTP call and returns response."""

    async def test_forward_bid_sends_correct_payload(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "executing", "bid": {"agent_id": "a1", "status": "executing"}}
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await router.forward_bid("t1", "a1", "srv-1", 0.9, 80.0)

        assert result == {"status": "executing", "bid": {"agent_id": "a1", "status": "executing"}}
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://remote:8000/peer/task/bid"
        payload = call_args[1]["json"]
        assert payload["task_id"] == "t1"
        assert payload["agent_id"] == "a1"
        assert payload["server_id"] == "srv-1"
        assert payload["confidence"] == 0.9
        assert payload["price"] == 80.0
        assert payload["from_node"] == "local-node"

    async def test_forward_bid_none_server_id_sends_empty_string(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "executing"}
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.forward_bid("t1", "a1", None, 0.9, 80.0)

        payload = mock_client.post.call_args[1]["json"]
        assert payload["server_id"] == ""

    async def test_forward_bid_http_error_propagates(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=MagicMock(status_code=500),
        )

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await router.forward_bid("t1", "a1", None, 0.9, 80.0)

    async def test_forward_bid_timeout_propagates(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("Connection timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.TimeoutException):
                await router.forward_bid("t1", "a1", None, 0.9, 80.0)

    async def test_forward_bid_connect_error_propagates(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.ConnectError):
                await router.forward_bid("t1", "a1", None, 0.9, 80.0)


class TestForwardResultHTTP:
    async def test_forward_result_sends_correct_payload(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await router.forward_result("t1", "a1", "my result content")

        assert result == {"ok": True}
        payload = mock_client.post.call_args[1]["json"]
        assert payload["task_id"] == "t1"
        assert payload["agent_id"] == "a1"
        assert payload["content"] == "my result content"
        assert payload["from_node"] == "local-node"
        assert mock_client.post.call_args[0][0] == "http://remote:8000/peer/task/result"

    async def test_forward_result_timeout(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote")
        router.set_endpoint("remote", "http://remote:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.TimeoutException):
                await router.forward_result("t1", "a1", "content")


class TestForwardRejectHTTP:
    async def test_forward_reject_sends_correct_payload(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await router.forward_reject("t1", "a1")

        assert result == {"ok": True}
        payload = mock_client.post.call_args[1]["json"]
        assert payload["task_id"] == "t1"
        assert payload["agent_id"] == "a1"
        assert payload["from_node"] == "local-node"
        assert mock_client.post.call_args[0][0] == "http://remote:8000/peer/task/reject"


class TestForwardSubtaskHTTP:
    async def test_forward_subtask_sends_correct_payload(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_route("parent-t1", "remote-node")
        router.set_endpoint("remote-node", "http://remote:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"subtask_id": "sub-1", "status": "unclaimed"}
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            subtask_data = {"initiator_id": "a1", "content": {"desc": "sub"}, "domains": ["coding"], "budget": 50.0}
            result = await router.forward_subtask("parent-t1", subtask_data)

        assert result == {"subtask_id": "sub-1", "status": "unclaimed"}
        payload = mock_client.post.call_args[1]["json"]
        assert payload["parent_task_id"] == "parent-t1"
        assert payload["subtask_data"] == subtask_data
        assert payload["from_node"] == "local-node"
        assert mock_client.post.call_args[0][0] == "http://remote:8000/peer/task/subtask"


class TestNotifyStatus:
    async def test_notifies_all_participants_except_self(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("node-a", "http://a:8000")
        router.set_endpoint("node-b", "http://b:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.notify_status(
                "t1", "completed", {"local-node", "node-a", "node-b"}, {"key": "val"},
            )

        # Should have been called for node-a and node-b, NOT local-node
        assert mock_client.post.call_count == 2
        urls = {call.args[0] for call in mock_client.post.call_args_list}
        assert "http://a:8000/peer/task/status" in urls
        assert "http://b:8000/peer/task/status" in urls

    async def test_notify_skips_nodes_without_endpoint(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("node-a", "http://a:8000")
        # node-b has no endpoint

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.notify_status("t1", "completed", {"node-a", "node-b"})

        assert mock_client.post.call_count == 1
        assert mock_client.post.call_args[0][0] == "http://a:8000/peer/task/status"

    async def test_notify_swallows_individual_failures(self, db):
        """If one node fails, others should still be notified."""
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("node-a", "http://a:8000")
        router.set_endpoint("node-b", "http://b:8000")

        call_count = 0

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()

            async def side_effect(url, **kwargs):
                nonlocal call_count
                call_count += 1
                if "a:8000" in url:
                    raise httpx.ConnectError("refused")
                return MagicMock()

            mock_client.post.side_effect = side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Should not raise even though node-a fails
            await router.notify_status("t1", "completed", {"node-a", "node-b"})

        assert call_count == 2  # Both attempted

    async def test_notify_empty_participants(self, db):
        router = ClusterRouter(db, "local-node")
        # Should complete without any HTTP calls
        await router.notify_status("t1", "completed", set())

    async def test_notify_only_self_makes_no_calls(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("local-node", "http://local:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.notify_status("t1", "completed", {"local-node"})

        mock_client.post.assert_not_called()


class TestForwardPush:
    async def test_forward_push_to_target_nodes(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("node-a", "http://a:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.forward_push(
                "TASK_BROADCAST", "t1", ["agent-1", "agent-2"], {"data": "x"}, {"node-a"},
            )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["type"] == "TASK_BROADCAST"
        assert payload["task_id"] == "t1"
        assert payload["recipients"] == ["agent-1", "agent-2"]
        assert payload["payload"] == {"data": "x"}

    async def test_forward_push_skips_self(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("local-node", "http://local:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await router.forward_push("EVT", "t1", [], {}, {"local-node"})

        mock_client.post.assert_not_called()

    async def test_forward_push_swallows_failures(self, db):
        router = ClusterRouter(db, "local-node")
        router.set_endpoint("node-a", "http://a:8000")

        with patch("eacn.network.cluster.router.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.ConnectError("refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Should not raise
            await router.forward_push("EVT", "t1", [], {}, {"node-a"})


class TestBroadcastTaskHTTP:
    """ClusterService.broadcast_task makes correct HTTP calls to discovered peers."""

    async def test_broadcast_sends_to_discovered_nodes(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        # Setup: a peer that handles "coding"
        from eacn.network.cluster.node import NodeCard
        peer = NodeCard(node_id="peer-1", endpoint="http://peer1:8000", domains=["coding"])
        cs.members.add(peer)
        cs.router.set_endpoint("peer-1", "http://peer1:8000")
        await cs.dht.announce("coding", "peer-1")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1", "domains": ["coding"], "initiator_id": "user1",
            })

        assert notified == ["peer-1"]
        payload = mock_client.post.call_args[1]["json"]
        assert payload["task_id"] == "t1"
        assert payload["origin"] == "local"

    async def test_broadcast_skips_self(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        # Announce local node for "coding" domain
        await cs.dht.announce("coding", "local")

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1", "domains": ["coding"],
            })

        assert notified == []
        mock_client.post.assert_not_called()

    async def test_broadcast_partial_failure(self, db):
        """Some peers fail, others succeed — returned list only includes successes."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        from eacn.network.cluster.node import NodeCard
        for i in range(3):
            nid = f"peer-{i}"
            peer = NodeCard(node_id=nid, endpoint=f"http://p{i}:8000", domains=["coding"])
            cs.members.add(peer)
            cs.router.set_endpoint(nid, f"http://p{i}:8000")
            await cs.dht.announce("coding", nid)

        call_idx = 0

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()

            async def side_effect(url, **kwargs):
                nonlocal call_idx
                call_idx += 1
                if "p1" in url:
                    raise httpx.ConnectError("refused")
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                return resp

            mock_client.post.side_effect = side_effect
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1", "domains": ["coding"],
            })

        # peer-1 failed, so only 2 should be notified
        assert len(notified) == 2
        assert "peer-1" not in notified

    async def test_broadcast_no_matching_domain(self, db):
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        notified = await cs.broadcast_task({
            "task_id": "t1", "domains": ["nonexistent_domain"],
        })
        assert notified == []

    async def test_broadcast_gets_endpoint_from_members_fallback(self, db):
        """If router has no endpoint but members do, it should use that."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        from eacn.network.cluster.node import NodeCard
        peer = NodeCard(node_id="peer-1", endpoint="http://fallback:8000", domains=["coding"])
        cs.members.add(peer)
        # Deliberately NOT setting router endpoint
        await cs.dht.announce("coding", "peer-1")

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("eacn.network.cluster.service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            notified = await cs.broadcast_task({
                "task_id": "t1", "domains": ["coding"],
            })

        assert notified == ["peer-1"]
        url = mock_client.post.call_args[0][0]
        assert "fallback:8000" in url
        # Endpoint should now be cached in router
        assert cs.router.get_endpoint("peer-1") == "http://fallback:8000"

    async def test_broadcast_skips_node_without_any_endpoint(self, db):
        """If neither router nor members have an endpoint, skip the node."""
        config = ClusterConfig(node_id="local", endpoint="http://local:8000")
        cs = ClusterService(db, config=config)
        cs._standalone = False

        # Announce a node in DHT but don't add to members or router
        await cs.dht.announce("coding", "ghost-node")

        notified = await cs.broadcast_task({
            "task_id": "t1", "domains": ["coding"],
        })
        assert notified == []
