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
        """Agent submits result → slot opens → waiting agent promoted.
        Uses max_concurrent_bidders=2 so auto_collect doesn't fire after 1 result."""
        await create_task(client, task_id="submit-promo", budget=500.0, max_concurrent_bidders=2)

        await bid(client, task_id="submit-promo", agent_id="a1", price=80.0)
        await bid(client, task_id="submit-promo", agent_id="a2", price=85.0)
        b3 = await bid(client, task_id="submit-promo", agent_id="a3", price=75.0)
        assert b3["status"] == "waiting"

        # a1 submits → a3 gets promoted
        await submit_result(client, task_id="submit-promo", agent_id="a1", content="done")

        task = await get_task(client, "submit-promo")
        bid_map = {b["agent_id"]: b["status"] for b in task["bids"]}
        assert bid_map["a3"] == "executing"


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

@pytest.mark.skip(reason="WebSocket removed in queue-only architecture")
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


# ══════════════════════════════════════════════════════════════════════
# Scenario 11: Invite agent (skip ability check)
# ══════════════════════════════════════════════════════════════════════

class TestInviteAgent:
    """Initiator invites a low-reputation agent — skips ability gate."""

    @pytest.mark.asyncio
    async def test_invited_agent_bypasses_ability_check(self, client):
        """a5 has low reputation (0.6), normally rejected.
        But if invited, the ability check is skipped."""
        # Create task
        await create_task(client, task_id="invite-task", budget=200.0)

        # Invite a5
        resp = await client.post("/api/tasks/invite-task/invite", json={
            "initiator_id": "user1", "agent_id": "a5",
        })
        assert resp.status_code == 200

        # a5 bids with low confidence — would fail ability check normally
        b = await bid(client, task_id="invite-task", agent_id="a5",
                      confidence=0.3, price=80.0)
        assert b["status"] == "executing"

    @pytest.mark.asyncio
    async def test_only_initiator_can_invite(self, client):
        await create_task(client, task_id="inv-perm", budget=200.0)

        resp = await client.post("/api/tasks/inv-perm/invite", json={
            "initiator_id": "user2",  # not the initiator
            "agent_id": "a3",
        })
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# Scenario 12: Discussions during bidding phase
# ══════════════════════════════════════════════════════════════════════

