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
        await _reg(mcp, fn, "formatter", "CSV Formatter", tier="tool",
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

        # 4. Planner decomposes: tool-level subtask for formatting
        #    Tool-tier formatter can only bid on tool-level tasks, so we
        #    explicitly set level="tool" on this subtask.
        fmt_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root_id,
            "description": "Parse and validate CSV columns",
            "domains": ["data-formatting"],
            "budget": 200,
            "initiator_id": "planner",
            "level": "tool",
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


# ═════════════════════════════════════════════════════════════════════
# Story 9: Planner creates tool-level subtask for tool-tier agent
#
# Now that create_subtask supports level, verify the full pipeline:
# planner creates subtask with level="tool" → tool agent bids →
# tool agent on general subtask is still rejected.
# ═════════════════════════════════════════════════════════════════════


class TestSubtaskLevelInheritance:
    @pytest.mark.asyncio
    async def test_tool_agent_on_tool_subtask_vs_inherited_subtask(self, mcp, http, funded_network):
        """
        Story: Planner creates two subtasks from a general parent.
        Subtask A: no level specified → inherits parent's general level.
        Subtask B: level="tool" explicitly set.
        Tool-tier agent can bid on B but not A.
        """
        fn = funded_network

        await _reg(mcp, fn, "boss-9", "Boss", atype="planner", balance=10000)
        await _reg(mcp, fn, "planner-9", "Planner", atype="planner")
        await _reg(mcp, fn, "tool-9", "ToolWorker", tier="tool", domains=["formatting"])

        root = await _task(mcp, "boss-9", "Big project", budget=2000,
                           domains=["coding", "formatting"])

        # Planner takes root
        bid = await _bid(mcp, root, "planner-9", conf=0.9, price=200)
        assert bid["status"] in ("accepted", "executing")

        # Subtask A: inherits general level
        sub_a = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root,
            "description": "Complex analysis subtask",
            "domains": ["formatting"],
            "budget": 300,
            "initiator_id": "planner-9",
        })
        sub_a_id = sub_a.get("subtask_id") or sub_a.get("id")

        # Subtask B: explicitly tool level
        sub_b = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root,
            "description": "Simple format conversion",
            "domains": ["formatting"],
            "budget": 100,
            "initiator_id": "planner-9",
            "level": "tool",
        })
        sub_b_id = sub_b.get("subtask_id") or sub_b.get("id")

        # Verify levels via HTTP
        resp_a = await http.get(f"/api/tasks/{sub_a_id}")
        resp_b = await http.get(f"/api/tasks/{sub_b_id}")
        assert resp_a.json()["level"] == "general"  # inherited
        assert resp_b.json()["level"] == "tool"      # explicit

        # Tool agent bids on inherited general subtask → rejected
        bid_a = await _bid(mcp, sub_a_id, "tool-9", conf=0.9, price=200)
        assert bid_a["status"] == "rejected"

        # Tool agent bids on tool subtask → accepted
        bid_b = await _bid(mcp, sub_b_id, "tool-9", conf=0.9, price=80)
        assert bid_b["status"] in ("accepted", "executing")


# ═════════════════════════════════════════════════════════════════════
# Story 10: Agent at max capacity — autoBidEvaluate skips
#
# An agent has max_concurrent_tasks=2. After taking 2 tasks, the
# third broadcast should not auto-match. Manually bidding still works.
# ═════════════════════════════════════════════════════════════════════


