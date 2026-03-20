"""Cluster Bootstrap: cold start via seed nodes.

Solves the "first friend" problem. Seed node addresses come from config.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from eacn.network.cluster.node import NodeCard, MembershipList

if TYPE_CHECKING:
    from eacn.network.cluster.config import ClusterConfig

_log = logging.getLogger(__name__)


class ClusterBootstrap:
    """Node cold-start: contact seed nodes to join the network."""

    def __init__(
        self,
        local_node: NodeCard,
        members: MembershipList,
        config: "ClusterConfig",
    ) -> None:
        self._local = local_node
        self._members = members
        self._seed_endpoints: list[str] = list(config.seed_nodes)
        self._is_seed = local_node.endpoint in self._seed_endpoints

    @property
    def is_seed(self) -> bool:
        return self._is_seed

    async def join_network(self) -> list[NodeCard]:
        """Contact seed nodes, announce self, return initial member list.

        Returns empty list if no seeds configured (standalone mode).
        """
        if not self._seed_endpoints:
            return []

        for endpoint in self._seed_endpoints:
            if endpoint == self._local.endpoint:
                continue
            try:
                nodes = await self._contact_seed(endpoint)
                return nodes
            except Exception:
                _log.warning("Seed node %s unreachable, trying next", endpoint)
                continue

        _log.warning("All seed nodes unreachable, entering standalone mode")
        return []

    async def _contact_seed(self, endpoint: str) -> list[NodeCard]:
        """POST /peer/join to a seed node."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{endpoint}/peer/join",
                json={"node_card": self._local.to_dict()},
            )
            resp.raise_for_status()
            data = resp.json()
            nodes = [NodeCard.from_dict(n) for n in data.get("nodes", [])]
            return nodes

    async def leave_network(self, peers: list[NodeCard]) -> None:
        """Notify all known peers that this node is leaving."""
        for peer in peers:
            if peer.node_id == self._local.node_id:
                continue
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{peer.endpoint}/peer/leave",
                        json={"node_id": self._local.node_id},
                    )
            except Exception:
                _log.debug("Failed to notify %s of leave", peer.node_id)

    def lookup(self, domain: str) -> list[str]:
        """Fallback lookup: scan full membership for domain."""
        return self._members.find_by_domain(domain, exclude=self._local.node_id)

    # ── Seed node: handle incoming join ──────────────────────────────

    def handle_join(self, card: NodeCard) -> list[NodeCard]:
        """Process incoming join request (seed node role).

        Returns current membership list for the joining node.
        """
        existing = self._members.get(card.node_id)
        if existing and existing.endpoint != card.endpoint:
            raise ValueError(
                f"node_id {card.node_id} already exists with different endpoint"
            )

        self._members.add(card)
        return self._members.all_nodes()

    def handle_leave(self, node_id: str) -> None:
        """Process incoming leave notification."""
        self._members.remove(node_id)

    def handle_heartbeat(
        self, node_id: str, domains: list[str], timestamp: str,
    ) -> None:
        """Process incoming heartbeat."""
        if not self._members.contains(node_id):
            return
        self._members.update_last_seen(node_id, timestamp)
        self._members.update_domains(node_id, domains)
        self._members.update_status(node_id, "online")