class TestDiscussions:
    """Initiator posts discussion updates during bidding phase."""

    @pytest.mark.asyncio
    async def test_discussion_during_bidding(self, client):
        await create_task(client, task_id="discuss-task", budget=200.0)
        await bid(client, task_id="discuss-task", agent_id="a1", price=80.0)

        # Post discussion
        resp = await client.post("/api/tasks/discuss-task/discussions", json={
            "initiator_id": "user1",
            "message": "Please focus on performance optimization",
        })
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"] == "bidding"

        # Post second message
        resp = await client.post("/api/tasks/discuss-task/discussions", json={
            "initiator_id": "user1",
            "message": "Also add unit tests",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_post_discussion(self, client):
        await create_task(client, task_id="discuss-perm", budget=200.0)
        await bid(client, task_id="discuss-perm", agent_id="a1", price=80.0)

        resp = await client.post("/api/tasks/discuss-perm/discussions", json={
            "initiator_id": "user2",
            "message": "hacking the discussion",
        })
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# Scenario 13: Deposit and balance management
# ══════════════════════════════════════════════════════════════════════

class TestDepositFlow:
    """Deposit funds, verify balance, then spend on tasks."""

    @pytest.mark.asyncio
    async def test_deposit_increases_balance(self, client):
        # user2 starts with 5000
        bal = (await client.get(
            "/api/economy/balance", params={"agent_id": "user2"}
        )).json()
        assert bal["available"] == 5000.0

        # Deposit more
        resp = await client.post("/api/economy/deposit", json={
            "agent_id": "user2", "amount": 3000.0,
        })
        assert resp.status_code == 200
        assert resp.json()["available"] == 8000.0

        # Verify via balance endpoint
        bal = (await client.get(
            "/api/economy/balance", params={"agent_id": "user2"}
        )).json()
        assert bal["available"] == 8000.0

    @pytest.mark.asyncio
    async def test_deposit_then_create_task(self, client):
        """New user deposits funds, then creates a task."""
        # Deposit to fresh account
        resp = await client.post("/api/economy/deposit", json={
            "agent_id": "new-user", "amount": 500.0,
        })
        assert resp.status_code == 200
        assert resp.json()["available"] == 500.0

        # Create task with newly deposited funds
        resp = await client.post("/api/tasks", json={
            "task_id": "fresh-task",
            "initiator_id": "new-user",
            "content": {"desc": "first task"},
            "domains": ["coding"],
            "budget": 200.0,
        })
        assert resp.status_code == 201

        # Balance: 500 - 200 frozen = 300 available
        bal = (await client.get(
            "/api/economy/balance", params={"agent_id": "new-user"}
        )).json()
        assert bal["available"] == 300.0
        assert bal["frozen"] == 200.0


# ══════════════════════════════════════════════════════════════════════
# Scenario 14: Reputation events from servers
# ══════════════════════════════════════════════════════════════════════

class TestReputationEvents:
    """Servers send reputation events to the network; scores aggregate."""

    @pytest.mark.asyncio
    async def test_positive_event_increases_score(self, client):
        initial = (await client.get("/api/reputation/a1")).json()["score"]

        resp = await client.post("/api/reputation/events", json={
            "agent_id": "a1",
            "event_type": "task_success",
            "server_id": "srv-1",
        })
        assert resp.status_code == 200
        new_score = resp.json()["score"]
        assert new_score >= initial

    @pytest.mark.asyncio
    async def test_negative_event_decreases_score(self, client):
        initial = (await client.get("/api/reputation/a2")).json()["score"]

        resp = await client.post("/api/reputation/events", json={
            "agent_id": "a2",
            "event_type": "task_failure",
            "server_id": "srv-1",
        })
        assert resp.status_code == 200
        new_score = resp.json()["score"]
        assert new_score <= initial

    @pytest.mark.asyncio
    async def test_burst_detection_blocks_spam(self, client):
        """Rapid identical events get blocked by anomaly detection."""
        scores = []
        for i in range(15):
            resp = await client.post("/api/reputation/events", json={
                "agent_id": "a3",
                "event_type": "task_success",
                "server_id": "srv-spam",
            })
            scores.append(resp.json()["score"])

        # Score should plateau — burst detection kicks in
        # Not every event should increase the score
        unique_scores = len(set(scores))
        assert unique_scores < 15  # some events were blocked


# ══════════════════════════════════════════════════════════════════════
# Scenario 15: Open tasks listing and filtering
# ══════════════════════════════════════════════════════════════════════

class TestOpenTasksListing:
    """List open tasks, filter by domain, verify status transitions."""

    @pytest.mark.asyncio
    async def test_list_open_tasks(self, client):
        await create_task(client, task_id="open-1", budget=100.0, domains=["coding"])
        await create_task(client, task_id="open-2", budget=200.0, domains=["design"])
        await create_task(client, task_id="open-3", budget=300.0, domains=["coding"])

        resp = await client.get("/api/tasks/open")
        assert resp.status_code == 200
        tasks = resp.json()
        task_ids = [t["id"] for t in tasks]
        assert "open-1" in task_ids
        assert "open-2" in task_ids
        assert "open-3" in task_ids

    @pytest.mark.asyncio
    async def test_filter_open_tasks_by_domain(self, client):
        await create_task(client, task_id="filter-c", budget=100.0, domains=["coding"])
        await create_task(client, task_id="filter-d", budget=100.0, domains=["design"])

        resp = await client.get("/api/tasks/open", params={"domains": "design"})
        assert resp.status_code == 200
        task_ids = [t["id"] for t in resp.json()]
        assert "filter-d" in task_ids
        assert "filter-c" not in task_ids

    @pytest.mark.asyncio
    async def test_completed_task_not_in_open_list(self, client):
        """Task that is closed/completed should not appear in open tasks."""
        await create_task(client, task_id="close-me", budget=100.0,
                          max_concurrent_bidders=1)
        await bid(client, task_id="close-me", agent_id="a1", price=80.0)
        await submit_result(client, task_id="close-me", agent_id="a1", content="done")

        # Close the task
        resp = await client.post("/api/tasks/close-me/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 200

        resp = await client.get("/api/tasks/open")
        task_ids = [t["id"] for t in resp.json()]
        assert "close-me" not in task_ids


# ══════════════════════════════════════════════════════════════════════
# Scenario 16: Task status query (initiator-only view)
# ══════════════════════════════════════════════════════════════════════

class TestTaskStatusQuery:
    """Only the task initiator can query task status."""

    @pytest.mark.asyncio
    async def test_initiator_can_query_status(self, client):
        await create_task(client, task_id="status-q", budget=100.0)
        await bid(client, task_id="status-q", agent_id="a1", price=80.0)

        resp = await client.get("/api/tasks/status-q/status",
                                params={"agent_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "bidding"
        assert len(data["bids"]) == 1
        assert data["bids"][0]["agent_id"] == "a1"

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_query_status(self, client):
        await create_task(client, task_id="status-deny", budget=100.0)

        resp = await client.get("/api/tasks/status-deny/status",
                                params={"agent_id": "user2"})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════
# Scenario 17: Audit log trail
# ══════════════════════════════════════════════════════════════════════

class TestAuditLogs:
    """All operations leave audit trail in the global logger."""

    @pytest.mark.asyncio
    async def test_task_lifecycle_logged(self, client):
        await create_task(client, task_id="log-task", budget=200.0,
                          max_concurrent_bidders=1)
        await bid(client, task_id="log-task", agent_id="a1", price=80.0)
        await submit_result(client, task_id="log-task", agent_id="a1", content="x")

        # Query logs for this task
        resp = await client.get("/api/admin/logs", params={"task_id": "log-task"})
        assert resp.status_code == 200
        logs = resp.json()
        fn_names = [e["fn_name"] for e in logs]
        assert "create_task" in fn_names
        assert "submit_bid" in fn_names
        assert "submit_result" in fn_names

    @pytest.mark.asyncio
    async def test_logs_filtered_by_agent(self, client):
        await create_task(client, task_id="log-agent", budget=200.0)
        await bid(client, task_id="log-agent", agent_id="a1", price=80.0)
        await bid(client, task_id="log-agent", agent_id="a2", price=85.0)

        resp = await client.get("/api/admin/logs",
                                params={"agent_id": "a2"})
        assert resp.status_code == 200
        logs = resp.json()
        # All returned logs should mention a2
        for entry in logs:
            assert entry["agent_id"] == "a2"


# ══════════════════════════════════════════════════════════════════════
# Scenario 18: Deadline update mid-task
# ══════════════════════════════════════════════════════════════════════

class TestDeadlineUpdate:
    """Initiator extends deadline while agents are working."""

    @pytest.mark.asyncio
    async def test_extend_deadline(self, client):
        await create_task(client, task_id="deadline-ext", budget=200.0,
                          deadline="2030-01-01T00:00:00Z")

        resp = await client.put("/api/tasks/deadline-ext/deadline", json={
            "initiator_id": "user1",
            "deadline": "2035-06-15T00:00:00Z",
        })
        assert resp.status_code == 200
        assert resp.json()["deadline"] == "2035-06-15T00:00:00Z"

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_update_deadline(self, client):
        await create_task(client, task_id="deadline-perm", budget=200.0)

        resp = await client.put("/api/tasks/deadline-perm/deadline", json={
            "initiator_id": "user2",
            "deadline": "2099-01-01T00:00:00Z",
        })
        assert resp.status_code == 400


# ══════════════════════════════════════════════════════════════════════
# Scenario 19: Agent unregister (individual removal)
# ══════════════════════════════════════════════════════════════════════

class TestAgentUnregister:
    """Agent leaves the network — removed from DHT and bootstrap."""

    @pytest.mark.asyncio
    async def test_unregister_agent(self, api):
        srv = await register_server(api, "http://unreg-srv:8000", "eve")
        await register_agent(api, "leaving-agent", srv, ["coding"])

        assert "leaving-agent" in await discover(api, "coding")

        resp = await api.delete("/api/discovery/agents/leaving-agent")
        assert resp.status_code == 200

        assert "leaving-agent" not in await discover(api, "coding")

        resp = await api.get("/api/discovery/agents/leaving-agent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_unregister_one_agent_does_not_affect_others(self, api):
        srv = await register_server(api, "http://co-srv:8000", "frank")
        await register_agent(api, "stay-agent", srv, ["coding"])
        await register_agent(api, "go-agent", srv, ["coding"])

        await api.delete("/api/discovery/agents/go-agent")

        agents = await discover(api, "coding")
        assert "stay-agent" in agents
        assert "go-agent" not in agents


# ══════════════════════════════════════════════════════════════════════
# Scenario 20: Direct messaging between agents
# ══════════════════════════════════════════════════════════════════════

class TestDirectMessaging:
    """Relay a message between two agents via the network."""

    @pytest.mark.asyncio
    async def test_message_to_disconnected_agent(self, network, api):
        """Message to an agent not connected via WS → undeliverable."""
        resp = await api.post("/api/messages", json={
            "to": {"agent_id": "offline-agent"},
            "from": {"agent_id": "sender-agent"},
            "content": "hello from sender",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["method"] == "undeliverable"


# ══════════════════════════════════════════════════════════════════════
# Scenario 21: Same agent bids on multiple tasks concurrently
# ══════════════════════════════════════════════════════════════════════

class TestAgentMultiTasking:
    """One agent works on multiple tasks at the same time."""

    @pytest.mark.asyncio
    async def test_agent_bids_on_multiple_tasks(self, client):
        await create_task(client, task_id="multi-a", budget=200.0)
        await create_task(client, task_id="multi-b", budget=200.0)
        await create_task(client, task_id="multi-c", budget=200.0)

        # a1 bids on all three
        for tid in ["multi-a", "multi-b", "multi-c"]:
            b = await bid(client, task_id=tid, agent_id="a1", price=80.0)
            assert b["status"] == "executing"

        # a1 submits results on all three
        for tid in ["multi-a", "multi-b", "multi-c"]:
            await submit_result(client, task_id=tid, agent_id="a1", content=f"result-{tid}")

        # Verify all tasks have results from a1
        for tid in ["multi-a", "multi-b", "multi-c"]:
            task = await get_task(client, tid)
            assert any(r["agent_id"] == "a1" for r in task["results"])


# ══════════════════════════════════════════════════════════════════════
# Scenario 22: Budget confirmation flow (over-budget bid)
# ══════════════════════════════════════════════════════════════════════

class TestBudgetConfirmation:
    """Agent bids above budget → pending → initiator approves/rejects."""

    @pytest.mark.asyncio
    async def test_over_budget_bid_needs_confirmation(self, client):
        await create_task(client, task_id="overbudget", budget=100.0)

        # Bid well above budget
        resp = await client.post("/api/tasks/overbudget/bid", json={
            "agent_id": "a1", "confidence": 0.95, "price": 500.0,
        })
        assert resp.status_code == 200
        status = resp.json()["status"]
        # Should be pending (needs budget confirmation) or rejected
        assert status in ("pending", "rejected")

    @pytest.mark.asyncio
    async def test_initiator_rejects_over_budget(self, client):
        await create_task(client, task_id="reject-over", budget=100.0)

        # Try over-budget bid
        await client.post("/api/tasks/reject-over/bid", json={
            "agent_id": "a1", "confidence": 0.95, "price": 500.0,
        })

        # Initiator rejects
        resp = await client.post("/api/tasks/reject-over/confirm-budget", json={
            "initiator_id": "user1",
            "approved": False,
        })
        assert resp.status_code == 200

        # All pending bids should now be rejected
        task = await get_task(client, "reject-over")
        for b in task["bids"]:
            if b["agent_id"] == "a1":
                assert b["status"] == "rejected"


# ══════════════════════════════════════════════════════════════════════
# Scenario 23: Collect results + adjudication data
# ══════════════════════════════════════════════════════════════════════

class TestCollectResults:
    """Initiator collects results and transitions task to completed."""

    @pytest.mark.asyncio
    async def test_collect_transitions_to_completed(self, client):
        await create_task(client, task_id="collect-t", budget=200.0,
                          max_concurrent_bidders=1)
        await bid(client, task_id="collect-t", agent_id="a1", price=80.0)
        await submit_result(client, task_id="collect-t", agent_id="a1",
                            content="final answer")

        # Task should be awaiting_retrieval
        task = await get_task(client, "collect-t")
        assert task["status"] == "awaiting_retrieval"

        # Collect results
        resp = await client.get("/api/tasks/collect-t/results",
                                params={"initiator_id": "user1"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["agent_id"] == "a1"

        # Task now completed
        task = await get_task(client, "collect-t")
        assert task["status"] == "completed"

    @pytest.mark.asyncio
    async def test_non_initiator_cannot_collect(self, client):
        await create_task(client, task_id="collect-deny", budget=200.0,
                          max_concurrent_bidders=1)
        await bid(client, task_id="collect-deny", agent_id="a1", price=80.0)
        await submit_result(client, task_id="collect-deny", agent_id="a1", content="x")

        resp = await client.get("/api/tasks/collect-deny/results",
                                params={"initiator_id": "user2"})
        assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════
# Scenario 24: Close task with no results → NO_ONE_ABLE
# ══════════════════════════════════════════════════════════════════════

class TestCloseTaskNoResults:
    """Initiator closes a task nobody bid on → budget refunded."""

    @pytest.mark.asyncio
    async def test_close_unclaimed_task_refunds(self, client):
        bal_before = (await client.get(
            "/api/economy/balance", params={"agent_id": "user1"}
        )).json()["available"]

        await create_task(client, task_id="ghost-task", budget=300.0)

        # Nobody bids → initiator closes
        resp = await client.post("/api/tasks/ghost-task/close", json={
            "initiator_id": "user1",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_one_able"

        # Budget refunded
        bal_after = (await client.get(
            "/api/economy/balance", params={"agent_id": "user1"}
        )).json()["available"]
        assert bal_after == bal_before


# ══════════════════════════════════════════════════════════════════════
# Scenario 25: Config hot-reload
# ══════════════════════════════════════════════════════════════════════

class TestConfigHotReload:
    """Admin updates config at runtime — modules pick up new values."""

    @pytest.mark.asyncio
    async def test_read_config(self, client):
        resp = await client.get("/api/admin/config")
        assert resp.status_code == 200
        config = resp.json()
        assert "reputation" in config
        assert "economy" in config
        assert "matcher" in config

    @pytest.mark.asyncio
    async def test_update_platform_fee(self, client):
        resp = await client.put("/api/admin/config", json={
            "economy": {"platform_fee_rate": 0.05},
        })
        assert resp.status_code == 200
        assert resp.json()["economy"]["platform_fee_rate"] == 0.05

        # Verify persisted
        resp = await client.get("/api/admin/config")
        assert resp.json()["economy"]["platform_fee_rate"] == 0.05


# ══════════════════════════════════════════════════════════════════════
# Scenario 26: List agents by server
# ══════════════════════════════════════════════════════════════════════

class TestListAgentsByServer:
    """List all agents belonging to a specific server."""

    @pytest.mark.asyncio
    async def test_list_agents_by_server(self, api):
        srv = await register_server(api, "http://list-srv:8000", "greg")
        await register_agent(api, "list-a1", srv, ["coding"])
        await register_agent(api, "list-a2", srv, ["design"])
        await register_agent(api, "list-a3", srv, ["coding"])

        resp = await api.get("/api/discovery/agents",
                             params={"server_id": srv})
        assert resp.status_code == 200
        agents = resp.json()
        agent_ids = [a["agent_id"] for a in agents]
        assert len(agent_ids) == 3
        assert set(agent_ids) == {"list-a1", "list-a2", "list-a3"}

    @pytest.mark.asyncio
    async def test_list_agents_by_domain(self, api):
        srv = await register_server(api, "http://dom-srv:8000", "helen")
        await register_agent(api, "dom-c1", srv, ["coding"])
        await register_agent(api, "dom-c2", srv, ["coding"])
        await register_agent(api, "dom-d1", srv, ["design"])

        resp = await api.get("/api/discovery/agents",
                             params={"domain": "coding"})
        assert resp.status_code == 200
        agent_ids = [a["agent_id"] for a in resp.json()]
        assert "dom-c1" in agent_ids
        assert "dom-c2" in agent_ids
        assert "dom-d1" not in agent_ids


# ══════════════════════════════════════════════════════════════════════
# Scenario 27: Cluster status endpoint
# ══════════════════════════════════════════════════════════════════════

class TestClusterStatus:
    """Verify cluster status endpoint reports correct state."""

    @pytest.mark.asyncio
    async def test_cluster_status_standalone(self, client):
        resp = await client.get("/api/cluster/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "standalone"
        assert "local" in data
        assert data["local"]["status"] == "online"
