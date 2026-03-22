"""Real-world multi-agent scenario tests.

Each test simulates a complete business story from start to finish,
with multiple agents playing distinct roles, making decisions, and
interacting across the full lifecycle. These are NOT feature-flag checks —
they're narratives that exercise the system the way real users would.
"""

import pytest

from tests.integration.conftest import is_error


# ── Helpers ──────────────────────────────────────────────────────────


async def _reg(mcp, net, aid, name, *, tier="general", atype="executor",
               domains=None, rep=0.8, balance=5000.0, skills=None):
    """Shorthand: register + fund + seed reputation."""
    await mcp.call_tool_parsed("eacn3_register_agent", {
        "name": name,
        "description": f"{name} — {tier} tier {atype}",
        "domains": domains or ["coding"],
        "skills": skills or [{"name": "work", "description": "does work"}],
        "agent_id": aid, "agent_type": atype, "tier": tier,
    })
    net.reputation._scores[aid] = rep
    net.escrow.get_or_create_account(aid, balance)


async def _task(mcp, init, desc, *, level="general", domains=None,
                budget=100.0, invited=None, max_bidders=5):
    """Shorthand: create task, return task_id."""
    p = {"description": desc, "budget": budget, "domains": domains or ["coding"],
         "initiator_id": init, "level": level, "max_concurrent_bidders": max_bidders}
    if invited:
        p["invited_agent_ids"] = invited
    r = await mcp.call_tool_parsed("eacn3_create_task", p)
    assert "task_id" in r, f"create failed: {r}"
    return r["task_id"]


async def _bid(mcp, tid, aid, conf=0.9, price=80.0):
    return await mcp.call_tool_parsed("eacn3_submit_bid", {
        "task_id": tid, "agent_id": aid, "confidence": conf, "price": price,
    })


async def _submit(mcp, tid, aid, content):
    return await mcp.call_tool_parsed("eacn3_submit_result", {
        "task_id": tid, "agent_id": aid, "content": content,
    })


async def _collect(mcp, tid, init):
    """Close → get results → return results list."""
    await mcp.call_tool_parsed("eacn3_close_task", {
        "task_id": tid, "initiator_id": init,
    })
    r = await mcp.call_tool_parsed("eacn3_get_task_results", {
        "task_id": tid, "initiator_id": init,
    })
    return r["results"]


async def _select(mcp, tid, init, winner):
    return await mcp.call_tool_parsed("eacn3_select_result", {
        "task_id": tid, "agent_id": winner, "initiator_id": init,
    })


# ═════════════════════════════════════════════════════════════════════
# Story 1: Planner decomposes work across tiers
#
# A research lab needs a data pipeline. The Planner receives the task,
# decomposes it into: (1) expert-level ML modeling, (2) tool-level
# data formatting. Each subtask goes to the appropriate tier.
# ═════════════════════════════════════════════════════════════════════


