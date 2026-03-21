"""Shared fixtures for API-level tests.

All tests go through HTTP endpoints — no direct class instantiation.
Network is set up with funded accounts and DHT entries for testing.
"""

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn3.network.api.websocket import ws_router


# ── Helpers ──────────────────────────────────────────────────────────

def _make_network_app(network: Network) -> FastAPI:
    """Create a bare FastAPI app with a pre-wired Network (no lifespan)."""
    app = FastAPI()
    app.include_router(net_router)
    app.include_router(discovery_router)
    app.include_router(ws_router)
    set_network(network)
    set_discovery_network(network)
    return app


# ── Network-level fixtures ───────────────────────────────────────────

@pytest.fixture
async def network():
    """Bare Network instance with in-memory DB (no pre-funding)."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    yield net
    await db.close()


@pytest.fixture
async def funded_network(network):
    """Network with funded user accounts + DHT agent entries.

    Accounts:
      user1 — 10 000 credits
      user2 — 5 000 credits

    DHT:
      coding   → a1, a2, a3
      design   → a4
      research → a5

    Reputation:
      a1=0.8, a2=0.75, a3=0.7, a4=0.65, a5=0.6
    """
    net = network
    # Fund accounts
    net.escrow.get_or_create_account("user1", 10_000.0)
    net.escrow.get_or_create_account("user2", 5_000.0)
    # DHT entries
    for agent_id in ("a1", "a2", "a3"):
        await net.dht.announce("coding", agent_id)
    await net.dht.announce("design", "a4")
    await net.dht.announce("research", "a5")
    # Reputation scores
    net.reputation._scores.update({
        "a1": 0.8, "a2": 0.75, "a3": 0.7, "a4": 0.65, "a5": 0.6,
    })
    return net


# ── HTTP Client fixtures ────────────────────────────────────────────

@pytest.fixture
async def api(network):
    """Unfunded httpx client → Network API."""
    app = _make_network_app(network)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def client(funded_network):
    """Funded httpx client → Network API (most tests use this)."""
    app = _make_network_app(funded_network)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Convenience helpers ─────────────────────────────────────────────

async def create_task(
    client: AsyncClient,
    task_id: str = "t1",
    initiator_id: str = "user1",
    domains: list[str] | None = None,
    budget: float = 100.0,
    **kwargs,
) -> dict:
    """POST /api/tasks and return JSON (asserts 201)."""
    body = {
        "task_id": task_id,
        "initiator_id": initiator_id,
        "content": kwargs.pop("content", {"desc": "test"}),
        "domains": domains or ["coding"],
        "budget": budget,
        **kwargs,
    }
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def bid(
    client: AsyncClient,
    task_id: str = "t1",
    agent_id: str = "a1",
    confidence: float = 0.9,
    price: float = 80.0,
    **kwargs,
) -> dict:
    """POST /api/tasks/{id}/bid and return JSON (asserts 200)."""
    body = {"agent_id": agent_id, "confidence": confidence, "price": price, **kwargs}
    resp = await client.post(f"/api/tasks/{task_id}/bid", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def submit_result(
    client: AsyncClient,
    task_id: str = "t1",
    agent_id: str = "a1",
    content: str = "result content",
) -> dict:
    """POST /api/tasks/{id}/result and return JSON (asserts 200)."""
    resp = await client.post(
        f"/api/tasks/{task_id}/result",
        json={"agent_id": agent_id, "content": content},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def close_task(
    client: AsyncClient,
    task_id: str = "t1",
    initiator_id: str = "user1",
) -> dict:
    """POST /api/tasks/{id}/close and return JSON (asserts 200)."""
    resp = await client.post(
        f"/api/tasks/{task_id}/close",
        json={"initiator_id": initiator_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def select_result(
    client: AsyncClient,
    task_id: str = "t1",
    agent_id: str = "a1",
    initiator_id: str = "user1",
) -> dict:
    """POST /api/tasks/{id}/select and return JSON (asserts 200)."""
    resp = await client.post(
        f"/api/tasks/{task_id}/select",
        json={"initiator_id": initiator_id, "agent_id": agent_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def setup_task_with_bid(
    client: AsyncClient,
    task_id: str = "t1",
    agent_id: str = "a1",
    budget: float = 100.0,
    price: float = 80.0,
    **task_kwargs,
) -> tuple[dict, dict]:
    """Create a task and have an agent bid on it. Returns (task, bid)."""
    task = await create_task(client, task_id=task_id, budget=budget, **task_kwargs)
    b = await bid(client, task_id=task_id, agent_id=agent_id, price=price)
    return task, b


async def setup_task_with_result(
    client: AsyncClient,
    task_id: str = "t1",
    agent_id: str = "a1",
    budget: float = 100.0,
    price: float = 80.0,
    **task_kwargs,
) -> tuple[dict, dict, dict]:
    """Create task → bid → submit result. Returns (task, bid, result_resp)."""
    task, b = await setup_task_with_bid(
        client, task_id=task_id, agent_id=agent_id, budget=budget, price=price,
        **task_kwargs,
    )
    r = await submit_result(client, task_id=task_id, agent_id=agent_id)
    return task, b, r
