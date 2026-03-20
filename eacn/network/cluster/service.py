"""ClusterService: main coordinator for the cluster layer.

Standalone mode: when no seed nodes are configured, all cluster operations
are no-ops. Existing single-node behavior is preserved exactly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

import httpx

from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.discovery import ClusterDiscovery
from eacn.network.cluster.router import ClusterRouter
from eacn.network.config import ClusterConfig

if TYPE_CHECKING:
    from eacn.network.db.database import Database

_log = logging.getLogger(__name__)


class ClusterService:

    def __init__(self, db: "Database", config: ClusterConfig | None = None) -> None:
        self.config = config or ClusterConfig()
        self._db = db

        node_id = self.config.node_id or str(uuid.uuid4())
        endpoint = self.config.endpoint or ""

        self.local_node = NodeCard(
            node_id=node_id, endpoint=endpoint,
            version=self.config.protocol_version,
        )

        self.members = MembershipList()
        self.members.add(self.local_node)

        self.bootstrap = ClusterBootstrap(self.local_node, self.members, self.config)
        self.dht = ClusterDHT(db)
        self.gossip = ClusterGossip(db, self.members, local_node_id=node_id)
        self.discovery = ClusterDiscovery(node_id, self.gossip, self.dht, self.bootstrap)
        self.router = ClusterRouter(db, node_id)

        self._standalone = not bool(self.config.seed_nodes)

    @property
    def standalone(self) -> bool:
        return self._standalone

    @property
    def node_id(self) -> str:
        return self.local_node.node_id

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._standalone:
            _log.info("Cluster starting in standalone mode")
            return

        peers = await self.bootstrap.join_network()
        for peer in peers:
            self.members.add(peer)
            self.router.set_endpoint(peer.node_id, peer.endpoint)

        for domain in self.local_node.domains:
            await self.dht.announce(domain, self.node_id)

        _log.info("Cluster started: node=%s, peers=%d",
                   self.node_id, self.members.count() - 1)

    async def stop(self) -> None:
        if self._standalone:
            return
        await self.bootstrap.leave_network(
            self.members.all_nodes(exclude=self.node_id))
        await self.dht.revoke_all(self.node_id)

    # ── Domain management ────────────────────────────────────────────

    async def announce_domain(self, domain: str) -> None:
        if domain not in self.local_node.domains:
            self.local_node.domains.append(domain)
        await self.dht.announce(domain, self.node_id)

    async def revoke_domain(self, domain: str) -> None:
        if domain in self.local_node.domains:
            self.local_node.domains.remove(domain)
        await self.dht.revoke(domain, self.node_id)

    # ── Task broadcasting ────────────────────────────────────────────

    async def broadcast_task(self, task_summary: dict[str, Any]) -> list[str]:
        """Broadcast task to peer nodes handling relevant domains."""
        if self._standalone:
            return []

        target_nodes: set[str] = set()
        for domain in task_summary.get("domains", []):
            target_nodes.update(await self.discovery.discover(domain))
        target_nodes.discard(self.node_id)

        if not target_nodes:
            return []

        notified: list[str] = []
        for node_id in target_nodes:
            endpoint = self.router.get_endpoint(node_id)
            if not endpoint:
                card = self.members.get(node_id)
                if card:
                    endpoint = card.endpoint
                    self.router.set_endpoint(node_id, endpoint)
                else:
                    continue
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{endpoint}/peer/task/broadcast",
                        json={**task_summary, "origin": self.node_id},
                    )
                    resp.raise_for_status()
                    notified.append(node_id)
            except Exception:
                _log.warning("Failed to broadcast task to node %s", node_id)

        return notified

    # ── Gossip trigger ───────────────────────────────────────────────

    async def trigger_gossip(self, task_id: str) -> None:
        if self._standalone:
            return
        for node_id in self.router.get_participants(task_id):
            if node_id != self.node_id:
                await self.gossip.exchange(self.node_id, node_id)

    # ── Peer request handlers (called by peer_routes) ────────────────

    def handle_join(self, card: NodeCard) -> list[NodeCard]:
        nodes = self.bootstrap.handle_join(card)
        self.router.set_endpoint(card.node_id, card.endpoint)
        return nodes

    def handle_leave(self, node_id: str) -> None:
        self.bootstrap.handle_leave(node_id)

    def handle_heartbeat(self, node_id: str, domains: list[str], timestamp: str) -> None:
        self.bootstrap.handle_heartbeat(node_id, domains, timestamp)

    def handle_broadcast(self, task_summary: dict[str, Any]) -> None:
        task_id = task_summary.get("task_id", "")
        origin = task_summary.get("origin", "")
        if task_id and origin:
            self.router.set_route(task_id, origin)

    async def handle_status_notification(
        self, task_id: str, status: str, payload: dict[str, Any],
    ) -> None:
        pass  # Local push delivery handled by caller

    async def handle_push(self, event_type: str, task_id: str,
                          recipients: list[str], payload: dict[str, Any]) -> int:
        return len(recipients)