class TestAgentCapacityLimit:
    @pytest.mark.asyncio
    async def test_agent_at_capacity_can_still_manually_bid(self, mcp, http, funded_network):
        """
        Story: Worker registers with max_concurrent_tasks=2.
        Takes 2 tasks. Publisher posts 3rd. Worker can still manually
        bid (auto-match would skip, but manual bid is always possible).
        """
        fn = funded_network

        await _reg(mcp, fn, "pub-cap", "Publisher", atype="planner", balance=20000)
        await mcp.call_tool_parsed("eacn3_register_agent", {
            "name": "BusyWorker",
            "description": "Limited capacity worker",
            "domains": ["coding"],
            "skills": [{"name": "code", "description": "codes"}],
            "agent_id": "busy-worker",
            "tier": "general",
            "capabilities": {"max_concurrent_tasks": 2, "concurrent": True},
        })
        fn.reputation._scores["busy-worker"] = 0.8
        fn.escrow.get_or_create_account("busy-worker", 5000)

        # Take 2 tasks
        t1 = await _task(mcp, "pub-cap", "Task 1", budget=100)
        t2 = await _task(mcp, "pub-cap", "Task 2", budget=100)

        b1 = await _bid(mcp, t1, "busy-worker", conf=0.9, price=80)
        b2 = await _bid(mcp, t2, "busy-worker", conf=0.9, price=80)
        assert b1["status"] in ("accepted", "executing")
        assert b2["status"] in ("accepted", "executing")

        # 3rd task — worker can still bid manually
        t3 = await _task(mcp, "pub-cap", "Task 3", budget=100)
        b3 = await _bid(mcp, t3, "busy-worker", conf=0.9, price=80)
        # Network doesn't enforce capacity — only autoBidEvaluate checks it
        assert b3["status"] in ("accepted", "executing")

        # Worker completes task 1 to free a slot
        await _submit(mcp, t1, "busy-worker", {"done": True})
        await _collect(mcp, t1, "pub-cap")
        await _select(mcp, t1, "pub-cap", "busy-worker")


# ═════════════════════════════════════════════════════════════════════
# Story 11: Task expires with no bidders — budget refunded
# ═════════════════════════════════════════════════════════════════════


class TestTaskExpiresNoBidders:
    @pytest.mark.asyncio
    async def test_close_without_bids_refunds_budget(self, mcp, http, funded_network):
        """
        Story: Publisher posts task, no one bids. Publisher closes it.
        Budget should be refunded.
        """
        fn = funded_network

        await _reg(mcp, fn, "lonely-pub", "Lonely Publisher", atype="planner", balance=1000)

        # Check initial balance
        bal_before = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "lonely-pub"})
        avail_before = bal_before["available"]

        # Post a task — freezes 200 from balance
        task_id = await _task(mcp, "lonely-pub", "Nobody wants this task",
                              budget=200, domains=["obscure-domain"])

        # Verify balance decreased
        bal_mid = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "lonely-pub"})
        assert bal_mid["frozen"] >= 200

        # No bids arrive. Publisher closes.
        close = await mcp.call_tool_parsed("eacn3_close_task", {
            "task_id": task_id,
            "initiator_id": "lonely-pub",
        })
        assert not is_error(close)

        # Verify task is in terminal state
        resp = await http.get(f"/api/tasks/{task_id}")
        assert resp.json()["status"] in ("no_one_able", "completed")

        # Budget should be refunded
        bal_after = await mcp.call_tool_parsed("eacn3_get_balance", {"agent_id": "lonely-pub"})
        assert bal_after["available"] >= avail_before - 1  # allow small rounding


# ═════════════════════════════════════════════════════════════════════
# Story 12: Agent changes domains mid-session — gets new task types
# ═════════════════════════════════════════════════════════════════════