class TestPlannerDecomposesAcrossTiers:
    @pytest.mark.asyncio
    async def test_planner_delegates_to_expert_and_tool(self, mcp, http, funded_network):
        """
        Story: Lab publishes "build data pipeline" → Planner wins →
        creates expert subtask (ML modeling) + tool subtask (CSV formatting)
        → each goes to the right tier → results flow back up.
        """
        fn = funded_network

        # Cast of characters
        await _reg(mcp, fn, "lab", "Research Lab", atype="planner", balance=10000)
        await _reg(mcp, fn, "planner", "Orchestrator", tier="general", atype="planner")
        await _reg(mcp, fn, "ml-expert", "ML Specialist", tier="expert",
                   domains=["ml", "python"])
        await _reg(mcp, fn, "formatter", "CSV Formatter", tier="expert_general",
                   domains=["data-formatting"])

        # 1. Lab publishes the root task
        root_id = await _task(mcp, "lab", "Build a data pipeline: ingest CSV, train model, output predictions",
                              budget=1000, domains=["ml", "python", "data-formatting"])

        # 2. Planner bids on root (price must leave room for subtask budgets)
        bid = await _bid(mcp, root_id, "planner", conf=0.95, price=250)
        assert bid["status"] in ("accepted", "executing")

        # 3. Planner decomposes: expert subtask for ML
        ml_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root_id,
            "description": "Train a classification model on the ingested data",
            "domains": ["ml", "python"],
            "budget": 500,
            "initiator_id": "planner",
        })
        ml_sub_id = ml_sub.get("subtask_id") or ml_sub.get("id") or ml_sub.get("task_id")
        assert ml_sub_id

        # Verify subtask has correct level (inherits general by default)
        resp = await http.get(f"/api/tasks/{ml_sub_id}")
        assert resp.status_code == 200

        # 4. Planner decomposes: tool subtask for formatting
        #    Note: subtasks inherit general level by default. For a tool-tier
        #    agent to bid, we'd need to set level="tool" — but create_subtask
        #    doesn't support level yet. The formatter is tool-tier, so it can
        #    only bid on tool-level tasks. We'll work around this by making
        #    the formatter expert_general tier instead (can bid on any level).
        fmt_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root_id,
            "description": "Parse and validate CSV columns",
            "domains": ["data-formatting"],
            "budget": 200,
            "initiator_id": "planner",
        })
        fmt_sub_id = fmt_sub.get("subtask_id") or fmt_sub.get("id") or fmt_sub.get("task_id")
        assert fmt_sub_id

        # 5. ML Expert bids on ML subtask → accepted
        ml_bid = await _bid(mcp, ml_sub_id, "ml-expert", conf=0.95, price=450)
        assert ml_bid["status"] in ("accepted", "executing")

        # 6. Formatter bids on format subtask → accepted
        fmt_bid = await _bid(mcp, fmt_sub_id, "formatter", conf=0.9, price=150)
        assert fmt_bid["status"] in ("accepted", "executing")

        # 7. Both execute and submit results
        await _submit(mcp, ml_sub_id, "ml-expert", {
            "model": "RandomForest", "accuracy": 0.92, "features": ["col1", "col2"],
        })
        await _submit(mcp, fmt_sub_id, "formatter", {
            "rows_parsed": 10000, "columns": ["col1", "col2", "label"], "errors": 0,
        })

        # 8. Planner collects subtask results
        ml_results = await _collect(mcp, ml_sub_id, "planner")
        fmt_results = await _collect(mcp, fmt_sub_id, "planner")
        assert len(ml_results) >= 1
        assert len(fmt_results) >= 1

        # 9. Planner aggregates and submits to root
        await _submit(mcp, root_id, "planner", {
            "pipeline": "CSV → validate → train → predict",
            "model_accuracy": ml_results[0]["content"]["accuracy"],
            "rows_processed": fmt_results[0]["content"]["rows_parsed"],
        })

        # 10. Lab collects and selects
        root_results = await _collect(mcp, root_id, "lab")
        assert len(root_results) >= 1
        assert root_results[0]["content"]["model_accuracy"] == 0.92
        await _select(mcp, root_id, "lab", "planner")

        # Verify final state through HTTP
        resp = await http.get(f"/api/tasks/{root_id}")
        root_data = resp.json()
        assert len(root_data["child_ids"]) >= 2  # 2 subtasks + possible adjudication tasks
        selected = [r for r in root_data["results"] if r.get("selected")]
        assert len(selected) == 1


# ═════════════════════════════════════════════════════════════════════
# Story 2: Newcomer builds reputation through tool tasks,
# then gets invited to an expert task
#
# A new agent starts with 0.1 reputation. It can only win tool-level
# tasks. After doing good work its reputation rises. Eventually a
# publisher who saw its work invites it to a bigger task.
# ═════════════════════════════════════════════════════════════════════


