"""Cluster Gossip: node-level known list exchange.

After task collaboration, participating nodes exchange their known node lists.
The more nodes collaborate, the richer each node's local knowledge becomes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from eacn.network.cluster.node import NodeCard, MembershipList

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class ClusterGossip:
    """Node-level known list exchange via collaboration events."""

    def __init__(self, db: "Database", members: MembershipList) -> None:
        self._db = db
        self._members = members

    async def exchange(self, node_a: str, node_b: str) -> None:
        """Exchange known node lists between two collaborating nodes.

        Both nodes learn about each other's known peers.
        """
        a_knows = await self._db.cluster_gossip_get_known(node_a)
        b_knows = await self._db.cluster_gossip_get_known(node_b)
        shared = a_knows | b_knows | {node_a, node_b}
        await self._db.cluster_gossip_add_many(node_a, shared - {node_a})
        await self._db.cluster_gossip_add_many(node_b, shared - {node_b})

    async def get_known(self, node_id: str) -> set[str]:
        return await self._db.cluster_gossip_get_known(node_id)

    async def lookup(self, node_id: str, domain: str) -> list[str]:
        """Find nodes in known list that handle the given domain.

        Pure local lookup — zero network overhead.
        """
        known = await self._db.cluster_gossip_get_known(node_id)
        results = []
        for kid in known:
            card = self._members.get(kid)
            if card and domain in card.domains and card.status == "online":
                results.append(kid)
        return results

    async def handle_exchange(
        self,
        from_node: NodeCard,
        known_cards: list[NodeCard],
    ) -> list[NodeCard]:
        """Handle incoming gossip exchange from a peer.

        Merge peer's known list into local, return local known list.
        """
        # Add peer to membership if not already known
        if not self._members.contains(from_node.node_id):
            self._members.add(from_node)

        # Add all cards from peer
        for card in known_cards:
            if not self._members.contains(card.node_id):
                self._members.add(card)

        # Merge gossip knowledge
        peer_known_ids = {c.node_id for c in known_cards} | {from_node.node_id}
        local_node_id = from_node.node_id  # We'll return our known list to peer
        # Store that we now know about these nodes
        for nid in peer_known_ids:
            if nid != from_node.node_id:
                await self._db.cluster_gossip_add(from_node.node_id, nid)

        # Return our full membership for the peer to merge
        return self._members.all_nodes(exclude=from_node.node_id)

    async def add_known(self, node_id: str, known_id: str) -> None:
        """Directly add a known node."""
        await self._db.cluster_gossip_add(node_id, known_id)

    async def remove_node(self, node_id: str) -> None:
        """Remove a node from all gossip records."""
        await self._db.cluster_gossip_remove(node_id)