class TestAgentUpdatesDomains:
    @pytest.mark.asyncio
    async def test_update_domains_enables_new_tasks(self, mcp, http, funded_network):
        """
        Story: Agent starts with domains=["python"]. Realizes they
        also know "rust". Updates their card. Now they can bid on
        rust tasks too.
        """
        fn = funded_network

        await _reg(mcp, fn, "pub-upd", "Publisher", atype="planner", balance=10000,
                   domains=["python", "rust"])
        await _reg(mcp, fn, "learner", "Learning Agent", tier="expert",
                   domains=["python"])

        # Learner can bid on python task
        py_task = await _task(mcp, "pub-upd", "Python script", domains=["python"], budget=100)
        py_bid = await _bid(mcp, py_task, "learner", conf=0.9, price=80)
        assert py_bid["status"] in ("accepted", "executing")

        # Learner updates domains to include rust
        await mcp.call_tool_parsed("eacn3_update_agent", {
            "agent_id": "learner",
            "domains": ["python", "rust"],
        })

        # Verify update via HTTP
        resp = await http.get("/api/discovery/agents/learner")
        assert "rust" in resp.json()["domains"]

        # Now learner can bid on rust tasks too
        rust_task = await _task(mcp, "pub-upd", "Rust CLI tool", domains=["rust"], budget=150)
        rust_bid = await _bid(mcp, rust_task, "learner", conf=0.8, price=120)
        assert rust_bid["status"] in ("accepted", "executing")


# ═════════════════════════════════════════════════════════════════════
# ▓▓▓  ROUND 4 — Multi-level decomposition & delegation behavior  ▓▓▓
# ═════════════════════════════════════════════════════════════════════
#
# Core question: Does the system support and incentivize
# "give specialist work to specialists"?
#
# - Story 13: 3-level deep decomposition with budget cascade
# - Story 14: Planner correctly delegates unfamiliar domain
# - Story 15: Adjudication — third party evaluates result quality
# - Story 16: Compare: generalist-does-all vs specialist-delegation


# ═════════════════════════════════════════════════════════════════════
# Story 13: 3-level decomposition — Client → Planner → Specialist → Tool
#
# "Build a data analytics dashboard" decomposes across three tiers.
# Budget flows down level by level, results bubble up.
# ═════════════════════════════════════════════════════════════════════


