"""ClusterService: main coordinator for the cluster layer.

Standalone mode: when no seed nodes are configured, all cluster operations
are no-ops. Existing single-node behavior is preserved exactly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Awaitable, TYPE_CHECKING

import httpx

from eacn.network.cluster.node import NodeCard, MembershipList
from eacn.network.cluster.bootstrap import ClusterBootstrap
from eacn.network.cluster.dht import ClusterDHT
from eacn.network.cluster.gossip import ClusterGossip
from eacn.network.cluster.discovery import ClusterDiscovery
from eacn.network.cluster.router import ClusterRouter
from eacn.network.config import ClusterConfig

if TYPE_CHECKING:
    from eacn.core.models import PushEvent
    from eacn.network.db.database import Database

# Callback that delivers a PushEvent to locally connected agents.
# Returns the number of recipients successfully delivered.
LocalPushHandler = Callable[["PushEvent"], Awaitable[int]]

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

        self._push_handler: LocalPushHandler | None = None
        self._agent_counts: dict[str, int] = {}  # node_id → connected agent count
        self._agent_counts_lock = asyncio.Lock()  # Protects _agent_counts (#105)
        self._standalone = not bool(self.config.seed_nodes)
        self._http: httpx.AsyncClient | None = None

    @property
    def standalone(self) -> bool:
        return self._standalone

    @property
    def node_id(self) -> str:
        return self.local_node.node_id

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        # Create a shared HTTP client for all cluster communication
        self._http = httpx.AsyncClient(timeout=10.0)
        self.router.set_http_client(self._http)
        self.bootstrap.set_http_client(self._http)

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
        if not self._standalone:
            await self.bootstrap.leave_network(
                self.members.all_nodes(exclude=self.node_id))
            await self.dht.revoke_all(self.node_id)
        # Close the shared HTTP client
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Domain management ────────────────────────────────────────────

    async def announce_domain(self, domain: str) -> None:
        if domain not in self.local_node.domains:
            self.local_node.domains.append(domain)
        await self.dht.announce(domain, self.node_id)

    async def revoke_domain(self, domain: str) -> None:
        if domain in self.local_node.domains:
            self.local_node.domains.remove(domain)
        await self.dht.revoke(domain, self.node_id)

    # ── Node health ────────────────────────────────────────────────

    def mark_node_suspect(self, node_id: str) -> None:
        """Mark a node as suspect (missed heartbeats). Agents on it may be unreachable."""
        self.members.update_status(node_id, "suspect")
        agent_count = self._agent_counts.get(node_id, 0)
        _log.warning("Node %s marked suspect (%d agents may be unreachable)",
                      node_id, agent_count)

    def mark_node_offline(self, node_id: str) -> None:
        """Mark a node as offline. Its agents are unreachable."""
        self.members.update_status(node_id, "offline")
        agent_count = self._agent_counts.pop(node_id, 0)
        _log.warning("Node %s marked offline (%d agents lost)", node_id, agent_count)

    def get_agent_counts(self) -> dict[str, int]:
        """Return connected agent counts per node (from last heartbeat)."""
        return dict(self._agent_counts)

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
                if self._http:
                    resp = await self._http.post(
                        f"{endpoint}/peer/task/broadcast",
                        json={**task_summary, "origin": self.node_id},
                    )
                else:
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

    # ── Push handler ────────────────────────────────────────────────

    def set_push_handler(self, handler: LocalPushHandler) -> None:
        """Register a callback for delivering push events to local agents."""
        self._push_handler = handler

    # ── Peer request handlers (called by peer_routes) ────────────────

    def handle_join(self, card: NodeCard) -> list[NodeCard]:
        nodes = self.bootstrap.handle_join(card)
        self.router.set_endpoint(card.node_id, card.endpoint)
        return nodes

    def handle_leave(self, node_id: str) -> None:
        self.bootstrap.handle_leave(node_id)

    def handle_heartbeat(self, node_id: str, domains: list[str], timestamp: str,
                         connected_agents: int = 0) -> None:
        old_status = None
        node = self.members.get(node_id)
        if node:
            old_status = node.status
        self.bootstrap.handle_heartbeat(node_id, domains, timestamp)
        # Track agent count for observability
        self._agent_counts[node_id] = connected_agents
        # Log if node came back online
        if old_status and old_status != "online":
            _log.info("Node %s back online with %d agents", node_id, connected_agents)

    def handle_broadcast(self, task_summary: dict[str, Any]) -> None:
        task_id = task_summary.get("task_id", "")
        origin = task_summary.get("origin", "")
        if task_id and origin:
            self.router.set_route(task_id, origin)

    async def handle_status_notification(
        self, task_id: str, status: str, payload: dict[str, Any],
    ) -> None:
        """Deliver a status change notification to local agents.

        The payload should include 'recipients' (agent IDs to notify).
        Falls back to a no-op if no push handler or no recipients.
        """
        recipients = payload.get("recipients", [])
        if not self._push_handler or not recipients:
            return
        from eacn.core.models import PushEvent, PushEventType
        # Map status to an appropriate event type when possible
        _status_event_map = {
            "awaiting_retrieval": PushEventType.TASK_COLLECTED,
            "timeout": PushEventType.TASK_TIMEOUT,
        }
        event_type = _status_event_map.get(status)
        if not event_type:
            # No matching push event type for this status — skip
            _log.debug("No push event type for status %s, skipping", status)
            return
        try:
            event = PushEvent(
                type=event_type,
                task_id=task_id,
                recipients=recipients,
                payload=payload,
            )
            await self._push_handler(event)
        except Exception:
            _log.warning("Failed to deliver status notification %s for task %s",
                         status, task_id, exc_info=True)

    async def handle_push(self, event_type: str, task_id: str,
                          recipients: list[str], payload: dict[str, Any]) -> int:
        """Deliver a forwarded push event to locally connected agents."""
        if not self._push_handler or not recipients:
            return 0
        from eacn.core.models import PushEvent, PushEventType
        try:
            event = PushEvent(
                type=PushEventType(event_type),
                task_id=task_id,
                recipients=recipients,
                payload=payload,
            )
            return await self._push_handler(event)
        except Exception:
            _log.warning("Failed to deliver forwarded push event %s", event_type,
                         exc_info=True)
            return 0
