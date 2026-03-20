"""Shared fixtures for cluster-level tests."""

import pytest
from eacn.network.db.database import Database
from eacn.network.cluster.config import ClusterConfig
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.service import ClusterService


@pytest.fixture
async def db():
    """In-memory database with cluster tables."""
    database = Database()
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def members():
    """Empty membership list."""
    return MembershipList()


@pytest.fixture
def local_node():
    """A local NodeCard for testing."""
    return NodeCard(
        node_id="node-local",
        endpoint="http://localhost:8000",
        domains=["coding", "translation"],
        version="0.1.0",
    )


@pytest.fixture
def peer_node():
    """A peer NodeCard for testing."""
    return NodeCard(
        node_id="node-peer",
        endpoint="http://peer:8000",
        domains=["translation", "design"],
        version="0.1.0",
    )


@pytest.fixture
def peer_node_b():
    """A second peer NodeCard for testing."""
    return NodeCard(
        node_id="node-peer-b",
        endpoint="http://peer-b:8000",
        domains=["coding", "research"],
        version="0.1.0",
    )


@pytest.fixture
async def standalone_cluster(db):
    """ClusterService in standalone mode (no seed nodes)."""
    config = ClusterConfig(
        node_id="node-standalone",
        endpoint="http://localhost:8000",
    )
    cs = ClusterService(db, config=config)
    await cs.start()
    return cs


@pytest.fixture
async def cluster_with_peers(db, peer_node, peer_node_b):
    """ClusterService with pre-populated peers (without real network)."""
    config = ClusterConfig(
        node_id="node-local",
        endpoint="http://localhost:8000",
    )
    cs = ClusterService(db, config=config)
    # Manually add peers (simulating post-join state)
    cs.members.add(peer_node)
    cs.members.add(peer_node_b)
    cs.router.set_endpoint(peer_node.node_id, peer_node.endpoint)
    cs.router.set_endpoint(peer_node_b.node_id, peer_node_b.endpoint)
    return cs