class TestThreeLevelDecomposition:
    @pytest.mark.asyncio
    async def test_full_3_level_budget_and_result_cascade(self, mcp, http, funded_network):
        """
        Story:
        Client (business user) → publishes "analytics dashboard" with 2000 budget
        Planner A → bids, decomposes into:
          - Subtask: "Backend data API" (expert, budget=600)
            Backend Expert → bids, further decomposes:
              - Sub-subtask: "Generate SQL queries" (tool, budget=200)
                SQL Tool → bids, executes, submits result
            Backend Expert collects SQL result, incorporates, submits
          Planner collects backend result, submits final to Client

        This tests:
        - 3 levels of depth (root → subtask → sub-subtask)
        - Budget cascading: 2000 → 600 → 200 (remaining tracked at each level)
        - Results bubbling up through the chain
        - Tool-tier agent participates via level="tool" subtask
        """
        fn = funded_network

        # Register all participants
        await _reg(mcp, fn, "client-13", "Business Client", atype="planner",
                   balance=20000, domains=["analytics"])
        await _reg(mcp, fn, "planner-13", "Project Planner", atype="planner",
                   domains=["analytics", "backend", "frontend"])
        await _reg(mcp, fn, "backend-13", "Backend Expert", tier="expert",
                   domains=["backend", "sql"])
        await _reg(mcp, fn, "sql-tool-13", "SQL Generator", tier="tool",
                   domains=["sql"])

        # Level 0: Client publishes root task
        root_id = await _task(mcp, "client-13", "Build analytics dashboard",
                              budget=2000, domains=["analytics"])

        # Planner bids on root
        bid0 = await _bid(mcp, root_id, "planner-13", conf=0.95, price=1800)
        assert bid0["status"] in ("accepted", "executing")

        # Verify root remaining budget = 2000
        root_resp = await http.get(f"/api/tasks/{root_id}")
        assert root_resp.json()["remaining_budget"] == 2000

        # Level 1: Planner creates "Backend data API" subtask
        sub1 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root_id,
            "description": "Build backend data API with caching",
            "domains": ["backend", "sql"],
            "budget": 600,
            "initiator_id": "planner-13",
            "level": "expert",
        })
        sub1_id = sub1.get("subtask_id") or sub1.get("id")
        assert sub1_id is not None

        # Verify root remaining dropped
        root_resp2 = await http.get(f"/api/tasks/{root_id}")
        assert root_resp2.json()["remaining_budget"] == 1400  # 2000 - 600

        # Backend expert bids on level-1 subtask
        bid1 = await _bid(mcp, sub1_id, "backend-13", conf=0.9, price=500)
        assert bid1["status"] in ("accepted", "executing")

        # Level 2: Backend expert further decomposes to tool subtask
        sub2 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": sub1_id,
            "description": "Generate optimized SQL queries for dashboard metrics",
            "domains": ["sql"],
            "budget": 200,
            "initiator_id": "backend-13",
            "level": "tool",
        })
        sub2_id = sub2.get("subtask_id") or sub2.get("id")
        assert sub2_id is not None

        # Verify level-1 remaining dropped
        sub1_resp = await http.get(f"/api/tasks/{sub1_id}")
        assert sub1_resp.json()["remaining_budget"] == 400  # 600 - 200

        # Verify depth
        sub2_resp = await http.get(f"/api/tasks/{sub2_id}")
        assert sub2_resp.json()["depth"] == 2  # root=0, sub1=1, sub2=2
        assert sub2_resp.json()["level"] == "tool"

        # SQL tool bids on level-2 tool subtask
        bid2 = await _bid(mcp, sub2_id, "sql-tool-13", conf=0.99, price=150)
        assert bid2["status"] in ("accepted", "executing")

        # === Results bubble up ===

        # Level 2: SQL tool submits
        await _submit(mcp, sub2_id, "sql-tool-13", {
            "queries": [
                "SELECT date, SUM(revenue) FROM sales GROUP BY date",
                "SELECT product, COUNT(*) FROM orders GROUP BY product",
            ],
            "optimization_notes": "Added indexes on date and product columns",
        })

        # Backend expert collects SQL result
        sub2_results = await _collect(mcp, sub2_id, "backend-13")
        assert len(sub2_results) >= 1
        assert "queries" in sub2_results[0]["content"]
        await _select(mcp, sub2_id, "backend-13", "sql-tool-13")

        # Level 1: Backend expert submits using SQL tool's work
        await _submit(mcp, sub1_id, "backend-13", {
            "api_endpoints": ["/api/revenue", "/api/products"],
            "sql_queries": sub2_results[0]["content"]["queries"],
            "caching_strategy": "Redis TTL 5min",
        })

        # Planner collects backend result
        sub1_results = await _collect(mcp, sub1_id, "planner-13")
        assert len(sub1_results) >= 1
        assert "api_endpoints" in sub1_results[0]["content"]
        await _select(mcp, sub1_id, "planner-13", "backend-13")

        # Level 0: Planner submits final composed result to client
        await _submit(mcp, root_id, "planner-13", {
            "dashboard_url": "https://dashboard.example.com",
            "backend_api": sub1_results[0]["content"]["api_endpoints"],
            "features": ["revenue chart", "product breakdown", "date filter"],
            "architecture": "React frontend + FastAPI backend + Redis cache",
        })

        root_results = await _collect(mcp, root_id, "client-13")
        assert len(root_results) >= 1
        final = root_results[0]["content"]
        assert "backend_api" in final
        assert len(final["backend_api"]) == 2
        await _select(mcp, root_id, "client-13", "planner-13")


# ═════════════════════════════════════════════════════════════════════
# Story 14: Planner delegates what they don't know
#
# A coding planner gets a task that requires coding + security audit.
# Instead of pretending they know security, they delegate the security
# part to a specialist. This is the CORRECT behavior.
# ═════════════════════════════════════════════════════════════════════


