"""Cluster Discovery: three-layer orchestration (Gossip -> DHT -> Bootstrap).

Finds which *nodes* handle a domain. Does NOT find agents directly —
that's the job of the existing discovery/ module within each node.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.cluster.bootstrap import ClusterBootstrap
    from eacn.network.cluster.dht import ClusterDHT
    from eacn.network.cluster.gossip import ClusterGossip

_log = logging.getLogger(__name__)


class ClusterDiscovery:
    """Orchestrates node-level discovery across Gossip, DHT, and Bootstrap."""

    def __init__(
        self,
        local_node_id: str,
        gossip: "ClusterGossip",
        dht: "ClusterDHT",
        bootstrap: "ClusterBootstrap",
    ) -> None:
        self._local_node_id = local_node_id
        self._gossip = gossip
        self._dht = dht
        self._bootstrap = bootstrap

    async def discover(self, domain: str) -> list[str]:
        """Find node_ids that handle the given domain.

        Three-layer fallback:
        1. Gossip (local known list, zero network overhead)
        2. DHT (structured lookup)
        3. Bootstrap (full scan, slowest but most complete)

        Always excludes local node.
        """
        # 1. Gossip: local cache
        results = await self._gossip.lookup(self._local_node_id, domain)
        results = [nid for nid in results if nid != self._local_node_id]
        if results:
            _log.debug("Gossip hit for domain=%s: %s", domain, results)
            return results

        # 2. DHT: structured lookup
        results = await self._dht.lookup(domain)
        results = [nid for nid in results if nid != self._local_node_id]
        if results:
            _log.debug("DHT hit for domain=%s: %s", domain, results)
            return results

        # 3. Bootstrap: fallback full scan
        results = self._bootstrap.lookup(domain)
        results = [nid for nid in results if nid != self._local_node_id]
        _log.debug("Bootstrap lookup for domain=%s: %s", domain, results)
        return results
