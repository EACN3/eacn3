"""Discovery query with three-layer fallback: Gossip → DHT → Bootstrap."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from eacn.network.discovery.dht import DHT
from eacn.network.discovery.gossip import GossipProtocol
from eacn.network.discovery.bootstrap import Bootstrap

if TYPE_CHECKING:
    from eacn.network.db.database import Database


class DiscoveryService:
    """Orchestrates three-layer discovery and agent registration."""

    def __init__(self, db: Database) -> None:
        self.dht = DHT(db)
        self.gossip = GossipProtocol(db)
        self.bootstrap = Bootstrap(db)

    async def discover(self, domain: str, requester_id: str | None = None) -> list[str]:
        """Find agents for a domain. Fallback: Gossip → DHT → Bootstrap."""
        # Layer 1: Gossip (local knowledge)
        if requester_id:
            gossip_results = await self.gossip.lookup(requester_id, domain)
            if gossip_results:
                return gossip_results

        # Layer 2: DHT (precise lookup)
        dht_results = await self.dht.lookup(domain)
        if dht_results:
            return dht_results

        # Layer 3: Bootstrap (fallback, full scan)
        return await self.bootstrap.query([domain])

    async def register_agent(self, card: dict[str, Any]) -> list[str]:
        """Register agent: store card + announce to DHT. Returns seed list."""
        # 1. Bootstrap stores the card
        seeds = await self.bootstrap.register_agent(card)
        # 2. DHT announces each domain
        for domain in card.get("domains", []):
            await self.dht.announce(domain, card["agent_id"])
        return seeds

    async def unregister_agent(self, agent_id: str) -> None:
        """Unregister agent: revoke from DHT + remove card."""
        await self.dht.revoke_all(agent_id)
        await self.bootstrap.unregister_agent(agent_id)

    async def register_server(
        self, server_id: str, version: str, endpoint: str, owner: str,
    ) -> None:
        await self.bootstrap.register_server(server_id, version, endpoint, owner)

    async def unregister_server(self, server_id: str) -> None:
        """Unregister server: revoke all its agents from DHT + delete agent cards + remove server card."""
        # 1. Revoke all agents from DHT (uses agent_cards for server→agent mapping)
        await self.dht.revoke_by_server(server_id)
        # 2. Delete all agent cards belonging to this server
        agent_ids = await self.bootstrap.get_agent_ids_by_server(server_id)
        for agent_id in agent_ids:
            await self.bootstrap.unregister_agent(agent_id)
        # 3. Delete server card
        await self.bootstrap.unregister_server(server_id)
