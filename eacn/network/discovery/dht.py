"""DHT (Distributed Hash Table) for domain-based agent discovery.

Maps domain tags to sets of agent IDs. Persisted via Database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.db.database import Database


class DHT:
    """Domain → agent_ids mapping, backed by database."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def announce(self, domain: str, agent_id: str) -> None:
        await self._db.dht_announce(domain, agent_id)

    async def revoke(self, domain: str, agent_id: str) -> None:
        await self._db.dht_revoke(domain, agent_id)

    async def revoke_all(self, agent_id: str) -> None:
        await self._db.dht_revoke_all(agent_id)

    async def revoke_by_server(self, server_id: str) -> None:
        await self._db.dht_revoke_by_server(server_id)

    async def lookup(self, domain: str) -> list[str]:
        return await self._db.dht_lookup(domain)
