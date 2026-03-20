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

    def __init__(
        self,
        db: "Database",
        members: MembershipList,
        local_node_id: str = "",
    ) -> None:
        self._db = db
        self._members = members
        self._local_node_id = local_node_id

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

        1. Add peer and its known cards to membership.
        2. Update local gossip knowledge: local node now knows about
           peer and peer's friends. Peer now knows about its own friends.
        3. Return local membership list for the peer to merge.
        """
        # Add peer to membership if not already known
        if not self._members.contains(from_node.node_id):
            self._members.add(from_node)

        # Add all cards from peer to membership
        for card in known_cards:
            if not self._members.contains(card.node_id):
                self._members.add(card)

        # Bidirectional gossip knowledge merge:
        # 1) peer tells us about its known nodes → we now know them too
        peer_brought_ids = {c.node_id for c in known_cards}
        all_new_ids = peer_brought_ids | {from_node.node_id}

        # Store that the peer knows about its friends
        for nid in peer_brought_ids:
            if nid != from_node.node_id:
                await self._db.cluster_gossip_add(from_node.node_id, nid)

        # 2) local node also learns about peer + peer's friends
        if self._local_node_id:
            new_for_local = all_new_ids - {self._local_node_id}
            if new_for_local:
                await self._db.cluster_gossip_add_many(
                    self._local_node_id, new_for_local,
                )

        # Return our full membership for the peer to merge
        return self._members.all_nodes(exclude=from_node.node_id)

    async def add_known(self, node_id: str, known_id: str) -> None:
        """Directly add a known node."""
        await self._db.cluster_gossip_add(node_id, known_id)

    async def remove_node(self, node_id: str) -> None:
        """Remove a node from all gossip records."""
        await self._db.cluster_gossip_remove(node_id)
