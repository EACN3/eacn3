"""TaskManager unit tests — direct state machine and data structure tests.

Tests TaskManager in isolation (no HTTP, no Network layer):
- State machine transitions
- Bid management
- Result management
- Subtask depth guards
- Auto-collect logic
- Deadline scanning
- Tree operations
"""

import pytest
from eacn.core.models import Task, TaskStatus, TaskType, TaskLevel, Bid, BidStatus, Result
from eacn.core.exceptions import TaskError, BudgetError
from eacn.network.task_manager import TaskManager


@pytest.fixture
def tm():
    return TaskManager()


def _make_task(task_id="t1", budget=100.0, **kwargs) -> Task:
    return Task(
        id=task_id, content={"desc": "test"}, initiator_id="user1",
        domains=["coding"], budget=budget, **kwargs,
    )


class TestCreate:
    def test_create_basic(self, tm):
        task = _make_task()
        created = tm.create(task)
        assert created.id == "t1"
        assert created.remaining_budget == 100.0

    def test_create_duplicate_raises(self, tm):
        tm.create(_make_task())
        with pytest.raises(TaskError, match="already exists"):
            tm.create(_make_task())

    def test_create_sets_remaining_budget(self, tm):
        task = _make_task(budget=500.0)
        created = tm.create(task)
        assert created.remaining_budget == 500.0


class TestStateMachine:
    def test_transition_unclaimed_to_bidding(self, tm):
        tm.create(_make_task())
        tm.transition("t1", TaskStatus.BIDDING)
        assert tm.get("t1").status == TaskStatus.BIDDING

    def test_transition_bidding_to_awaiting(self, tm):
        tm.create(_make_task())
        tm.transition("t1", TaskStatus.BIDDING)
        tm.transition("t1", TaskStatus.AWAITING_RETRIEVAL)
        assert tm.get("t1").status == TaskStatus.AWAITING_RETRIEVAL

    def test_invalid_transition_raises(self, tm):
        tm.create(_make_task())
        with pytest.raises(TaskError, match="Invalid transition"):
            tm.transition("t1", TaskStatus.COMPLETED)

    def test_completed_is_terminal(self, tm):
        tm.create(_make_task())
        tm.transition("t1", TaskStatus.BIDDING)
        tm.transition("t1", TaskStatus.AWAITING_RETRIEVAL)
        tm.transition("t1", TaskStatus.COMPLETED)
        with pytest.raises(TaskError, match="Invalid transition"):
            tm.transition("t1", TaskStatus.BIDDING)


