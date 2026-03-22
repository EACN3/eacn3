"""Network FastAPI application with lifespan management.

Startup: connect DB, init Network, wire push handler.
Shutdown: close DB.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from eacn.network.app import Network
from eacn.network.db import Database
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

    # Wire push handler → WebSocket delivery + cross-node forwarding
    async def ws_push_handler(event):
        """Deliver locally, then forward undelivered recipients to remote nodes."""
        delivered = await manager.broadcast_event(event)

        # If all recipients were delivered locally, we're done
        if delivered >= len(event.recipients):
            return

        # Find recipients not connected to this node
        undelivered = [r for r in event.recipients if not manager.is_connected(r)]
        if not undelivered:
            return

        # Forward to participant nodes that may have these agents connected
        participant_nodes = network.cluster.router.get_participants(event.task_id)
        if participant_nodes:
            await network.cluster.router.forward_push(
                event.type.value,
                event.task_id,
                undelivered,
                event.payload,
                participant_nodes,
            )

    network.push.set_handler(ws_push_handler)

    # Wire cluster local push handler (receives forwarded events from peer nodes)
    network.cluster.set_push_handler(manager.broadcast_event)

    app.state.db = db
    app.state.network = network
    set_network(network)
    set_discovery_network(network)
    set_peer_cluster(network.cluster)
    set_peer_network(network)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
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
