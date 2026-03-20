"""NodeCard model and membership management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class NodeCard(BaseModel):
    """Identity and metadata for a network node."""

    node_id: str
    endpoint: str
    domains: list[str] = Field(default_factory=list)
    status: str = "online"  # online | suspect | offline
    version: str = "0.1.0"
    joined_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeCard:
        return cls(**data)


class MembershipList:
    """In-memory membership list backed by database persistence."""

    def __init__(self) -> None:
        self._nodes: dict[str, NodeCard] = {}

    def add(self, card: NodeCard) -> None:
        self._nodes[card.node_id] = card

    def remove(self, node_id: str) -> NodeCard | None:
        return self._nodes.pop(node_id, None)

    def get(self, node_id: str) -> NodeCard | None:
        return self._nodes.get(node_id)

    def all_online(self, exclude: str | None = None) -> list[NodeCard]:
        """Return all nodes with status 'online', optionally excluding one."""
        return [
            n for n in self._nodes.values()
            if n.status == "online" and n.node_id != exclude
        ]

    def all_nodes(self, exclude: str | None = None) -> list[NodeCard]:
        return [
            n for n in self._nodes.values()
            if n.node_id != exclude
        ]

    def find_by_domain(self, domain: str, exclude: str | None = None) -> list[str]:
        """Find node IDs whose domains include the given domain."""
        return [
            n.node_id for n in self._nodes.values()
            if domain in n.domains
            and n.status == "online"
            and n.node_id != exclude
        ]

    def update_last_seen(self, node_id: str, timestamp: str | None = None) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.last_seen = timestamp or datetime.now(timezone.utc).isoformat()

    def update_domains(self, node_id: str, domains: list[str]) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.domains = domains

    def update_status(self, node_id: str, status: str) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.status = status

    def contains(self, node_id: str) -> bool:
        return node_id in self._nodes

    def count(self) -> int:
        return len(self._nodes)