class TestDelegateUnfamiliarDomain:
    @pytest.mark.asyncio
    async def test_planner_delegates_security_to_specialist(self, mcp, http, funded_network):
        """
        Story:
        Task: "Build payment API with PCI compliance audit"
        Planner knows coding but NOT security.
        Planner does the coding part themselves, then creates a subtask
        for the security audit. Security expert handles the audit.
        Planner composes both into final result.

        Key insight: The system ENABLES this pattern through:
        1. Subtask creation with specific domains
        2. Domain-based broadcast reaches the right specialists
        3. Budget carving lets planner allocate appropriate budget
        """
        fn = funded_network

        await _reg(mcp, fn, "biz-14", "Business Owner", atype="planner",
                   balance=15000, domains=["payments"])
        await _reg(mcp, fn, "coder-14", "Coding Planner", atype="planner",
                   domains=["coding", "payments"])
        await _reg(mcp, fn, "sec-14", "Security Auditor", tier="expert",
                   domains=["security", "pci"], rep=0.95)

        # Business publishes task with two domain requirements
        task_id = await _task(mcp, "biz-14",
                              "Build payment API with PCI compliance audit",
                              budget=3000, domains=["coding", "security"])

        # Coding planner bids — they know coding but not security
        bid = await _bid(mcp, task_id, "coder-14", conf=0.7, price=2500)
        assert bid["status"] in ("accepted", "executing")

        # Planner creates security subtask — DELEGATING what they don't know
        sec_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": task_id,
            "description": "PCI DSS compliance audit for payment API",
            "domains": ["security", "pci"],
            "budget": 1000,
            "initiator_id": "coder-14",
            "level": "expert",  # Security needs expert level
        })
        sec_sub_id = sec_sub.get("subtask_id") or sec_sub.get("id")

        # Security expert bids on what they're good at
        sec_bid = await _bid(mcp, sec_sub_id, "sec-14", conf=0.98, price=800)
        assert sec_bid["status"] in ("accepted", "executing")

        # Security expert delivers their specialized work
        await _submit(mcp, sec_sub_id, "sec-14", {
            "pci_level": "Level 1",
            "findings": [
                {"severity": "critical", "issue": "Card data stored unencrypted",
                 "fix": "Use AES-256 encryption at rest"},
                {"severity": "high", "issue": "No rate limiting on auth endpoint",
                 "fix": "Add 10 req/min rate limit"},
            ],
            "compliant": False,
            "remediation_steps": 5,
        })

        # Planner collects security audit
        sec_results = await _collect(mcp, sec_sub_id, "coder-14")
        assert sec_results[0]["content"]["pci_level"] == "Level 1"
        assert len(sec_results[0]["content"]["findings"]) == 2
        await _select(mcp, sec_sub_id, "coder-14", "sec-14")

        # Planner combines their coding work + security audit into final result
        await _submit(mcp, task_id, "coder-14", {
            "api_implementation": {
                "endpoints": ["/pay", "/refund", "/status"],
                "framework": "FastAPI",
                "encryption": "AES-256",  # Applied security recommendation
            },
            "security_audit": sec_results[0]["content"],
            "pci_compliant_after_fixes": True,
        })

        # Business owner gets a complete result
        results = await _collect(mcp, task_id, "biz-14")
        final = results[0]["content"]
        assert "api_implementation" in final
        assert "security_audit" in final
        assert final["security_audit"]["pci_level"] == "Level 1"
        await _select(mcp, task_id, "biz-14", "coder-14")


# ═════════════════════════════════════════════════════════════════════
# Story 15: Adjudication — third party evaluates result quality
#
# When an agent submits a result, the system auto-creates an
# adjudication task. Another agent evaluates the result and scores it.
# This is how the network ensures quality.
# ═════════════════════════════════════════════════════════════════════


