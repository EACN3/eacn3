"""Network FastAPI application with lifespan management.

Startup: connect DB, init Network, wire push handler + message queue.
Shutdown: close DB.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from eacn.network.app import Network
from eacn.network.db import Database
from eacn.network.offline_store import OfflineStore
from eacn.network.api.routes import router, set_network, set_offline_store
from eacn.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn.network.api.peer_routes import peer_router, set_peer_cluster, set_peer_network


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────
    db_path = app.state.db_path if hasattr(app.state, "db_path") else ":memory:"
    db = Database(db_path)
    await db.connect()

    network = Network(db=db)
    try:
        await network.start()
    except Exception:
        # Clean up resources on startup failure (#41)
        await network.cluster.stop()
        await db.close()
        raise

    # ── Message queue (per-agent) ─────────────────────────────────────
    push_cfg = network.config.push
    offline_store = OfflineStore(
        db=db,
        max_per_agent=push_cfg.offline_max_per_agent,
        ttl_seconds=push_cfg.offline_ttl_seconds,
    )

    # Wire push handler → queue-first delivery
    #
    # Every event is written to the per-agent message queue unconditionally.
    # Agents drain it via HTTP GET /api/events/{agent_id}.
    # No WebSocket — just a queue.
    async def queue_push_handler(event):
        """Enqueue for all recipients with unique msg_id per recipient (#69)."""
        import uuid
        for agent_id in event.recipients:
            per_agent_msg_id = uuid.uuid4().hex
            await offline_store.store(
                msg_id=per_agent_msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )

        # Forward to remote cluster nodes for their local agents
        if len(event.recipients) > 0:
            participant_nodes = network.cluster.router.get_participants(event.task_id)
            if participant_nodes:
                await network.cluster.router.forward_push(
                    event.type.value,
                    event.task_id,
                    event.recipients,
                    event.payload,
                    participant_nodes,
                )

    network.push.set_handler(queue_push_handler)

    # Cluster handler: remote node forwarded an event — enqueue locally.
    async def cluster_push_handler(event):
        import uuid
        for agent_id in event.recipients:
            per_agent_msg_id = uuid.uuid4().hex
            await offline_store.store(
                msg_id=per_agent_msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )

    network.cluster.set_push_handler(cluster_push_handler)

    app.state.db = db
    app.state.network = network
    app.state.offline_store = offline_store
    app.state.startup_complete = True  # Gate for #42
    set_network(network)
    set_offline_store(offline_store)
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
    app.state.startup_complete = False

    @app.middleware("http")
    async def startup_gate(request: Request, call_next):
        """Return 503 until startup is complete (#42)."""
        if not getattr(request.app.state, "startup_complete", False):
            if request.url.path != "/health":
                return JSONResponse({"detail": "Server starting up"}, status_code=503)
        return await call_next(request)

    # Read from env var if not explicitly provided; fall back to file-based default
    resolved_db_path = db_path or os.environ.get("EACN3_DB_PATH", "eacn3.db")
    # Validate path has no traversal (#48)
    if resolved_db_path != ":memory:" and ".." in os.path.normpath(resolved_db_path):
        raise ValueError(f"DB path must not contain path traversal: {resolved_db_path}")
    app.state.db_path = resolved_db_path

    @app.get("/health")
    async def health():
        return JSONResponse({"status": "ok"})

    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(peer_router)
    return app
