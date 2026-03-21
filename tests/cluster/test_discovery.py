"""Tests for ClusterDiscovery (three-layer orchestration)."""

import pytest
from eacn3.network.cluster.discovery import ClusterDiscovery
from eacn3.network.cluster.bootstrap import ClusterBootstrap
from eacn3.network.cluster.dht import ClusterDHT
from eacn3.network.cluster.gossip import ClusterGossip
from eacn3.network.config import ClusterConfig
from eacn3.network.cluster.node import NodeCard, MembershipList


@pytest.fixture
async def discovery_setup(db):
    """Set up a full discovery stack for testing."""
    members = MembershipList()
    local = NodeCard(
        node_id="local", endpoint="http://local:8000", domains=["coding"],
    )
    members.add(local)

    config = ClusterConfig(node_id="local", endpoint="http://local:8000")
    bootstrap = ClusterBootstrap(local, members, config)
    dht = ClusterDHT(db)
    gossip = ClusterGossip(db, members, local_node_id="local")
    disc = ClusterDiscovery("local", gossip, dht, bootstrap)

    return disc, gossip, dht, bootstrap, members


class TestDiscoverViaGossip:
    async def test_returns_gossip_results(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        peer = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["coding"])
        members.add(peer)
        await gossip.add_known("local", "peer")

        result = await disc.discover("coding")
        assert result == ["peer"]

    async def test_gossip_takes_priority_over_dht(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Gossip has gossip-peer for domain "x"
        peer_g = NodeCard(node_id="gossip-peer", endpoint="http://gp:8000", domains=["x"])
        members.add(peer_g)
        await gossip.add_known("local", "gossip-peer")

        # DHT also has dht-peer for domain "x"
        await dht.announce("x", "dht-peer")

        result = await disc.discover("x")
        # Must return gossip result only (first layer wins)
        assert result == ["gossip-peer"]
        assert "dht-peer" not in result


class TestDiscoverViaDHT:
    async def test_dht_fallback_when_gossip_empty(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        await dht.announce("design", "node-x")

        result = await disc.discover("design")
        assert result == ["node-x"]

    async def test_dht_takes_priority_over_bootstrap(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        await dht.announce("y", "dht-node")
        peer_bs = NodeCard(node_id="bs-node", endpoint="http://bs:8000", domains=["y"])
        members.add(peer_bs)

        result = await disc.discover("y")
        assert result == ["dht-node"]
        assert "bs-node" not in result


class TestDiscoverViaBootstrap:
    async def test_bootstrap_fallback(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        peer = NodeCard(node_id="peer-b", endpoint="http://pb:8000", domains=["research"])
        members.add(peer)

        result = await disc.discover("research")
        assert result == ["peer-b"]


class TestDiscoverExclusion:
    async def test_excludes_local_from_gossip(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Add local to its own gossip known list
        await gossip.add_known("local", "local")
        other = NodeCard(node_id="other", endpoint="http://other:8000", domains=["coding"])
        members.add(other)
        await gossip.add_known("local", "other")

        result = await disc.discover("coding")
        assert "local" not in result
        assert "other" in result

    async def test_excludes_local_from_dht(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        await dht.announce("coding", "local")
        await dht.announce("coding", "remote")

        result = await disc.discover("coding")
        assert "local" not in result
        assert "remote" in result

    async def test_excludes_local_from_bootstrap(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup
        # local already has "coding" in domains
        result = await disc.discover("coding")
        assert "local" not in result

    async def test_no_results_returns_empty_list(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup
        result = await disc.discover("nonexistent-domain")
        assert result == []
