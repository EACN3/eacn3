"""Gossip protocol: agents exchange known agent lists during collaboration.

Persisted via Database so knowledge survives restarts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.db.database import Database


class GossipProtocol:
    """Natural agent discovery spread through collaboration interactions."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def exchange(self, agent_a: str, agent_b: str) -> None:
        """Exchange known agent lists between two collaborating agents."""
        a_knows = await self._db.gossip_get_known(agent_a)
        b_knows = await self._db.gossip_get_known(agent_b)
        shared = a_knows | b_knows | {agent_a, agent_b}
        await self._db.gossip_add_many(agent_a, shared - {agent_a})
        await self._db.gossip_add_many(agent_b, shared - {agent_b})

    async def get_known(self, agent_id: str) -> set[str]:
        return await self._db.gossip_get_known(agent_id)

    async def lookup(self, agent_id: str, domain: str) -> list[str]:
        """Find agents in known list that handle the given domain."""
        known = await self._db.gossip_get_known(agent_id)
        results = []
        for kid in known:
            card = await self._db.get_agent_card(kid)
            if card and domain in card["domains"]:
                results.append(kid)
        return results