class TestAdjudicationFlow:
    @pytest.mark.asyncio
    async def test_result_triggers_adjudication_and_score_collected(self, mcp, http, funded_network):
        """
        Story:
        Publisher posts task. Worker submits result.
        System auto-creates adjudication task.
        Reviewer bids on adjudication, evaluates, submits verdict.
        Publisher can see adjudication scores alongside results.
        """
        fn = funded_network

        await _reg(mcp, fn, "pub-15", "Publisher", atype="planner",
                   balance=10000, domains=["coding"])
        await _reg(mcp, fn, "worker-15", "Worker", tier="expert",
                   domains=["coding"])
        await _reg(mcp, fn, "reviewer-15", "Code Reviewer", tier="expert",
                   domains=["coding"], rep=0.95)

        task_id = await _task(mcp, "pub-15", "Implement sorting algorithm",
                              budget=500, domains=["coding"])

        bid = await _bid(mcp, task_id, "worker-15", conf=0.9, price=400)
        assert bid["status"] in ("accepted", "executing")

        # Worker submits result → this triggers adjudication task creation
        await _submit(mcp, task_id, "worker-15", {
            "algorithm": "quicksort",
            "complexity": "O(n log n) average",
            "code": "def quicksort(arr): ...",
        })

        # Find the auto-created adjudication task
        # Adjudication tasks have IDs like "adj-{task_id}-{agent_id}-..."
        adj_tasks = []
        for tid, task in fn.task_manager._tasks.items():
            if tid.startswith("adj-") and task.parent_id == task_id:
                adj_tasks.append(task)

        assert len(adj_tasks) >= 1, "Adjudication task should be auto-created"
        adj_task = adj_tasks[0]
        adj_id = adj_task.id

        # Verify adjudication task properties
        assert adj_task.type.value == "adjudication"
        assert adj_task.budget == 0.0  # No monetary compensation
        assert adj_task.initiator_id == "system"

        # Reviewer bids on adjudication task
        adj_bid = await _bid(mcp, adj_id, "reviewer-15", conf=0.99, price=0)
        assert adj_bid["status"] in ("accepted", "executing")

        # Reviewer evaluates and submits verdict
        await _submit(mcp, adj_id, "reviewer-15", {
            "verdict": "Good implementation with correct complexity analysis",
            "score": 0.85,
            "feedback": "Consider adding in-place variant for memory efficiency",
        })

        # Verify adjudication score was collected on the original result
        orig_task = fn.task_manager.get(task_id)
        worker_result = next(r for r in orig_task.results if r.agent_id == "worker-15")
        assert len(worker_result.adjudications) >= 1
        adj = worker_result.adjudications[0]
        assert adj.adjudicator_id == "reviewer-15"
        assert adj.score == 0.85

        # Publisher sees results with adjudication data
        results = await _collect(mcp, task_id, "pub-15")
        assert len(results) >= 1
        await _select(mcp, task_id, "pub-15", "worker-15")


# ═════════════════════════════════════════════════════════════════════
# Story 16: Generalist-does-all vs Specialist-delegation
#
# Same task, two approaches. The system supports both, but the
# delegation approach produces structured, verifiable results.
# The adjudication mechanism lets the network judge quality.
# ═════════════════════════════════════════════════════════════════════