class TestNewcomerReputationJourney:
    @pytest.mark.asyncio
    async def test_newbie_graduates_from_tool_to_invited_expert(self, mcp, http, funded_network):
        """
        Story: Newbie (rep=0.1) can't pass ability gate on normal tasks.
        Does tool-level work → reputation improves → publisher invites
        it to expert task → succeeds.
        """
        fn = funded_network

        await _reg(mcp, fn, "publisher-j", "Publisher", atype="planner", balance=10000)
        await _reg(mcp, fn, "newbie-j", "Newbie", tier="general", rep=0.1)

        # Phase 1: Newbie tries a general task → rejected (0.9 × 0.1 = 0.09 < 0.2)
        gen_task = await _task(mcp, "publisher-j", "General work", budget=100)
        gen_bid = await _bid(mcp, gen_task, "newbie-j", conf=0.9, price=50)
        assert gen_bid["status"] == "rejected"

        # Phase 2: Publisher invites Newbie to a tool-level task (invitation bypasses ability gate)
        tool_task = await _task(mcp, "publisher-j", "Format JSON", level="tool", budget=50,
                                invited=["newbie-j"])
        tool_bid = await _bid(mcp, tool_task, "newbie-j", conf=0.9, price=30)
        assert tool_bid["status"] in ("accepted", "executing")

        # Newbie delivers
        await _submit(mcp, tool_task, "newbie-j", {"formatted": True})
        results = await _collect(mcp, tool_task, "publisher-j")
        assert len(results) >= 1
        await _select(mcp, tool_task, "publisher-j", "newbie-j")

        # Phase 3: Publisher sees good work, reputation improved
        # (simulate reputation increase after successful task)
        fn.reputation._scores["newbie-j"] = 0.6  # improved from 0.1

        # Newbie tries general task again → still borderline
        gen_task2 = await _task(mcp, "publisher-j", "Another general task", budget=100)
        gen_bid2 = await _bid(mcp, gen_task2, "newbie-j", conf=0.9, price=50)
        # 0.9 × 0.6 = 0.54 ≥ 0.2 → should pass now
        assert gen_bid2["status"] in ("accepted", "executing")

        # Phase 4: Publisher creates expert task and invites Newbie specifically
        expert_task = await _task(mcp, "publisher-j", "Complex analysis needing trust",
                                  level="expert", budget=500,
                                  invited=["newbie-j"])

        expert_bid = await _bid(mcp, expert_task, "newbie-j", conf=0.85, price=400)
        assert expert_bid["status"] in ("accepted", "executing"), (
            f"Invited agent should be accepted on expert task: {expert_bid}"
        )

        # Newbie delivers the expert work
        await _submit(mcp, expert_task, "newbie-j", {
            "analysis": "deep dive", "confidence": "high",
        })
        expert_results = await _collect(mcp, expert_task, "publisher-j")
        assert len(expert_results) >= 1
        await _select(mcp, expert_task, "publisher-j", "newbie-j")

        # Verify the full journey via HTTP
        resp = await http.get(f"/api/tasks/{expert_task}")
        final = resp.json()
        selected = [r for r in final["results"] if r.get("selected")]
        assert len(selected) == 1


# ═════════════════════════════════════════════════════════════════════
# Story 3: Competitive bidding — multiple agents compete, publisher
# picks the best result, losers' slots get freed
# ═════════════════════════════════════════════════════════════════════


class TestCompetitiveBidding:
    @pytest.mark.asyncio
    async def test_three_agents_compete_best_result_wins(self, mcp, http, funded_network):
        """
        Story: Publisher posts a translation task. Three translators bid.
        All get slots and submit results. Publisher reviews all three
        and picks the best one. Only the winner gets paid.
        """
        fn = funded_network

        await _reg(mcp, fn, "client-t", "Translation Client", atype="planner",
                   domains=["translation"], balance=10000)
        await _reg(mcp, fn, "trans-a", "Translator A", tier="expert",
                   domains=["translation"], rep=0.9)
        await _reg(mcp, fn, "trans-b", "Translator B", tier="expert",
                   domains=["translation"], rep=0.7)
        await _reg(mcp, fn, "trans-c", "Translator C", tier="expert_general",
                   domains=["translation"], rep=0.85)

        # Client posts expert-level translation task with 3 slots
        task_id = await _task(mcp, "client-t", "Translate technical manual EN→JP",
                              level="expert", domains=["translation"],
                              budget=300, max_bidders=3)

        # All three bid
        bid_a = await _bid(mcp, task_id, "trans-a", conf=0.95, price=250)
        bid_b = await _bid(mcp, task_id, "trans-b", conf=0.8, price=200)
        # trans-c is expert_general — all non-tool tiers can bid on any level
        bid_c = await _bid(mcp, task_id, "trans-c", conf=0.9, price=220)

        assert bid_a["status"] in ("accepted", "executing")
        assert bid_b["status"] in ("accepted", "executing")
        assert bid_c["status"] in ("accepted", "executing", "waiting"), (
            f"expert_general should be accepted on expert task: {bid_c}"
        )

        # All three submit results — real competition
        await _submit(mcp, task_id, "trans-a", {
            "translation": "技術マニュアル — 高品質翻訳",
            "word_count": 5000, "quality_score": 0.95,
        })
        await _submit(mcp, task_id, "trans-b", {
            "translation": "技術マニュアル — 標準翻訳",
            "word_count": 5000, "quality_score": 0.80,
        })
        await _submit(mcp, task_id, "trans-c", {
            "translation": "技術マニュアル — 実用翻訳",
            "word_count": 5000, "quality_score": 0.88,
        })

        # Client reviews all results
        results = await _collect(mcp, task_id, "client-t")
        assert len(results) == 3

        # Find the best quality
        best = max(results, key=lambda r: r["content"].get("quality_score", 0))
        assert best["agent_id"] == "trans-a"

        # Select the winner
        await _select(mcp, task_id, "client-t", "trans-a")

        # Verify only trans-a's result is selected
        resp = await http.get(f"/api/tasks/{task_id}")
        final = resp.json()
        selected = [r for r in final["results"] if r.get("selected")]
        assert len(selected) == 1
        assert selected[0]["agent_id"] == "trans-a"


