"""Tests: Adjudication task full lifecycle.

Covers: auto-creation of adjudication tasks / correct properties / no cascading
adjudication / adjudication bids skip budget confirmation / auto-collection.
"""

import pytest
from tests.api.conftest import create_task, bid, submit_result, close_task


class TestAdjudicationCreation:
    @pytest.mark.asyncio
    async def test_created_on_normal_result(self, client):
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = [t for t in all_tasks if t["type"] == "adjudication"]
        assert len(adj) >= 1

    @pytest.mark.asyncio
    async def test_adjudication_properties(self, client):
        await create_task(client, task_id="t1", budget=500.0, domains=["coding"])
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        assert adj["budget"] == 0.0
        assert adj["parent_id"] == "t1"
        assert adj["domains"] == ["coding"]
        assert adj["status"] == "unclaimed"

    @pytest.mark.asyncio
    async def test_no_cascading_adjudication(self, client):
        """Submitting result for an adjudication task should not create a new adjudication."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # Find the adjudication task
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        adj_id = adj["id"]

        # Bid on adjudication task + submit result
        await bid(client, task_id=adj_id, agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(client, task_id=adj_id, agent_id="a2", content="approved")

        # Should not have created a new adjudication task
        all_after = (await client.get("/api/tasks")).json()
        adj_after = [t for t in all_after if t["type"] == "adjudication"]
        assert len(adj_after) == 1  # still only one

    @pytest.mark.asyncio
    async def test_adjudication_bid_no_price_check(self, client):
        """Adjudication task bids should skip price check (budget=0)."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")

        b = await bid(client, task_id=adj["id"], agent_id="a2", confidence=0.9, price=0.0)
        assert b["status"] == "executing"

    @pytest.mark.asyncio
    async def test_multiple_results_multiple_adjudications(self, client):
        """Multiple results should each produce an adjudication task."""
        await create_task(client, task_id="t1", budget=500.0, max_concurrent_bidders=3)
        await bid(client, task_id="t1", agent_id="a1")
        await bid(client, task_id="t1", agent_id="a2")
        await submit_result(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a2")

        all_tasks = (await client.get("/api/tasks")).json()
        adj = [t for t in all_tasks if t["type"] == "adjudication"]
        assert len(adj) >= 2


class TestAdjudicationAutoCollection:
    @pytest.mark.asyncio
    async def test_adjudication_result_collected_to_parent(self, client):
        """Adjudication results should auto-populate parent task Result's adjudications."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # Find the adjudication task
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        adj_id = adj["id"]

        # Bid on adjudication task + submit adjudication result
        await bid(client, task_id=adj_id, agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(
            client, task_id=adj_id, agent_id="a2",
            content="approved",
        )

        # Check parent task result's adjudications
        parent = (await client.get("/api/tasks/t1")).json()
        result = parent["results"][0]
        assert len(result["adjudications"]) >= 1
        assert result["adjudications"][0]["adjudicator_id"] == "a2"

    @pytest.mark.asyncio
    async def test_adjudication_in_collect_results_response(self, client):
        """get_task_results should include adjudications field."""
        await create_task(client, task_id="t1")
        await bid(client, task_id="t1", agent_id="a1")
        await submit_result(client, task_id="t1", agent_id="a1")

        # Adjudicate
        all_tasks = (await client.get("/api/tasks")).json()
        adj = next(t for t in all_tasks if t["type"] == "adjudication")
        await bid(client, task_id=adj["id"], agent_id="a2", confidence=0.9, price=0.0)
        await submit_result(client, task_id=adj["id"], agent_id="a2", content="approved")

        # Close and collect
        await close_task(client, task_id="t1")
        resp = await client.get("/api/tasks/t1/results", params={"initiator_id": "user1"})
        data = resp.json()
        assert "adjudications" in data
        assert len(data["adjudications"]) >= 1
