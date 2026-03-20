"""Cluster DHT: domain -> {node_id} cross-node routing.

Uses consistent hashing (SHA256) to map domains to responsible nodes.
For simplicity in v0.1, stores mappings in the local database
(single-node DHT). Full distributed hashing is a future extension.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class ClusterDHT:
    """Domain -> {node_id} mapping for cross-node discovery."""

    def __init__(self, db: "Database") -> None:
        self._db = db

    async def announce(self, domain: str, node_id: str) -> None:
        """Register domain -> node_id mapping."""
        await self._db.cluster_dht_store(domain, node_id)

    async def revoke(self, domain: str, node_id: str) -> None:
        """Remove domain -> node_id mapping."""
        await self._db.cluster_dht_revoke(domain, node_id)

    async def revoke_all(self, node_id: str) -> None:
        """Remove all domain mappings for a node."""
        await self._db.cluster_dht_revoke_all(node_id)

    async def lookup(self, domain: str) -> list[str]:
        """Find all node_ids registered for a domain."""
        return await self._db.cluster_dht_lookup(domain)

    # ── Peer-facing store/revoke (used by peer_routes) ──────────────

    async def handle_store(self, domain: str, node_id: str) -> None:
        """Handle incoming DHT store from a peer."""
        await self.announce(domain, node_id)

    async def handle_revoke(self, domain: str, node_id: str) -> None:
        """Handle incoming DHT revoke from a peer."""
        await self.revoke(domain, node_id)

    async def handle_lookup(self, domain: str) -> list[str]:
        """Handle incoming DHT lookup from a peer."""
        return await self.lookup(domain)
