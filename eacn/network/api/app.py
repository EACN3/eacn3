"""Network FastAPI application with lifespan management.

Startup: connect DB, init Network, wire push handler.
Shutdown: close DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

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

    # Wire push handler → WebSocket delivery
    async def ws_push_handler(event):
        await manager.broadcast_event(event)

    network.push.set_handler(ws_push_handler)

    app.state.db = db
    app.state.network = network
    set_network(network)
    set_discovery_network(network)
    set_peer_cluster(network.cluster)
    set_peer_network(network)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    await db.close()


def create_app(db_path: str = ":memory:") -> FastAPI:
    """Factory function for creating the Network API app."""
    app = FastAPI(
        title="EACN Network API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.db_path = db_path
    app.include_router(router)
    app.include_router(discovery_router)
    app.include_router(peer_router)
    app.include_router(ws_router)
    return app