class TestBidManagement:
    def test_add_bid_transitions_to_bidding(self, tm):
        tm.create(_make_task())
        bid = Bid(agent_id="a1", confidence=0.9, price=80.0)
        status = tm.add_bid("t1", bid)
        assert status == BidStatus.EXECUTING
        assert tm.get("t1").status == TaskStatus.BIDDING

    def test_second_bid_waits_when_full(self, tm):
        tm.create(_make_task(max_concurrent_bidders=1))
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        status = tm.add_bid("t1", Bid(agent_id="a2", confidence=0.8, price=70.0))
        assert status == BidStatus.WAITING

    def test_budget_locked_when_full(self, tm):
        tm.create(_make_task(max_concurrent_bidders=1))
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        assert tm.get("t1").budget_locked is True

    def test_reject_bid(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.reject_bid("t1", "a1")
        bid = tm.get("t1").bids[0]
        assert bid.status == BidStatus.REJECTED

    def test_reject_nonexistent_raises(self, tm):
        tm.create(_make_task())
        with pytest.raises(TaskError, match="not found"):
            tm.reject_bid("t1", "ghost")


class TestPromoteFromQueue:
    def test_promote_waiting_to_executing(self, tm):
        tm.create(_make_task(max_concurrent_bidders=1))
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_bid("t1", Bid(agent_id="a2", confidence=0.8, price=70.0))
        tm.reject_bid("t1", "a1")
        promoted = tm.promote_from_queue("t1")
        assert promoted == "a2"
        assert tm.get("t1").bids[1].status == BidStatus.EXECUTING

    def test_promote_none_when_empty_queue(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        assert tm.promote_from_queue("t1") is None


class TestResultManagement:
    def test_add_result(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_result("t1", Result(agent_id="a1", content="done"))
        assert len(tm.get("t1").results) == 1

    def test_add_result_validates_bidder(self, tm):
        tm.create(_make_task())
        # Need at least one bid to move to BIDDING status first
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        with pytest.raises(TaskError, match="not an active bidder"):
            tm.add_result("t1", Result(agent_id="ghost", content="sneaky"))

    def test_select_result(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_result("t1", Result(agent_id="a1", content="done"))
        selected = tm.select_result("t1", "a1")
        assert selected.selected is True
        assert tm.get("t1").status == TaskStatus.COMPLETED

    def test_select_nonexistent_raises(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        with pytest.raises(TaskError, match="No result from"):
            tm.select_result("t1", "ghost")


class TestAutoCollect:
    def test_auto_collect_at_threshold(self, tm):
        tm.create(_make_task(max_concurrent_bidders=2))
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_bid("t1", Bid(agent_id="a2", confidence=0.8, price=70.0))
        tm.add_result("t1", Result(agent_id="a1", content="r1"))
        assert tm.check_auto_collect("t1") is False  # 1 < 2
        tm.add_result("t1", Result(agent_id="a2", content="r2"))
        assert tm.check_auto_collect("t1") is True  # 2 >= 2
        assert tm.get("t1").status == TaskStatus.AWAITING_RETRIEVAL

    def test_auto_collect_already_collected(self, tm):
        tm.create(_make_task(max_concurrent_bidders=1))
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_result("t1", Result(agent_id="a1", content="r1"))
        tm.check_auto_collect("t1")
        # Second call returns False (already transitioned)
        assert tm.check_auto_collect("t1") is False


class TestSubtask:
    def test_create_subtask(self, tm):
        tm.create(_make_task(budget=500.0, max_depth=5))
        sub = tm.create_subtask("t1", {"desc": "sub"}, ["coding"], 100.0, "a1")
        assert sub.depth == 1
        assert sub.parent_id == "t1"
        assert sub.id in tm.get("t1").child_ids
        assert tm.get("t1").remaining_budget == 400.0

    def test_subtask_depth_limit(self, tm):
        tm.create(_make_task(budget=500.0, max_depth=2))
        sub = tm.create_subtask("t1", {}, ["coding"], 200.0, "a1")
        with pytest.raises(TaskError, match="Max depth"):
            tm.create_subtask(sub.id, {}, ["coding"], 50.0, "a2")

    def test_subtask_over_budget(self, tm):
        tm.create(_make_task(budget=100.0))
        with pytest.raises(BudgetError, match="exceeds parent"):
            tm.create_subtask("t1", {}, ["coding"], 200.0, "a1")


class TestDeadlineScanning:
    def test_scan_expired(self, tm):
        tm.create(_make_task(task_id="exp1", deadline="2020-01-01T00:00:00Z"))
        tm.create(_make_task(task_id="exp2", deadline="2030-01-01T00:00:00Z"))
        expired = tm.scan_expired()
        ids = [t.id for t in expired]
        assert "exp1" in ids
        assert "exp2" not in ids

    def test_scan_z_suffix(self, tm):
        tm.create(_make_task(task_id="z1", deadline="2020-06-15T12:00:00Z"))
        expired = tm.scan_expired()
        assert any(t.id == "z1" for t in expired)

    def test_scan_plus_offset(self, tm):
        tm.create(_make_task(task_id="p1", deadline="2020-06-15T12:00:00+00:00"))
        expired = tm.scan_expired()
        assert any(t.id == "p1" for t in expired)

    def test_handle_expired_with_results(self, tm):
        tm.create(_make_task(task_id="hr1"))
        tm.add_bid("hr1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_result("hr1", Result(agent_id="a1", content="done"))
        status = tm.handle_expired("hr1")
        assert status == TaskStatus.AWAITING_RETRIEVAL

    def test_handle_expired_without_results(self, tm):
        tm.create(_make_task(task_id="hr2"))
        status = tm.handle_expired("hr2")
        assert status == TaskStatus.NO_ONE_ABLE


class TestTreeOps:
    def test_get_subtree(self, tm):
        tm.create(_make_task(task_id="root", budget=500.0, max_depth=5))
        sub1 = tm.create_subtask("root", {}, ["coding"], 100.0, "a1")
        sub2 = tm.create_subtask("root", {}, ["coding"], 100.0, "a2")
        tree = tm.get_subtree("root")
        ids = [t.id for t in tree]
        assert "root" in ids
        assert sub1.id in ids
        assert sub2.id in ids

    def test_get_root(self, tm):
        tm.create(_make_task(task_id="root", budget=500.0, max_depth=5))
        sub = tm.create_subtask("root", {}, ["coding"], 100.0, "a1")
        root = tm.get_root(sub.id)
        assert root.id == "root"

    def test_close_task_with_results(self, tm):
        tm.create(_make_task())
        tm.add_bid("t1", Bid(agent_id="a1", confidence=0.9, price=80.0))
        tm.add_result("t1", Result(agent_id="a1", content="done"))
        task = tm.close_task("t1")
        assert task.status == TaskStatus.AWAITING_RETRIEVAL

    def test_close_task_without_results(self, tm):
        tm.create(_make_task())
        task = tm.close_task("t1")
        assert task.status == TaskStatus.NO_ONE_ABLE

    def test_discussions(self, tm):
        tm.create(_make_task())
        tm.update_discussions("t1", "hello", author="user1")
        task = tm.get("t1")
        assert len(task.content["discussions"]) == 1
        assert task.content["discussions"][0]["author"] == "user1"
