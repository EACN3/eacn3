"""Network FastAPI application with lifespan management.

Startup: connect DB, init Network, wire push handler + offline store.
Shutdown: close DB.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from eacn.network.app import Network
from eacn.network.db import Database
from eacn.network.offline_store import OfflineStore
from eacn.network.api.routes import router, set_network
from eacn.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network
from eacn.network.api.websocket import ws_router, manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────
    db_path = app.state.db_path if hasattr(app.state, "db_path") else ":memory:"
    db = Database(db_path)
    await db.connect()

    network = Network(db=db)
    await network.start()

    # ── Offline store & ACK config ───────────────────────────────────
    push_cfg = network.config.push
    offline_store = OfflineStore(
        db=db,
        max_per_agent=push_cfg.offline_max_per_agent,
        ttl_seconds=push_cfg.offline_ttl_seconds,
    )
    manager.set_offline_store(offline_store)
    manager.ack_timeout = push_cfg.ack_timeout

    # Wire push handler → queue-first delivery
    #
    # Every event is written to the per-agent message queue (OfflineStore)
    # FIRST. This is the source of truth — agents poll via HTTP to drain it.
    # WebSocket is an optional accelerator: if the agent happens to be
    # connected, we push a copy for low-latency delivery. But the queue
    # is always populated regardless.
    async def queue_first_push_handler(event):
        """Enqueue for all recipients, then optionally notify via WS."""
        # 1. Enqueue for every recipient (primary delivery path)
        for agent_id in event.recipients:
            await offline_store.store(
                msg_id=event.msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )

        # 2. Best-effort WS push for low-latency (optional accelerator)
        #    Agents that are WS-connected get notified immediately.
        #    They still drain the queue via HTTP — WS is just a hint.
        for agent_id in event.recipients:
            if manager.is_connected(agent_id):
                try:
                    await manager.send_to(agent_id, {
                        "msg_id": event.msg_id,
                        "type": event.type.value,
                        "task_id": event.task_id,
                        "payload": event.payload,
                    })
                except Exception:
                    pass  # Queue already has it — WS failure is harmless

        # 3. Forward to remote cluster nodes for their local agents
        local_agents = {r for r in event.recipients if manager.is_connected(r)}
        remote_agents = [r for r in event.recipients if r not in local_agents]
        if remote_agents:
            participant_nodes = network.cluster.router.get_participants(event.task_id)
            if participant_nodes:
                await network.cluster.router.forward_push(
                    event.type.value,
                    event.task_id,
                    remote_agents,
                    event.payload,
                    participant_nodes,
                )

    network.push.set_handler(queue_first_push_handler)

    # Cluster handler: remote node forwarded an event to us.
    # Same logic: enqueue first, then WS hint.
    async def cluster_push_handler(event):
        for agent_id in event.recipients:
            await offline_store.store(
                msg_id=event.msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )
            if manager.is_connected(agent_id):
                try:
                    await manager.send_to(agent_id, {
                        "msg_id": event.msg_id,
                        "type": event.type.value,
                        "task_id": event.task_id,
                        "payload": event.payload,
                    })
                except Exception:
                    pass

    network.cluster.set_push_handler(cluster_push_handler)

    app.state.db = db
    app.state.network = network
    app.state.offline_store = offline_store
    set_network(network)
    set_discovery_network(network)
    set_peer_cluster(network.cluster)
    set_peer_network(network)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    await network.cluster.stop()
    await db.close()


def create_app(db_path: str | None = None) -> FastAPI:
    """Factory function for creating the Network API app."""
    app = FastAPI(
        title="EACN Network API",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Read from env var if not explicitly provided; fall back to file-based default
    resolved_db_path = db_path or os.environ.get("EACN3_DB_PATH", "eacn3.db")
    app.state.db_path = resolved_db_path

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(peer_router)
    app.include_router(ws_router)
    return app
