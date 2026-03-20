"""Bootstrap: cold-start registration and authoritative AgentCard/ServerCard store.

Provides seed lists for new agents and fallback queries for discovery.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from eacn.network.db.database import Database


class Bootstrap:
    """Cold-start entry point and authoritative card storage."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # ── AgentCard ──────────────────────────────────────────────────────

    async def register_agent(self, card: dict[str, Any]) -> list[str]:
        """Store AgentCard, return seed list (same-domain agent IDs)."""
        await self._db.save_agent_card(card)
        # Build seed list from agents sharing any domain
        seeds: set[str] = set()
        for domain in card.get("domains", []):
            cards = await self._db.query_agent_cards_by_domain(domain)
            for c in cards:
                if c["agent_id"] != card["agent_id"]:
                    seeds.add(c["agent_id"])
        return list(seeds)

    async def unregister_agent(self, agent_id: str) -> None:
        await self._db.delete_agent_card(agent_id)

    async def get_agent_card(self, agent_id: str) -> dict[str, Any] | None:
        return await self._db.get_agent_card(agent_id)

    async def query(self, domains: list[str]) -> list[str]:
        """Fallback query: find agent IDs by domains from full storage."""
        result: set[str] = set()
        for domain in domains:
            cards = await self._db.query_agent_cards_by_domain(domain)
            for c in cards:
                result.add(c["agent_id"])
        return list(result)

    # ── ServerCard ─────────────────────────────────────────────────────

    async def register_server(
        self, server_id: str, version: str, endpoint: str, owner: str,
    ) -> None:
        await self._db.save_server_card(server_id, version, endpoint, owner)

    async def unregister_server(self, server_id: str) -> None:
        await self._db.delete_server_card(server_id)

    async def get_server_card(self, server_id: str) -> dict[str, Any] | None:
        return await self._db.get_server_card(server_id)

    async def set_server_status(self, server_id: str, status: str) -> None:
        await self._db.update_server_status(server_id, status)

    async def get_agent_ids_by_server(self, server_id: str) -> list[str]:
        return await self._db.get_agent_ids_by_server(server_id)
