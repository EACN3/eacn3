"""Tests: production-realistic multi-agent scenarios via HTTP API.

Scenarios:
  1. Multiple servers + agents register, discover each other, heartbeat
  2. Agents bid on tasks concurrently, queue management, reject/promote
  3. Agent goes offline mid-task, another agent takes over
  4. Agent reconnects via WebSocket after disconnect
  5. Full lifecycle: register → discover → bid → execute → submit → select → settle
  6. Server unregister cascades agent cleanup
  7. Concurrent tasks with budget contention
"""

import time
import pytest
from httpx import AsyncClient
from starlette.testclient import TestClient

from eacn.network.api.websocket import manager as ws_manager
from tests.api.conftest import (
    _make_network_app, create_task, bid, submit_result, select_result,
)


# ── Helpers ──────────────────────────────────────────────────────────

async def register_server(api: AsyncClient, endpoint: str = "http://srv:8000", owner: str = "alice"):
    resp = await api.post("/api/discovery/servers", json={
        "version": "0.1.0", "endpoint": endpoint, "owner": owner,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()["server_id"]


async def register_agent(
    api: AsyncClient, agent_id: str, server_id: str,
    domains: list[str], name: str = "Agent",
):
    resp = await api.post("/api/discovery/agents", json={
        "agent_id": agent_id, "name": name,
        "domains": domains,
        "skills": [{"name": d} for d in domains],
        "url": f"http://{agent_id}:9000",
        "server_id": server_id,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


async def heartbeat(api: AsyncClient, server_id: str):
    resp = await api.post(f"/api/discovery/servers/{server_id}/heartbeat")
    assert resp.status_code == 200
    return resp.json()


async def fund(api: AsyncClient, agent_id: str, amount: float):
    resp = await api.post("/api/admin/fund", json={"agent_id": agent_id, "amount": amount})
    assert resp.status_code == 200
    return resp.json()


async def get_task(api: AsyncClient, task_id: str) -> dict:
    resp = await api.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    return resp.json()


async def reject_task(api: AsyncClient, task_id: str, agent_id: str, reason: str = ""):
    resp = await api.post(f"/api/tasks/{task_id}/reject", json={
        "agent_id": agent_id, "reason": reason,
    })
    assert resp.status_code == 200
    return resp.json()


async def discover(api: AsyncClient, domain: str) -> list[str]:
    resp = await api.get("/api/discovery/query", params={"domain": domain})
    assert resp.status_code == 200
    return resp.json()["agent_ids"]


# ══════════════════════════════════════════════════════════════════════
# Scenario 1: Multi-server, multi-agent registration & discovery
# ══════════════════════════════════════════════════════════════════════

class TestMultiAgentRegistration:
    """Two servers each register multiple agents across overlapping domains."""

    @pytest.mark.asyncio
    async def test_two_servers_register_agents_and_discover(self, api):
        # ── Two servers come online ───────────────────────────────
        srv1 = await register_server(api, "http://srv1:8000", "alice")
        srv2 = await register_server(api, "http://srv2:8000", "bob")

        # ── Server 1: 3 coding agents ────────────────────────────
        for i in range(1, 4):
            await register_agent(api, f"coder-{i}", srv1, ["coding"])

        # ── Server 2: 2 coding + 1 design agent ──────────────────
        await register_agent(api, "coder-4", srv2, ["coding"])
        await register_agent(api, "coder-5", srv2, ["coding"])
        await register_agent(api, "designer-1", srv2, ["design"])

        # ── Discovery: coding domain → all 5 coders ──────────────
        coding_agents = await discover(api, "coding")
        assert len(coding_agents) == 5
        for i in range(1, 6):
            assert f"coder-{i}" in coding_agents

        # ── Discovery: design domain → 1 designer ────────────────
        design_agents = await discover(api, "design")
        assert design_agents == ["designer-1"]

        # ── Heartbeat both servers ────────────────────────────────
        await heartbeat(api, srv1)
        await heartbeat(api, srv2)

        # ── Verify server cards ───────────────────────────────────
        resp = await api.get(f"/api/discovery/servers/{srv1}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "online"

    @pytest.mark.asyncio
    async def test_agent_with_multiple_domains(self, api):
        """An agent skilled in both coding and design is discoverable in both."""
        srv = await register_server(api, "http://srv:8000", "carol")
        await register_agent(api, "fullstack-1", srv, ["coding", "design"])

        assert "fullstack-1" in await discover(api, "coding")
        assert "fullstack-1" in await discover(api, "design")

    @pytest.mark.asyncio
    async def test_re_register_agent_updates_card(self, api):
        """Re-registering same agent_id updates its card (idempotent)."""
        srv = await register_server(api, "http://srv:8000", "dave")
        await register_agent(api, "agent-x", srv, ["coding"])
        # Re-register with different domains
        resp = await api.post("/api/discovery/agents", json={
            "agent_id": "agent-x", "name": "Updated Agent",
            "domains": ["coding", "design"], "skills": [{"name": "coding"}, {"name": "design"}],
            "url": "http://agent-x:9000", "server_id": srv,
        })
        assert resp.status_code == 201
        # Now discoverable in both domains
        assert "agent-x" in await discover(api, "design")


# ══════════════════════════════════════════════════════════════════════
# Scenario 2: Concurrent bidding, queue management, reject & promote
# ══════════════════════════════════════════════════════════════════════

class TestConcurrentBiddingAndQueue:
    """Multiple agents bid on the same task; queue fills; reject promotes."""

    @pytest.mark.asyncio
    async def test_agents_fill_concurrent_slots_then_queue(self, client):
        """5 agents bid on a task with max_concurrent_bidders=3.
        First 3 → EXECUTING, next 2 → WAITING."""
        task_data = await create_task(
            client, task_id="busy-task", budget=500.0,
            max_concurrent_bidders=3,
        )
        assert task_data["status"] == "unclaimed"

        statuses = []
        for i, agent_id in enumerate(["a1", "a2", "a3", "a4", "a5"]):
            price = 80.0 + i
            b = await bid(client, task_id="busy-task", agent_id=agent_id, price=price)
            statuses.append(b["status"])

        # First 3 executing, last 2 waiting
        assert statuses[:3] == ["executing", "executing", "executing"]
        assert statuses[3:] == ["waiting", "waiting"]

    @pytest.mark.asyncio
    async def test_reject_promotes_waiting_agent(self, client):
        """Agent rejects task → next in queue gets promoted to EXECUTING."""
        await create_task(client, task_id="promo-task", budget=500.0, max_concurrent_bidders=2)

        await bid(client, task_id="promo-task", agent_id="a1", price=80.0)
        await bid(client, task_id="promo-task", agent_id="a2", price=85.0)
        b3 = await bid(client, task_id="promo-task", agent_id="a3", price=90.0)
        assert b3["status"] == "waiting"

        # a1 rejects → a3 should be promoted
        await reject_task(client, "promo-task", "a1", reason="too busy")

        task = await get_task(client, "promo-task")
        bid_map = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert bid_map["a1"] == "rejected"
        assert bid_map["a3"] == "executing"  # promoted from queue

    @pytest.mark.asyncio
    async def test_submit_result_promotes_waiting(self, client):
        """Agent submits result → slot opens → waiting agent promoted."""
        await create_task(client, task_id="submit-promo", budget=500.0, max_concurrent_bidders=1)

        await bid(client, task_id="submit-promo", agent_id="a1", price=80.0)
        b2 = await bid(client, task_id="submit-promo", agent_id="a2", price=85.0)
        assert b2["status"] == "waiting"

        # a1 submits → a2 gets promoted
        await submit_result(client, task_id="submit-promo", agent_id="a1", content="done")

        task = await get_task(client, "submit-promo")
        bid_map = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert bid_map["a2"] == "executing"


# ══════════════════════════════════════════════════════════════════════
# Scenario 3: Agent goes offline, task gets rejected, another takes over
# ══════════════════════════════════════════════════════════════════════

class TestAgentOfflineAndTakeover:
    """Simulates agent crash: executing agent rejects → queued agent takes over."""

    @pytest.mark.asyncio
    async def test_crash_and_takeover(self, client):
        await create_task(client, task_id="crash-task", budget=300.0, max_concurrent_bidders=1)

        # Agent a1 starts executing
        b1 = await bid(client, task_id="crash-task", agent_id="a1", price=80.0)
        assert b1["status"] == "executing"

        # Agent a2 is waiting
        b2 = await bid(client, task_id="crash-task", agent_id="a2", price=85.0)
        assert b2["status"] == "waiting"

        # ── a1 crashes / goes offline → network rejects on its behalf ──
        await reject_task(client, "crash-task", "a1", reason="agent offline")

        # ── a2 is promoted and can now complete the work ──────────
        task = await get_task(client, "crash-task")
        bid_map = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert bid_map["a1"] == "rejected"
        assert bid_map["a2"] == "executing"

        # a2 submits result
        await submit_result(client, task_id="crash-task", agent_id="a2", content="recovered result")

        task = await get_task(client, "crash-task")
        assert task["status"] == "awaiting_retrieval"
        assert any(r["agent_id"] == "a2" for r in task["results"])


# ══════════════════════════════════════════════════════════════════════
# Scenario 4: WebSocket connect, disconnect, reconnect
# ══════════════════════════════════════════════════════════════════════

class TestWebSocketReconnect:
    """Agent connects via WS, disconnects, reconnects — verified via ping/pong."""

    @pytest.mark.asyncio
    async def test_ws_connect_and_ping(self, network):
        """Agent connects, sends ping, gets pong — proves WS is alive."""
        app = _make_network_app(network)

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/agent-ws-1") as ws:
                ws.send_text("ping")
                assert ws.receive_text() == "pong"
            # Connection closes cleanly — no exception

    @pytest.mark.asyncio
    async def test_ws_reconnect_new_connection_works(self, network):
        """Agent disconnects and reconnects — new connection works fine."""
        app = _make_network_app(network)

        with TestClient(app) as tc:
            # First connection
            with tc.websocket_connect("/ws/agent-ws-2") as ws1:
                ws1.send_text("ping")
                assert ws1.receive_text() == "pong"

            # Disconnect, then reconnect
            with tc.websocket_connect("/ws/agent-ws-2") as ws2:
                ws2.send_text("ping")
                assert ws2.receive_text() == "pong"

    @pytest.mark.asyncio
    async def test_ws_reconnect_replaces_old_connection(self, network):
        """Second WS connection for same agent_id — new one works."""
        app = _make_network_app(network)

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/agent-ws-3") as ws1:
                ws1.send_text("ping")
                assert ws1.receive_text() == "pong"
                # Open second connection for same agent
                with tc.websocket_connect("/ws/agent-ws-3") as ws2:
                    ws2.send_text("ping")
                    assert ws2.receive_text() == "pong"

    @pytest.mark.asyncio
    async def test_multiple_agents_connected(self, network):
        """Multiple agents connected simultaneously — all can communicate."""
        app = _make_network_app(network)

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/agent-A") as wsA:
                with tc.websocket_connect("/ws/agent-B") as wsB:
                    with tc.websocket_connect("/ws/agent-C") as wsC:
                        # All can ping independently
                        for ws in [wsA, wsB, wsC]:
                            ws.send_text("ping")
                            assert ws.receive_text() == "pong"


# ══════════════════════════════════════════════════════════════════════
# Scenario 5: Full lifecycle — register → task → bid → result → settle
# ══════════════════════════════════════════════════════════════════════

class TestFullLifecycleViaAPI:
    """Complete production flow through HTTP API:
    register servers + agents → fund → create task → bid → submit → select → settle.
    """

    @pytest.mark.asyncio
    async def test_end_to_end(self, network, api):
        """Uses unfunded `api` client + direct `network` ref for reputation setup."""
        c = api

        # ── 1. Register infrastructure ────────────────────────────
        srv = await register_server(c, "http://prod-srv:8000", "company-a")
        await register_agent(c, "worker-1", srv, ["coding"], name="Worker 1")
        await register_agent(c, "worker-2", srv, ["coding"], name="Worker 2")

        # Set reputation so bids pass ability check (direct access to network)
        network.reputation._scores["worker-1"] = 0.8
        network.reputation._scores["worker-2"] = 0.75

        # ── 2. Fund the task initiator ────────────────────────────
        await fund(c, "company-a", 10_000.0)

        # ── 3. Create task (2 concurrent slots → auto-collect after both submit)
        task_resp = await create_task(
            c, task_id="prod-task-1", initiator_id="company-a",
            domains=["coding"], budget=500.0,
            max_concurrent_bidders=2,
        )
        assert task_resp["status"] == "unclaimed"

        # ── 4. Both workers bid ───────────────────────────────────
        b1 = await bid(c, task_id="prod-task-1", agent_id="worker-1", price=400.0)
        b2 = await bid(c, task_id="prod-task-1", agent_id="worker-2", price=450.0)
        assert b1["status"] == "executing"
        assert b2["status"] == "executing"

        # ── 5. worker-1 submits result ────────────────────────────
        await submit_result(c, task_id="prod-task-1", agent_id="worker-1",
                            content="implementation v1")

        # ── 6. worker-2 submits result ────────────────────────────
        await submit_result(c, task_id="prod-task-1", agent_id="worker-2",
                            content="implementation v2")

        # ── 7. Both slots submitted → auto-collected ──────────────
        task = await get_task(c, "prod-task-1")
        assert task["status"] == "awaiting_retrieval"
        assert len(task["results"]) == 2

        # ── 8. Initiator selects worker-1's result ────────────────
        await select_result(c, task_id="prod-task-1", agent_id="worker-1",
                            initiator_id="company-a")

        # ── 9. Verify settlement: balance decreased by bid price ──
        bal = await c.get("/api/economy/balance", params={"agent_id": "company-a"})
        assert bal.status_code == 200
        balance = bal.json()
        # Budget was 500, bid price was 400, platform fee applies
        assert balance["available"] < 10_000.0  # money was spent

        # ── 10. Verify reputation propagated ──────────────────────
        rep = await c.get("/api/reputation/worker-1")
        assert rep.status_code == 200
        # worker-1 was selected → reputation should increase
        assert rep.json()["score"] >= 0.8


# ══════════════════════════════════════════════════════════════════════
# Scenario 6: Server unregister cascades agent removal
# ══════════════════════════════════════════════════════════════════════

class TestServerUnregisterCascade:
    """Unregistering a server removes all its agents from discovery."""

    @pytest.mark.asyncio
    async def test_cascade_cleanup(self, api):
        srv = await register_server(api, "http://temp-srv:8000", "temp-owner")
        await register_agent(api, "temp-agent-1", srv, ["coding"])
        await register_agent(api, "temp-agent-2", srv, ["coding"])

        # Both discoverable
        agents = await discover(api, "coding")
        assert "temp-agent-1" in agents
        assert "temp-agent-2" in agents

        # Unregister server
        resp = await api.delete(f"/api/discovery/servers/{srv}")
        assert resp.status_code == 200

        # Agents gone from discovery
        agents = await discover(api, "coding")
        assert "temp-agent-1" not in agents
        assert "temp-agent-2" not in agents

        # Agent cards gone
        resp = await api.get("/api/discovery/agents/temp-agent-1")
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# Scenario 7: Multiple concurrent tasks with budget contention
# ══════════════════════════════════════════════════════════════════════

class TestBudgetContention:
    """User creates multiple tasks — budget freezes accumulate correctly."""

    @pytest.mark.asyncio
    async def test_multiple_tasks_freeze_budget(self, client):
        # user1 has 10,000 credits (from funded_network)
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        initial = bal.json()["available"]

        # Create 3 tasks, each freezing 2000
        for i in range(3):
            await create_task(client, task_id=f"multi-{i}", budget=2000.0)

        # 6000 frozen, 4000 available
        bal = await client.get("/api/economy/balance", params={"agent_id": "user1"})
        assert bal.json()["frozen"] == 6000.0
        assert bal.json()["available"] == initial - 6000.0

    @pytest.mark.asyncio
    async def test_insufficient_budget_rejected(self, client):
        """Creating a task that exceeds available balance fails."""
        # user2 has 5,000 credits
        resp = await client.post("/api/tasks", json={
            "task_id": "too-expensive",
            "initiator_id": "user2",
            "content": {"desc": "expensive task"},
            "domains": ["coding"],
            "budget": 999_999.0,
        })
        assert resp.status_code == 402  # insufficient funds


# ══════════════════════════════════════════════════════════════════════
# Scenario 8: Agent updates domains, re-discoverable in new domain
# ══════════════════════════════════════════════════════════════════════

class TestAgentDomainUpdate:
    """Agent switches from coding to design — old domain revoked, new announced."""

    @pytest.mark.asyncio
    async def test_update_domains(self, api):
        srv = await register_server(api, "http://flex-srv:8000", "flex")
        await register_agent(api, "flex-agent", srv, ["coding"])

        assert "flex-agent" in await discover(api, "coding")

        # Update to design only
        resp = await api.put("/api/discovery/agents/flex-agent", json={
            "domains": ["design"],
        })
        assert resp.status_code == 200

        # No longer in coding, now in design
        assert "flex-agent" not in await discover(api, "coding")
        assert "flex-agent" in await discover(api, "design")


# ══════════════════════════════════════════════════════════════════════
# Scenario 9: Task with subtask delegation
# ══════════════════════════════════════════════════════════════════════

class TestSubtaskDelegation:
    """Executing agent creates subtask from parent budget."""

    @pytest.mark.asyncio
    async def test_executor_delegates_subtask(self, client):
        # Create parent task
        await create_task(client, task_id="parent-1", budget=1000.0)

        # a1 bids and is executing
        b = await bid(client, task_id="parent-1", agent_id="a1", price=800.0)
        assert b["status"] == "executing"

        # a1 creates subtask from parent budget
        resp = await client.post("/api/tasks/parent-1/subtask", json={
            "initiator_id": "a1",
            "content": {"desc": "sub-work"},
            "domains": ["design"],
            "budget": 200.0,
        })
        assert resp.status_code == 201
        sub = resp.json()
        assert sub["budget"] == 200.0
        assert sub["status"] == "unclaimed"

        # Verify parent task knows about child
        parent = await get_task(client, "parent-1")
        assert sub["id"] in parent["child_ids"]


# ══════════════════════════════════════════════════════════════════════
# Scenario 10: Task deadline expires, timeout settlement
# ══════════════════════════════════════════════════════════════════════

class TestTaskDeadlineExpiry:
    """Task with deadline expires → NO_ONE_ABLE + budget refunded."""

    @pytest.mark.asyncio
    async def test_expired_task_refunds_budget(self, client):
        bal_before = (await client.get(
            "/api/economy/balance", params={"agent_id": "user1"}
        )).json()["available"]

        await create_task(
            client, task_id="expiring-task", budget=500.0,
            deadline="2020-01-01T00:00:00Z",  # already past
        )

        # Trigger deadline scan
        resp = await client.post("/api/admin/scan-deadlines")
        assert resp.status_code == 200
        assert "expiring-task" in resp.json()["expired"]

        # Task closed
        task = await get_task(client, "expiring-task")
        assert task["status"] == "no_one_able"

        # Budget refunded
        bal_after = (await client.get(
            "/api/economy/balance", params={"agent_id": "user1"}
        )).json()["available"]
        assert bal_after == bal_before  # fully refunded