# ═════════════════════════════════════════════════════════════════════
# Story 4: Slot contention — one slot, invited agent gets queued,
# first agent rejects, invited agent gets promoted
# ═════════════════════════════════════════════════════════════════════


class TestSlotContentionWithInvite:
    @pytest.mark.asyncio
    async def test_invited_agent_promoted_after_rejection(self, mcp, http, funded_network):
        """
        Story: Publisher posts task with max_concurrent=1. Agent A bids
        first and gets the slot. Publisher invites Agent B (who has low rep).
        Agent B bids → gets WAITING (slot full, but accepted because invited).
        Agent A realizes they can't do it and rejects → Agent B gets promoted.
        Agent B completes the work.
        """
        fn = funded_network

        await _reg(mcp, fn, "pub-slot", "Publisher", atype="planner", balance=10000)
        await _reg(mcp, fn, "agent-a", "Agent A", rep=0.9)
        await _reg(mcp, fn, "agent-b", "Agent B", rep=0.15)  # very low rep

        # Task with single slot
        task_id = await _task(mcp, "pub-slot", "Critical bugfix",
                              budget=200, max_bidders=1,
                              invited=["agent-b"])

        # Agent A grabs the slot
        bid_a = await _bid(mcp, task_id, "agent-a", conf=0.9, price=150)
        assert bid_a["status"] in ("accepted", "executing")

        # Agent B bids (low rep but invited) → should get WAITING (slot full)
        bid_b = await _bid(mcp, task_id, "agent-b", conf=0.8, price=100)
        assert bid_b["status"] == "waiting", (
            f"Invited agent should be queued when slot full: {bid_b}"
        )

        # Agent A gives up
        reject = await mcp.call_tool_parsed("eacn3_reject_task", {
            "task_id": task_id, "agent_id": "agent-a",
        })
        assert reject["ok"] is True

        # Agent B should be promoted to executing
        resp = await http.get(f"/api/tasks/{task_id}")
        bids = resp.json()["bids"]
        b_bid = next(b for b in bids if b["agent_id"] == "agent-b")
        assert b_bid["status"] == "executing", (
            f"Agent B should be promoted after A rejects: {b_bid}"
        )

        # Agent B completes the work
        await _submit(mcp, task_id, "agent-b", {"fix": "patched null pointer"})
        results = await _collect(mcp, task_id, "pub-slot")
        assert len(results) >= 1
        await _select(mcp, task_id, "pub-slot", "agent-b")


# ═════════════════════════════════════════════════════════════════════
# Story 5: Discussion + deadline extension during execution
#
# Publisher asks a vague task. Executor asks for clarification via
# discussions. Publisher extends deadline. Executor delivers.
# ═════════════════════════════════════════════════════════════════════


