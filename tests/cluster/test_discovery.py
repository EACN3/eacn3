"""Tests for ClusterDiscovery (three-layer orchestration)."""

import pytest
from eacn.network.cluster.discovery import ClusterDiscovery
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.config import ClusterConfig
from eacn.network.cluster.node import NodeCard, MembershipList


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
    gossip = ClusterGossip(db, members)
    disc = ClusterDiscovery("local", gossip, dht, bootstrap)

    return disc, gossip, dht, bootstrap, members


class TestClusterDiscovery:
    async def test_discover_via_gossip(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Add a peer that knows about coding
        peer = NodeCard(node_id="peer", endpoint="http://peer:8000", domains=["coding"])
        members.add(peer)
        await gossip.add_known("local", "peer")

        result = await disc.discover("coding")
        assert result == ["peer"]

    async def test_discover_via_dht_fallback(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Nothing in gossip, but DHT has it
        await dht.announce("design", "node-x")

        result = await disc.discover("design")
        assert result == ["node-x"]

    async def test_discover_via_bootstrap_fallback(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Nothing in gossip or DHT, but a node in membership has the domain
        peer = NodeCard(node_id="peer-b", endpoint="http://pb:8000", domains=["research"])
        members.add(peer)

        result = await disc.discover("research")
        assert result == ["peer-b"]

    async def test_discover_excludes_local(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Local node has "coding" but should not be returned
        await dht.announce("coding", "local")
        result = await disc.discover("coding")
        assert "local" not in result

    async def test_discover_no_results(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        result = await disc.discover("nonexistent-domain")
        assert result == []

    async def test_gossip_takes_priority_over_dht(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        peer_g = NodeCard(node_id="gossip-peer", endpoint="http://gp:8000", domains=["x"])
        members.add(peer_g)
        await gossip.add_known("local", "gossip-peer")
        await dht.announce("x", "dht-peer")

        result = await disc.discover("x")
        # Gossip hit returns first, DHT is not consulted
        assert result == ["gossip-peer"]

    async def test_dht_takes_priority_over_bootstrap(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        await dht.announce("y", "dht-node")
        peer_bs = NodeCard(node_id="bs-node", endpoint="http://bs:8000", domains=["y"])
        members.add(peer_bs)

        result = await disc.discover("y")
        # DHT hit returns first
        assert result == ["dht-node"]

    async def test_discover_excludes_local_from_all_layers(self, discovery_setup):
        disc, gossip, dht, bootstrap, members = discovery_setup

        # Add local to gossip known and DHT
        await gossip.add_known("local", "local")
        await dht.announce("coding", "local")

        peer = NodeCard(node_id="other", endpoint="http://other:8000", domains=["coding"])
        members.add(peer)
        await gossip.add_known("local", "other")

        result = await disc.discover("coding")
        assert "local" not in result
        assert "other" in result