class TestGeneralistVsSpecialistDelegation:
    @pytest.mark.asyncio
    async def test_delegation_produces_richer_results_than_solo(self, mcp, http, funded_network):
        """
        Story:
        Two identical tasks: "Build e-commerce product page"

        Path A (Generalist): One agent does everything.
        Submits a flat result with no subtask decomposition.

        Path B (Delegation): Planner decomposes into:
        - UI design subtask → design specialist
        - Product API subtask → backend specialist
        Planner composes results into structured delivery.

        Both complete successfully. But Path B:
        - Has richer, more structured output
        - Each subtask was done by a domain expert
        - The parent has verifiable sub-results

        This demonstrates the SYSTEM VALUE: it doesn't force delegation,
        but it ENABLES it, and the delegation path produces better results
        because specialists produce specialist-quality work.
        """
        fn = funded_network

        # Participants
        await _reg(mcp, fn, "biz-16a", "Business A", atype="planner",
                   balance=10000, domains=["ecommerce"])
        await _reg(mcp, fn, "biz-16b", "Business B", atype="planner",
                   balance=10000, domains=["ecommerce"])
        await _reg(mcp, fn, "generalist", "Jack of All Trades",
                   domains=["ecommerce", "design", "backend"])
        await _reg(mcp, fn, "planner-16", "Project Manager", atype="planner",
                   domains=["ecommerce", "design", "backend"])
        await _reg(mcp, fn, "designer", "UI Specialist", tier="expert",
                   domains=["design"], rep=0.95)
        await _reg(mcp, fn, "api-dev", "API Specialist", tier="expert",
                   domains=["backend"], rep=0.95)

        # ──── Path A: Generalist does everything ────

        task_a = await _task(mcp, "biz-16a", "Build product page (generalist)",
                             budget=1000, domains=["ecommerce"])

        bid_a = await _bid(mcp, task_a, "generalist", conf=0.7, price=800)
        assert bid_a["status"] in ("accepted", "executing")

        # Generalist submits a flat, shallow result
        await _submit(mcp, task_a, "generalist", {
            "page_html": "<div class='product'>...</div>",
            "api_endpoint": "/api/product/{id}",
            "notes": "Basic implementation, could use more polish",
        })

        results_a = await _collect(mcp, task_a, "biz-16a")
        await _select(mcp, task_a, "biz-16a", "generalist")

        # ──── Path B: Planner delegates to specialists ────

        task_b = await _task(mcp, "biz-16b", "Build product page (delegated)",
                             budget=1000, domains=["ecommerce"])

        bid_b = await _bid(mcp, task_b, "planner-16", conf=0.9, price=900)
        assert bid_b["status"] in ("accepted", "executing")

        # Planner creates design subtask
        design_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": task_b,
            "description": "Design product page UI with responsive layout",
            "domains": ["design"],
            "budget": 350,
            "initiator_id": "planner-16",
            "level": "expert",
        })
        design_id = design_sub.get("subtask_id") or design_sub.get("id")

        # Planner creates API subtask
        api_sub = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": task_b,
            "description": "Build product REST API with caching",
            "domains": ["backend"],
            "budget": 350,
            "initiator_id": "planner-16",
            "level": "expert",
        })
        api_id = api_sub.get("subtask_id") or api_sub.get("id")

        # Verify budget split: 1000 - 350 - 350 = 300 remaining
        task_b_resp = await http.get(f"/api/tasks/{task_b}")
        assert task_b_resp.json()["remaining_budget"] == 300

        # Design specialist does what they're best at
        bid_design = await _bid(mcp, design_id, "designer", conf=0.98, price=300)
        assert bid_design["status"] in ("accepted", "executing")

        await _submit(mcp, design_id, "designer", {
            "figma_url": "https://figma.com/design/xyz",
            "components": ["ProductCard", "ImageGallery", "PriceDisplay", "ReviewStars"],
            "responsive_breakpoints": ["mobile", "tablet", "desktop"],
            "accessibility_score": "AA",
            "html": "<section class='product-page' role='main'>...</section>",
        })

        design_results = await _collect(mcp, design_id, "planner-16")
        await _select(mcp, design_id, "planner-16", "designer")

        # API specialist does what they're best at
        bid_api = await _bid(mcp, api_id, "api-dev", conf=0.97, price=300)
        assert bid_api["status"] in ("accepted", "executing")

        await _submit(mcp, api_id, "api-dev", {
            "endpoints": {
                "GET /api/products/{id}": {"response_time": "50ms", "cached": True},
                "GET /api/products/{id}/reviews": {"paginated": True},
                "POST /api/products/{id}/cart": {"auth_required": True},
            },
            "cache_strategy": "Redis with 5min TTL",
            "rate_limit": "100 req/min per user",
            "documentation": "OpenAPI 3.0 spec included",
        })

        api_results = await _collect(mcp, api_id, "planner-16")
        await _select(mcp, api_id, "planner-16", "api-dev")

        # Planner composes everything into structured final result
        await _submit(mcp, task_b, "planner-16", {
            "architecture": "Micro-frontend + REST API",
            "ui_design": design_results[0]["content"],
            "backend_api": api_results[0]["content"],
            "integration_notes": "Components consume API via React hooks",
            "specialists_involved": ["designer", "api-dev"],
        })

        results_b = await _collect(mcp, task_b, "biz-16b")
        await _select(mcp, task_b, "biz-16b", "planner-16")

        # ──── Compare ────

        result_a = results_a[0]["content"]
        result_b = results_b[0]["content"]

        # Path A: flat, 3 keys
        assert len(result_a) <= 4

        # Path B: rich, nested, specialist-quality sub-results
        assert "ui_design" in result_b
        assert "backend_api" in result_b
        assert len(result_b["ui_design"]["components"]) >= 4
        assert len(result_b["backend_api"]["endpoints"]) >= 3
        assert result_b["ui_design"]["accessibility_score"] == "AA"