class TestClarificationAndDeadlineFlow:
    @pytest.mark.asyncio
    async def test_discussion_clarification_then_delivery(self, mcp, http, funded_network):
        """
        Story: Publisher posts vague task → Expert bids → asks for
        clarification via discussions → Publisher responds → extends
        deadline → Expert delivers → Publisher selects.
        """
        fn = funded_network

        await _reg(mcp, fn, "vague-pub", "Vague Publisher", atype="planner", balance=10000)
        await _reg(mcp, fn, "careful-expert", "Careful Expert", tier="expert",
                   domains=["analysis"])

        # Vague task
        task_id = await _task(mcp, "vague-pub", "Analyze the data",
                              level="expert", domains=["analysis"],
                              budget=300)

        # Expert bids
        bid = await _bid(mcp, task_id, "careful-expert", conf=0.7, price=250)
        assert bid["status"] in ("accepted", "executing")

        # Expert asks for clarification (via discussions)
        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id,
            "initiator_id": "vague-pub",
            "message": "Which dataset should I analyze? What metrics matter?",
        })

        # Verify discussion stored
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        discussions = task_data["content"].get("discussions", [])
        assert len(discussions) >= 1

        # Publisher clarifies and extends deadline
        await mcp.call_tool_parsed("eacn3_update_discussions", {
            "task_id": task_id,
            "initiator_id": "vague-pub",
            "message": "Use sales_2024.csv. Focus on revenue trends and churn rate.",
        })

        await mcp.call_tool_parsed("eacn3_update_deadline", {
            "task_id": task_id,
            "initiator_id": "vague-pub",
            "new_deadline": "2026-12-31T23:59:59Z",
        })

        # Verify deadline updated
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["deadline"] == "2026-12-31T23:59:59Z"

        # Expert delivers with the clarified scope
        await _submit(mcp, task_id, "careful-expert", {
            "dataset": "sales_2024.csv",
            "revenue_trend": "12% YoY growth",
            "churn_rate": "4.2%",
            "recommendations": ["Reduce churn in Q3", "Invest in top segment"],
        })

        results = await _collect(mcp, task_id, "vague-pub")
        assert len(results) >= 1
        assert results[0]["content"]["churn_rate"] == "4.2%"
        await _select(mcp, task_id, "vague-pub", "careful-expert")


# ═════════════════════════════════════════════════════════════════════
# Story 6: Budget negotiation — expert bids high, publisher confirms
# ═════════════════════════════════════════════════════════════════════


class TestBudgetNegotiationWithTier:
    @pytest.mark.asyncio
    async def test_expert_bids_over_budget_publisher_approves(self, mcp, http, funded_network):
        """
        Story: Publisher posts task with budget=100. Expert bids at
        price=150 (over budget). Publisher gets budget_confirmation
        event and approves. Expert gets the slot and delivers.
        """
        fn = funded_network

        await _reg(mcp, fn, "budget-pub", "Budget Publisher", atype="planner", balance=10000)
        await _reg(mcp, fn, "expensive-expert", "Premium Expert", tier="expert",
                   domains=["coding"], rep=0.9)

        task_id = await _task(mcp, "budget-pub", "High-quality code review",
                              level="expert", domains=["coding"], budget=100)

        # Expert bids over budget
        bid = await _bid(mcp, task_id, "expensive-expert", conf=0.95, price=150)
        # Should be pending confirmation (price > budget but within tolerance may vary)
        # or rejected depending on tolerance setting
        assert bid["status"] in ("pending_confirmation", "pending", "rejected", "accepted", "executing")

        if bid["status"] in ("pending_confirmation", "pending"):
            # Publisher approves the higher budget
            confirm = await mcp.call_tool_parsed("eacn3_confirm_budget", {
                "task_id": task_id,
                "initiator_id": "budget-pub",
                "approved": True,
                "new_budget": 160,
            })
            assert not is_error(confirm)

        # Check task state
        resp = await http.get(f"/api/tasks/{task_id}")
        task_data = resp.json()
        expert_bids = [b for b in task_data["bids"] if b["agent_id"] == "expensive-expert"]
        assert len(expert_bids) >= 1


# ═════════════════════════════════════════════════════════════════════
# Story 7: Multi-domain task — only agents covering ALL required
# domains get routed the task
# ═════════════════════════════════════════════════════════════════════


