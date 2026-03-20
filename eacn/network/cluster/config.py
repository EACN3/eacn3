"""Cluster configuration: seed nodes, heartbeat, timeouts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClusterConfig(BaseModel):
    """Configuration for the cluster layer."""

    seed_nodes: list[str] = Field(default_factory=list)
    """Seed node endpoints, e.g. ["http://seed-1:8000", "http://seed-2:8000"]."""

    heartbeat_interval: int = 10
    """Seconds between heartbeat rounds."""

    heartbeat_fan_out: int = 3
    """Number of random peers to heartbeat each round (K)."""

    suspect_rounds: int = 3
    """Consecutive missed heartbeats before marking suspect (M)."""

    offline_rounds: int = 6
    """Consecutive missed heartbeats before marking offline (2M)."""

    node_id: str = ""
    """Stable node ID. If empty, auto-generated on first start and persisted."""

    endpoint: str = ""
    """This node's public endpoint for peer communication."""

    protocol_version: str = "0.1.0"
    """Protocol version string."""