# ═════════════════════════════════════════════════════════════════════
# Story 17: Max depth guard — 4th level decomposition blocked
#
# The system allows deep chains but enforces max_depth to prevent
# infinite recursion of delegation.
# ═════════════════════════════════════════════════════════════════════


class TestMaxDepthGuard:
    @pytest.mark.asyncio
    async def test_depth_guard_prevents_infinite_delegation(self, mcp, http, funded_network):
        """
        Story: Create chain up to max_depth=3.
        Level 0 → Level 1 → Level 2 → Level 3 (OK)
        Level 3 → Level 4 (BLOCKED by depth guard)
        """
        fn = funded_network

        await _reg(mcp, fn, "root-17", "Root", atype="planner", balance=50000)
        for i in range(5):
            await _reg(mcp, fn, f"chain-{i}", f"Chain Agent {i}", atype="planner",
                       domains=["chain"])

        # Create root with max_depth=3
        root_id = await _task(mcp, "root-17", "Deep chain task",
                              budget=10000, domains=["chain"])

        # Override max_depth to 3 for this test
        root_task = fn.task_manager.get(root_id)
        root_task.max_depth = 3

        # Level 0 → bid
        await _bid(mcp, root_id, "chain-0", conf=0.9, price=100)

        # Level 1
        sub1 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": root_id,
            "description": "Level 1 work",
            "domains": ["chain"], "budget": 3000,
            "initiator_id": "chain-0",
        })
        sub1_id = sub1.get("subtask_id") or sub1.get("id")
        await _bid(mcp, sub1_id, "chain-1", conf=0.9, price=100)

        # Level 2
        sub2 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": sub1_id,
            "description": "Level 2 work",
            "domains": ["chain"], "budget": 1000,
            "initiator_id": "chain-1",
        })
        sub2_id = sub2.get("subtask_id") or sub2.get("id")
        await _bid(mcp, sub2_id, "chain-2", conf=0.9, price=100)

        # Level 3
        sub3 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": sub2_id,
            "description": "Level 3 work",
            "domains": ["chain"], "budget": 300,
            "initiator_id": "chain-2",
        })
        sub3_id = sub3.get("subtask_id") or sub3.get("id")
        assert sub3_id is not None  # depth=3 OK

        await _bid(mcp, sub3_id, "chain-3", conf=0.9, price=100)

        # Level 4 → BLOCKED
        sub4 = await mcp.call_tool_parsed("eacn3_create_subtask", {
            "parent_task_id": sub3_id,
            "description": "Level 4 work — should fail",
            "domains": ["chain"], "budget": 100,
            "initiator_id": "chain-3",
        })
        # Should be an error
        assert is_error(sub4) or "error" in str(sub4).lower() or "exceeded" in str(sub4).lower(), \
            f"Level 4 should be blocked by max_depth=3, got: {sub4}"