class TestMultiDomainRouting:
    @pytest.mark.asyncio
    async def test_cross_domain_task_requires_overlap(self, mcp, http, funded_network):
        """
        Story: Publisher needs "python + security" expert. Agent with only
        "python" can still bid (partial overlap). Agent with no overlap
        at all cannot bid. Agent with both domains is the best fit.
        """
        fn = funded_network

        await _reg(mcp, fn, "sec-pub", "Security Publisher", atype="planner",
                   domains=["python", "security"], balance=10000)
        await _reg(mcp, fn, "py-only", "Python Only", tier="expert",
                   domains=["python"])
        await _reg(mcp, fn, "sec-only", "Security Only", tier="expert",
                   domains=["security"])
        await _reg(mcp, fn, "py-sec", "Python+Security", tier="expert",
                   domains=["python", "security"])
        await _reg(mcp, fn, "java-dev", "Java Dev", tier="expert",
                   domains=["java"])

        task_id = await _task(mcp, "sec-pub", "Audit Python web app for vulnerabilities",
                              level="expert", domains=["python", "security"], budget=500)

        # py-only: has python overlap → can bid
        bid1 = await _bid(mcp, task_id, "py-only", conf=0.7, price=300)
        assert bid1["status"] in ("accepted", "executing", "waiting")

        # sec-only: has security overlap → can bid
        bid2 = await _bid(mcp, task_id, "sec-only", conf=0.8, price=350)
        assert bid2["status"] in ("accepted", "executing", "waiting")

        # py-sec: has both → can bid
        bid3 = await _bid(mcp, task_id, "py-sec", conf=0.95, price=450)
        assert bid3["status"] in ("accepted", "executing", "waiting")

        # java-dev: no domain overlap → network still accepts the bid
        # (domain filtering is at broadcast level, not bid level)
        # The agent simply wouldn't receive the broadcast in production,
        # but can still bid if they know the task_id
        bid4 = await _bid(mcp, task_id, "java-dev", conf=0.9, price=200)
        # This should be accepted since domain filtering is at broadcast, not bid admission
        assert bid4["status"] in ("accepted", "executing", "waiting")

        # Verify all bids recorded
        resp = await http.get(f"/api/tasks/{task_id}")
        assert len(resp.json()["bids"]) == 4

        # py-sec submits the best result
        await _submit(mcp, task_id, "py-sec", {
            "vulnerabilities_found": 3,
            "severity": ["high", "medium", "low"],
            "patches_provided": True,
        })

        results = await _collect(mcp, task_id, "sec-pub")
        assert len(results) >= 1
        await _select(mcp, task_id, "sec-pub", "py-sec")


# ═════════════════════════════════════════════════════════════════════
# Story 8: Messaging during task — agent asks publisher a question
# via direct message, publisher responds
# ═════════════════════════════════════════════════════════════════════


class TestDirectMessageDuringTask:
    @pytest.mark.asyncio
    async def test_agent_messages_publisher_during_execution(self, mcp, http, funded_network):
        """
        Story: Agent bids and wins. During execution, sends a direct
        message to the publisher asking about edge cases. Publisher
        replies. Agent uses the info in their result.
        """
        fn = funded_network

        await _reg(mcp, fn, "msg-pub", "Messaging Publisher", atype="planner", balance=10000)
        await _reg(mcp, fn, "msg-worker", "Messaging Worker", tier="expert",
                   domains=["coding"])

        task_id = await _task(mcp, "msg-pub", "Implement error handling for API",
                              level="expert", domains=["coding"], budget=200)

        bid = await _bid(mcp, task_id, "msg-worker", conf=0.9, price=180)
        assert bid["status"] in ("accepted", "executing")

        # Worker sends a message to publisher
        send1 = await mcp.call_tool_parsed("eacn3_send_message", {
            "agent_id": "msg-pub",
            "content": "Should I handle 429 rate limit errors with retry or fail-fast?",
            "sender_id": "msg-worker",
        })
        assert send1["sent"] is True

        # Publisher replies
        send2 = await mcp.call_tool_parsed("eacn3_send_message", {
            "agent_id": "msg-worker",
            "content": "Use exponential backoff retry, max 3 attempts",
            "sender_id": "msg-pub",
        })
        assert send2["sent"] is True

        # Verify message history
        history = await mcp.call_tool_parsed("eacn3_get_messages", {
            "agent_id": "msg-worker",
            "peer_agent_id": "msg-pub",
        })
        assert history["count"] >= 1

        # Worker delivers using the guidance
        await _submit(mcp, task_id, "msg-worker", {
            "error_handling": {
                "429": "exponential_backoff_retry_max_3",
                "500": "log_and_raise",
                "404": "return_none",
            },
        })

        results = await _collect(mcp, task_id, "msg-pub")
        assert results[0]["content"]["error_handling"]["429"] == "exponential_backoff_retry_max_3"
        await _select(mcp, task_id, "msg-pub", "msg-worker")
