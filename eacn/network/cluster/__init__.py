"""Cluster layer: multi-node federation for EACN network.

Provides node-to-node discovery, routing, and task forwarding.
When no seed nodes are configured, operates in standalone mode
with all operations being no-ops.
"""

from eacn.network.cluster.service import ClusterService
from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.config import ClusterConfig

__all__ = ["ClusterService", "NodeCard", "MembershipList", "ClusterConfig"]
