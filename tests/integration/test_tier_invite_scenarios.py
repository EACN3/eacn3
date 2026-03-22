"""Integration tests: multi-role tier/invite scenarios through plugin -> network.

Tests multi-actor interactions involving agent tiers, task levels,
invitation-based bypass of bid admission, and full marketplace simulations.
Each test exercises the FULL stack: MCP plugin subprocess -> HTTP -> Network.
"""

import pytest

from tests.integration.conftest import is_error, seed_reputation


# ── Helpers ──────────────────────────────────────────────────────────


async def _register_agent(
    mcp,
    funded_network,
    agent_id: str,
    name: str,
    *,
    tier: str = "general",
    agent_type: str = "executor",
    domains: list[str] | None = None,
    reputation: float = 0.8,
    balance: float = 5000.0,
):
    """Register an agent with given tier/type and seed its reputation + balance."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": name,
        "description": f"{name} agent ({tier} tier)",
        "domains": domains or ["coding"],
        "skills": [{"name": "work", "description": "does work"}],
        "agent_id": agent_id,
        "agent_type": agent_type,
        "tier": tier,
    })
    funded_network.reputation._scores[agent_id] = reputation
    funded_network.escrow.get_or_create_account(agent_id, balance)


async def _create_task(
    mcp,
    initiator_id: str,
    description: str,
    *,
    level: str = "general",
    domains: list[str] | None = None,
    budget: float = 100.0,
    invited_agent_ids: list[str] | None = None,
) -> str:
    """Create a task and return its task_id."""
    params = {
        "description": description,
        "budget": budget,
        "domains": domains or ["coding"],
        "initiator_id": initiator_id,
        "level": level,
    }
    if invited_agent_ids is not None:
        params["invited_agent_ids"] = invited_agent_ids
    task = await mcp.call_tool_parsed("eacn3_create_task", params)
    assert "task_id" in task, f"Task creation failed: {task}"
    return task["task_id"]


async def _bid(mcp, task_id: str, agent_id: str, confidence: float = 0.9, price: float = 80.0):
    """Submit a bid and return the parsed result."""
    return await mcp.call_tool_parsed("eacn3_submit_bid", {
        "task_id": task_id,
        "agent_id": agent_id,
        "confidence": confidence,
        "price": price,
    })


# ═════════════════════════════════════════════════════════════════════
# Scenario 1: Tool-tier agent correctly restricted
# ═════════════════════════════════════════════════════════════════════


class TestToolTierRestriction:
    """Tool-tier agents can ONLY bid on tool-level tasks."""

    @pytest.mark.asyncio
    async def test_tool_tier_rejected_on_general_task(self, mcp, http, funded_network):
        """ToolBot (tool tier) cannot bid on a general-level task."""
        await _register_agent(mcp, funded_network, "alice", "Alice", tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "toolbot", "ToolBot", tier="tool")

        # Alice creates a general-level task
        task_id = await _create_task(mcp, "alice", "General task for humans")

        # ToolBot bids on general task -> REJECTED
        bid = await _bid(mcp, task_id, "toolbot")
        assert bid["status"] == "rejected", f"Tool-tier bid should be rejected on general task: {bid}"

        # Verify rejection via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        tool_bids = [b for b in task_data["bids"] if b["agent_id"] == "toolbot"]
        assert len(tool_bids) == 1
        assert tool_bids[0]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_tool_tier_accepted_on_tool_task(self, mcp, http, funded_network):
        """ToolBot (tool tier) CAN bid on a tool-level task."""
        await _register_agent(mcp, funded_network, "alice2", "Alice", tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "toolbot2", "ToolBot", tier="tool")

        # Alice creates a tool-level task
        task_id = await _create_task(mcp, "alice2", "Simple tool task", level="tool")

        # ToolBot bids -> ACCEPTED
        bid = await _bid(mcp, task_id, "toolbot2")
        assert bid["status"] in ("accepted", "executing", "waiting"), (
            f"Tool-tier bid should be accepted on tool task: {bid}"
        )

    @pytest.mark.asyncio
    async def test_tool_full_lifecycle(self, mcp, http, funded_network):
        """ToolBot bids on tool task, submits result, Alice collects."""
        await _register_agent(mcp, funded_network, "alice3", "Alice", tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "toolbot3", "ToolBot", tier="tool")

        task_id = await _create_task(mcp, "alice3", "Run formatting tool", level="tool")

        # ToolBot bids and gets accepted
        bid = await _bid(mcp, task_id, "toolbot3")
        assert bid["status"] in ("accepted", "executing", "waiting")

        # ToolBot submits result
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "toolbot3",
            "content": {"formatted": True, "output": "done"},
        })

        # Alice closes and collects
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "alice3",
        })

        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "alice3",
        })
        assert len(results["results"]) >= 1
        assert results["results"][0]["agent_id"] == "toolbot3"

        # Alice selects the result
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id,
            "agent_id": "toolbot3",
            "initiator_id": "alice3",
        })

        # Verify via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        final = resp.json()
        selected = [r for r in final["results"] if r.get("selected")]
        assert len(selected) == 1
        assert selected[0]["agent_id"] == "toolbot3"


# ═════════════════════════════════════════════════════════════════════
# Scenario 2: Expert hierarchy works
# ═════════════════════════════════════════════════════════════════════


class TestExpertHierarchy:
    """Tier hierarchy: general > expert > expert_general > tool."""

    @pytest.mark.asyncio
    async def test_expert_task_tier_filtering(self, mcp, http, funded_network):
        """General and expert can bid on expert tasks; tool cannot."""
        await _register_agent(mcp, funded_network, "gen-agent", "GeneralAgent", tier="general")
        await _register_agent(mcp, funded_network, "exp-agent", "ExpertAgent", tier="expert")
        await _register_agent(mcp, funded_network, "tool-agent", "ToolAgent", tier="tool")
        # Need a planner to create the task
        await _register_agent(mcp, funded_network, "coordinator-s2", "Coordinator",
                              tier="general", agent_type="planner")

        # Create expert-level task
        task_id = await _create_task(mcp, "coordinator-s2", "Expert-level analysis", level="expert")

        # General-agent bids on expert task -> ACCEPTED (general >= expert)
        bid_gen = await _bid(mcp, task_id, "gen-agent")
        assert bid_gen["status"] in ("accepted", "executing", "waiting"), (
            f"General tier should be accepted on expert task: {bid_gen}"
        )

        # Expert-agent bids on expert task -> ACCEPTED (expert == expert)
        bid_exp = await _bid(mcp, task_id, "exp-agent")
        assert bid_exp["status"] in ("accepted", "executing", "waiting"), (
            f"Expert tier should be accepted on expert task: {bid_exp}"
        )

        # Tool-agent bids on expert task -> REJECTED (tool < expert)
        bid_tool = await _bid(mcp, task_id, "tool-agent")
        assert bid_tool["status"] == "rejected", (
            f"Tool tier should be rejected on expert task: {bid_tool}"
        )

        # Verify all bids exist in task state via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        bids_by_agent = {b["agent_id"]: b["status"] for b in task_data["bids"]}
        assert bids_by_agent["gen-agent"] in ("waiting_execution", "executing")
        assert bids_by_agent["exp-agent"] in ("waiting_execution", "executing")
        assert bids_by_agent["tool-agent"] == "rejected"


# ═════════════════════════════════════════════════════════════════════
# Scenario 3: Publisher invites low-reputation agent
# ═════════════════════════════════════════════════════════════════════


class TestInviteLowReputationAgent:
    """Invitation bypasses the confidence x reputation ability gate."""

    @pytest.mark.asyncio
    async def test_low_rep_rejected_then_invited_accepted(self, mcp, http, funded_network):
        """NewbieAgent (rep=0.1) rejected without invite, accepted after invite."""
        await _register_agent(mcp, funded_network, "publisher", "Publisher",
                              tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "newbie", "NewbieAgent",
                              tier="general", reputation=0.1)

        # Publisher creates task WITHOUT inviting newbie
        task_id = await _create_task(mcp, "publisher", "Task needing invitation")

        # NewbieAgent bids with confidence=0.9 -> ability = 0.9 * 0.1 = 0.09 < 0.5 threshold
        bid1 = await _bid(mcp, task_id, "newbie", confidence=0.9)
        assert bid1["status"] == "rejected", (
            f"Low-rep agent should be rejected without invite: {bid1}"
        )

        # Verify rejection via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        newbie_bids = [b for b in task_data["bids"] if b["agent_id"] == "newbie"]
        assert len(newbie_bids) == 1
        assert newbie_bids[0]["status"] == "rejected"

        # Publisher invites NewbieAgent
        invite_result = await mcp.call_tool_parsed("eacn3_invite_agent", {
            "task_id": task_id,
            "agent_id": "newbie",
            "initiator_id": "publisher",
            "message": "I trust you, please bid!",
        })
        assert not is_error(invite_result), f"Invite failed: {invite_result}"

        # Verify invited_agent_ids updated on network
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        invited = task_data.get("invited_agent_ids", [])
        assert "newbie" in invited, f"newbie should be in invited list: {invited}"

        # The previous bid was already rejected and recorded, so we need a fresh
        # task to re-bid (network rejects duplicate bids from same agent).
        # Create a second task and invite upfront.
        task_id2 = await _create_task(mcp, "publisher", "Second task for newbie")
        await mcp.call_tool_parsed("eacn3_invite_agent", {
            "task_id": task_id2,
            "agent_id": "newbie",
            "initiator_id": "publisher",
        })

        # Now NewbieAgent bids on the second task -> ACCEPTED (invited bypasses ability)
        bid2 = await _bid(mcp, task_id2, "newbie", confidence=0.9)
        assert bid2["status"] in ("accepted", "executing", "waiting"), (
            f"Invited low-rep agent should be accepted: {bid2}"
        )


# ═════════════════════════════════════════════════════════════════════
# Scenario 4: Pre-set invited_agent_ids at creation
# ═════════════════════════════════════════════════════════════════════


class TestPresetInvitedAgents:
    """invited_agent_ids set at task creation bypasses admission for listed agents."""

    @pytest.mark.asyncio
    async def test_preinvited_agent_accepted_immediately(self, mcp, http, funded_network):
        """SpecialistAgent (rep=0.1) accepted immediately when pre-invited."""
        await _register_agent(mcp, funded_network, "pub-s4", "Publisher",
                              tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "specialist", "SpecialistAgent",
                              tier="general", reputation=0.1)

        # Publisher creates task with invited_agent_ids including specialist
        task_id = await _create_task(
            mcp, "pub-s4", "Pre-invited specialist task",
            invited_agent_ids=["specialist"],
        )

        # Verify invited_agent_ids via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        assert "specialist" in task_data.get("invited_agent_ids", [])

        # SpecialistAgent bids -> ACCEPTED immediately (pre-invited)
        bid = await _bid(mcp, task_id, "specialist", confidence=0.9)
        assert bid["status"] in ("accepted", "executing", "waiting"), (
            f"Pre-invited agent should be accepted immediately: {bid}"
        )

        # Verify bid status via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        bids_by_agent = {b["agent_id"]: b["status"] for b in resp.json()["bids"]}
        assert bids_by_agent["specialist"] in ("waiting_execution", "executing")

    @pytest.mark.asyncio
    async def test_non_invited_low_rep_still_rejected(self, mcp, http, funded_network):
        """Agent NOT in invited_agent_ids with low rep is still rejected."""
        await _register_agent(mcp, funded_network, "pub-s4b", "Publisher",
                              tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "specialist-b", "SpecialistB",
                              tier="general", reputation=0.1)
        await _register_agent(mcp, funded_network, "outsider", "Outsider",
                              tier="general", reputation=0.1)

        # Only specialist-b is invited
        task_id = await _create_task(
            mcp, "pub-s4b", "Selective invite task",
            invited_agent_ids=["specialist-b"],
        )

        # Outsider bids -> REJECTED (not invited, low rep)
        bid_out = await _bid(mcp, task_id, "outsider", confidence=0.9)
        assert bid_out["status"] == "rejected", (
            f"Non-invited low-rep agent should be rejected: {bid_out}"
        )

        # specialist-b bids -> ACCEPTED (invited)
        bid_spec = await _bid(mcp, task_id, "specialist-b", confidence=0.9)
        assert bid_spec["status"] in ("accepted", "executing", "waiting"), (
            f"Pre-invited agent should be accepted: {bid_spec}"
        )


# ═════════════════════════════════════════════════════════════════════
# Scenario 5: Invite bypasses tier restriction
# ═════════════════════════════════════════════════════════════════════


class TestInviteBypassesTier:
    """Invitation overrides tier restriction — tool agent can bid on expert task when invited."""

    @pytest.mark.asyncio
    async def test_tool_rejected_then_invited_on_expert_task(self, mcp, http, funded_network):
        """ToolBot rejected on expert task, then accepted after invitation."""
        await _register_agent(mcp, funded_network, "pub-s5", "Publisher",
                              tier="general", agent_type="planner")
        await _register_agent(mcp, funded_network, "toolbot-s5", "ToolBot", tier="tool")

        # Publisher creates expert-level task
        task_id = await _create_task(mcp, "pub-s5", "Expert task for invited tool", level="expert")

        # ToolBot bids on expert task -> REJECTED (tool cannot bid on expert)
        bid1 = await _bid(mcp, task_id, "toolbot-s5")
        assert bid1["status"] == "rejected", (
            f"Tool-tier bid on expert task should be rejected: {bid1}"
        )

        # Verify rejection via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        tool_bids = [b for b in resp.json()["bids"] if b["agent_id"] == "toolbot-s5"]
        assert tool_bids[0]["status"] == "rejected"

        # Publisher invites ToolBot
        invite_result = await mcp.call_tool_parsed("eacn3_invite_agent", {
            "task_id": task_id,
            "agent_id": "toolbot-s5",
            "initiator_id": "pub-s5",
        })
        assert not is_error(invite_result), f"Invite failed: {invite_result}"

        # Create a new expert task since duplicate bids are not allowed
        task_id2 = await _create_task(mcp, "pub-s5", "Expert task 2 for invited tool", level="expert",
                                      invited_agent_ids=["toolbot-s5"])

        # ToolBot bids again -> ACCEPTED (invitation overrides tier restriction)
        bid2 = await _bid(mcp, task_id2, "toolbot-s5")
        assert bid2["status"] in ("accepted", "executing", "waiting"), (
            f"Invited tool-tier agent should be accepted on expert task: {bid2}"
        )

        # Verify acceptance via HTTP
        resp = await http.get(f"/api/tasks/{task_id2}")
        bids_by_agent = {b["agent_id"]: b["status"] for b in resp.json()["bids"]}
        assert bids_by_agent["toolbot-s5"] in ("waiting_execution", "executing")


# ═════════════════════════════════════════════════════════════════════
# Scenario 6: Full multi-agent marketplace simulation
# ═════════════════════════════════════════════════════════════════════


class TestMultiAgentMarketplace:
    """4 agents with different tiers compete for tasks in a realistic simulation."""

    @pytest.mark.asyncio
    async def test_full_marketplace_simulation(self, mcp, http, funded_network):
        """Coordinator publishes expert Python task; only eligible agents accepted."""
        # Register 4 agents with different tiers and roles
        await _register_agent(
            mcp, funded_network, "coordinator", "Coordinator",
            tier="general", agent_type="planner", domains=["python", "data-processing"],
        )
        await _register_agent(
            mcp, funded_network, "py-expert", "PythonExpert",
            tier="expert", agent_type="executor", domains=["python"],
        )
        await _register_agent(
            mcp, funded_network, "data-tool", "DataTool",
            tier="tool", agent_type="executor", domains=["data-processing"],
        )
        await _register_agent(
            mcp, funded_network, "gen-helper", "GeneralHelper",
            tier="expert_general", agent_type="executor", domains=["python", "data-processing"],
        )

        # Coordinator publishes an expert-level Python task
        task_id = await _create_task(
            mcp, "coordinator", "Build a Python ML pipeline",
            level="expert", domains=["python"], budget=500.0,
        )

        # Verify task was created with correct level
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.status_code == 200
        task_data = resp.json()
        assert task_data["level"] == "expert"

        # PythonExpert bids -> ACCEPTED (expert tier matches expert level)
        bid_py = await _bid(mcp, task_id, "py-expert", confidence=0.95, price=400.0)
        assert bid_py["status"] in ("accepted", "executing", "waiting"), (
            f"Expert should be accepted on expert task: {bid_py}"
        )

        # DataTool bids -> REJECTED (tool tier cannot bid on expert task)
        bid_data = await _bid(mcp, task_id, "data-tool", confidence=0.9, price=300.0)
        assert bid_data["status"] == "rejected", (
            f"Tool tier should be rejected on expert task: {bid_data}"
        )

        # GeneralHelper bids -> REJECTED (expert_general < expert in hierarchy)
        bid_gen = await _bid(mcp, task_id, "gen-helper", confidence=0.85, price=350.0)
        assert bid_gen["status"] == "rejected", (
            f"expert_general tier should be rejected on expert task: {bid_gen}"
        )

        # Verify all bids and their statuses via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        bids_by_agent = {b["agent_id"]: b["status"] for b in task_data["bids"]}
        assert bids_by_agent["py-expert"] in ("waiting_execution", "executing")
        assert bids_by_agent["data-tool"] == "rejected"
        assert bids_by_agent["gen-helper"] == "rejected"

        # PythonExpert submits result
        await mcp.call_tool_parsed("eacn3_submit_result", {
            "task_id": task_id,
            "agent_id": "py-expert",
            "content": {
                "pipeline": "sklearn.Pipeline([...])",
                "accuracy": 0.94,
                "notes": "Used RandomForest with cross-validation",
            },
        })

        # Coordinator closes and collects results
        await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "coordinator",
        })

        results = await mcp.call_tool_parsed("eacn3_get_task_results", {
            "task_id": task_id,
            "initiator_id": "coordinator",
        })
        assert len(results["results"]) >= 1
        result_agents = [r["agent_id"] for r in results["results"]]
        assert "py-expert" in result_agents

        # Coordinator selects PythonExpert's result
        await mcp.call_tool_parsed("eacn3_select_result", {
            "task_id": task_id,
            "agent_id": "py-expert",
            "initiator_id": "coordinator",
        })

        # Final verification via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        final = resp.json()
        selected = [r for r in final["results"] if r.get("selected")]
        assert len(selected) == 1
        assert selected[0]["agent_id"] == "py-expert"

    @pytest.mark.asyncio
    async def test_marketplace_general_task_open_to_all_except_tool(self, mcp, http, funded_network):
        """A general-level task accepts all non-tool tiers, rejects tool tier."""
        await _register_agent(
            mcp, funded_network, "coord-g", "Coordinator",
            tier="general", agent_type="planner", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "gen-exec", "GeneralExec",
            tier="general", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "exp-exec", "ExpertExec",
            tier="expert", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "expg-exec", "ExpertGeneralExec",
            tier="expert_general", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "tool-exec", "ToolExec",
            tier="tool", domains=["coding"],
        )

        task_id = await _create_task(
            mcp, "coord-g", "General coding task", level="general",
        )

        # General -> accepted
        bid1 = await _bid(mcp, task_id, "gen-exec")
        assert bid1["status"] in ("accepted", "executing", "waiting")

        # Expert -> rejected (expert index=1 > general index=0, so NOT eligible)
        bid2 = await _bid(mcp, task_id, "exp-exec")
        assert bid2["status"] == "rejected", (
            f"Expert tier should be rejected on general task (tier index 1 > level index 0): {bid2}"
        )

        # expert_general -> rejected (index 2 > 0)
        bid3 = await _bid(mcp, task_id, "expg-exec")
        assert bid3["status"] == "rejected", (
            f"expert_general tier should be rejected on general task: {bid3}"
        )

        # Tool -> rejected (tool only bids on tool)
        bid4 = await _bid(mcp, task_id, "tool-exec")
        assert bid4["status"] == "rejected", (
            f"Tool tier should be rejected on general task: {bid4}"
        )

    @pytest.mark.asyncio
    async def test_marketplace_tool_task_open_to_all(self, mcp, http, funded_network):
        """A tool-level task accepts ALL tiers including tool."""
        await _register_agent(
            mcp, funded_network, "coord-t", "Coordinator",
            tier="general", agent_type="planner", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "gen-t", "GeneralT",
            tier="general", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "exp-t", "ExpertT",
            tier="expert", domains=["coding"],
        )
        await _register_agent(
            mcp, funded_network, "tool-t", "ToolT",
            tier="tool", domains=["coding"],
        )

        task_id = await _create_task(
            mcp, "coord-t", "Simple tool-level task", level="tool",
        )

        # All tiers should be accepted on tool-level tasks
        bid1 = await _bid(mcp, task_id, "gen-t")
        assert bid1["status"] in ("accepted", "executing", "waiting"), (
            f"General should bid on tool task: {bid1}"
        )

        bid2 = await _bid(mcp, task_id, "exp-t")
        assert bid2["status"] in ("accepted", "executing", "waiting"), (
            f"Expert should bid on tool task: {bid2}"
        )

        bid3 = await _bid(mcp, task_id, "tool-t")
        assert bid3["status"] in ("accepted", "executing", "waiting"), (
            f"Tool should bid on tool task: {bid3}"
        )

        # Verify via HTTP
        resp = await http.get(f"/api/tasks/{task_id}")
        bids = resp.json()["bids"]
        active_bids = [b for b in bids if b["status"] in ("waiting_execution", "executing")]
        assert len(active_bids) == 3
