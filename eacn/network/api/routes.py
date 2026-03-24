"""Network HTTP API routes — wraps Network orchestration layer."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query

from eacn.core.exceptions import TaskError, BudgetError
from eacn.core.models import TaskStatus
from eacn.network.api.schemas import (
    CreateTaskRequest, TaskResponse,
    SubmitBidRequest, BidResponse,
    SubmitResultRequest, SelectResultRequest,
    RejectTaskRequest,
    CreateSubtaskRequest, ConfirmBudgetRequest,
    CloseTaskRequest,
    UpdateDiscussionsRequest, UpdateDeadlineRequest,
    ReputationEventRequest, ReputationResponse,
    BalanceResponse, DepositRequest, DepositResponse,
    RelayMessageRequest,
    InviteAgentRequest, InviteAgentResponse,
    OkResponse,
)

router = APIRouter(prefix="/api", tags=["network"])

_network = None


def set_network(network) -> None:
    global _network
    _network = network


def _net():
    if _network is None:
        raise HTTPException(503, "Network not initialized")
    return _network


def _task_to_response(task) -> TaskResponse:
    return TaskResponse(**task.model_dump())



@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(req: CreateTaskRequest):
    try:
        from eacn.core.models import HumanContact
        human_contact = None
        if req.human_contact:
            human_contact = HumanContact(**req.human_contact.model_dump())
        task = await _net().create_task(
            task_id=req.task_id,
            initiator_id=req.initiator_id,
            content=req.content,
            domains=req.domains,
            budget=req.budget,
            deadline=req.deadline,
            max_concurrent_bidders=req.max_concurrent_bidders,
            max_depth=req.max_depth,
            human_contact=human_contact,
            level=req.level,
            invited_agent_ids=req.invited_agent_ids,
        )
        return _task_to_response(task)
    except BudgetError as e:
        raise HTTPException(402, str(e))
    except TaskError as e:
        raise HTTPException(409, str(e))


# NOTE: /tasks/open MUST be before /tasks/{task_id} to avoid path parameter capture
@router.get("/tasks/open", response_model=list[TaskResponse])
async def list_open_tasks(
    domains: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List open tasks available for bidding (status=unclaimed or bidding with slots)."""
    tasks = _net().task_manager.list_all()
    open_tasks = [
        t for t in tasks
        if t.status.value in ("unclaimed", "bidding") and not t.concurrent_slots_full
    ]
    if domains:
        domain_set = set(domains.split(","))
        open_tasks = [t for t in open_tasks if set(t.domains) & domain_set]
    open_tasks = open_tasks[offset: offset + limit]
    return [_task_to_response(t) for t in open_tasks]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    try:
        task = _net().task_manager.get(task_id)
        return _task_to_response(task)
    except TaskError as e:
        raise HTTPException(404, str(e))


@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str, agent_id: str):
    """Agent queries a task they initiated (validates initiator_id == agent_id).

    Returns task status info WITHOUT results and adjudications.
    """
    try:
        task = _net().task_manager.get(task_id)
    except TaskError as e:
        raise HTTPException(404, str(e))

    if task.initiator_id != agent_id:
        raise HTTPException(403, "Only the task initiator can query task status")

    return {
        "id": task.id,
        "status": task.status.value,
        "initiator_id": task.initiator_id,
        "domains": task.domains,
        "budget": task.budget,
        "deadline": task.deadline,
        "type": task.type.value,
        "depth": task.depth,
        "parent_id": task.parent_id,
        "child_ids": task.child_ids,
        "bids": [b.model_dump() for b in task.bids],
    }


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = None,
    initiator_id: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    tasks = _net().task_manager.list_all()
    if status:
        tasks = [t for t in tasks if t.status.value == status]
    if initiator_id:
        tasks = [t for t in tasks if t.initiator_id == initiator_id]
    tasks = tasks[offset: offset + limit]
    return [_task_to_response(t) for t in tasks]



@router.post("/tasks/{task_id}/bid", response_model=BidResponse)
async def submit_bid(task_id: str, req: SubmitBidRequest):
    net = _net()
    # Cluster: check if task is on a remote node
    if not net.cluster.router.is_local(task_id):
        try:
            result = await net.cluster.router.forward_bid(
                task_id, req.agent_id, req.server_id, req.confidence, req.price,
            )
            return BidResponse(
                status=result.get("status", "rejected"),
                task_id=task_id,
                agent_id=req.agent_id,
            )
        except Exception as e:
            raise HTTPException(502, f"Forward failed: {e}")
    try:
        bid_status = await net.submit_bid(
            task_id=task_id,
            agent_id=req.agent_id,
            confidence=req.confidence,
            price=req.price,
            server_id=req.server_id,
        )
        return BidResponse(
            status=bid_status.value, task_id=task_id, agent_id=req.agent_id,
        )
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/invite", response_model=InviteAgentResponse)
async def invite_agent(task_id: str, req: InviteAgentRequest):
    """Invite an agent to bid on a task (skips ability check)."""
    try:
        await _net().invite_agent(
            task_id=task_id,
            initiator_id=req.initiator_id,
            agent_id=req.agent_id,
        )
        return InviteAgentResponse(
            task_id=task_id,
            agent_id=req.agent_id,
            message="Agent invited",
        )
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/reject", response_model=OkResponse)
async def reject_task(task_id: str, req: RejectTaskRequest):
    """Agent rejects/withdraws from an assigned task for re-allocation."""
    net = _net()
    if not net.cluster.router.is_local(task_id):
        try:
            await net.cluster.router.forward_reject(task_id, req.agent_id)
            return OkResponse(message="Task rejected (forwarded)")
        except Exception as e:
            raise HTTPException(502, f"Forward failed: {e}")
    try:
        await net.reject_task(
            task_id=task_id,
            agent_id=req.agent_id,
            reason=req.reason,
        )
        return OkResponse(message="Task rejected, slot freed")
    except TaskError as e:
        raise HTTPException(400, str(e))



@router.post("/tasks/{task_id}/result", response_model=OkResponse)
async def submit_result(task_id: str, req: SubmitResultRequest):
    net = _net()
    if not net.cluster.router.is_local(task_id):
        try:
            await net.cluster.router.forward_result(task_id, req.agent_id, req.content)
            return OkResponse(message="Result submitted (forwarded)")
        except Exception as e:
            raise HTTPException(502, f"Forward failed: {e}")
    try:
        await net.submit_result(
            task_id=task_id,
            agent_id=req.agent_id,
            content=req.content,
        )
        return OkResponse(message="Result submitted")
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/select", response_model=OkResponse)
async def select_result(task_id: str, req: SelectResultRequest):
    try:
        await _net().select_result(
            task_id=task_id,
            agent_id=req.agent_id,
            initiator_id=req.initiator_id,
        )
        return OkResponse(message="Result selected, settlement done")
    except (TaskError, BudgetError) as e:
        raise HTTPException(400, str(e))


@router.get("/tasks/{task_id}/results")
async def get_task_results(task_id: str, initiator_id: str):
    """Initiator collects results and adjudications.

    Preconditions:
    - Caller must be the task initiator
    - Task status must be awaiting_retrieval or completed
    - First call transitions task from awaiting_retrieval → completed
    """
    try:
        task = _net().task_manager.get(task_id)
    except TaskError as e:
        raise HTTPException(404, str(e))

    if task.initiator_id != initiator_id:
        raise HTTPException(403, "Only the task initiator can collect results")

    if task.status not in (TaskStatus.AWAITING_RETRIEVAL, TaskStatus.COMPLETED):
        raise HTTPException(
            400,
            f"Cannot collect results in status {task.status.value}; "
            "task must be in awaiting_retrieval or completed",
        )

    results = await _net().collect_results(task_id)

    # Flatten all adjudications across results
    all_adjudications = []
    for r in results:
        for adj in r.adjudications:
            all_adjudications.append({
                "result_agent_id": r.agent_id,
                **adj.model_dump(),
            })

    return {
        "results": [r.model_dump() for r in results],
        "adjudications": all_adjudications,
    }



@router.post("/tasks/{task_id}/close", response_model=TaskResponse)
async def close_task(task_id: str, req: CloseTaskRequest):
    try:
        task = await _net().close_task(task_id, initiator_id=req.initiator_id)
        return _task_to_response(task)
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.put("/tasks/{task_id}/deadline", response_model=TaskResponse)
async def update_deadline(task_id: str, req: UpdateDeadlineRequest):
    try:
        task = await _net().update_deadline(
            task_id, req.deadline, initiator_id=req.initiator_id,
        )
        return _task_to_response(task)
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/discussions", response_model=TaskResponse)
async def update_discussions(task_id: str, req: UpdateDiscussionsRequest):
    try:
        task = await _net().update_discussions(
            task_id, req.message, initiator_id=req.initiator_id,
        )
        return _task_to_response(task)
    except TaskError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/confirm-budget", response_model=OkResponse)
async def confirm_budget(task_id: str, req: ConfirmBudgetRequest):
    try:
        await _net().confirm_budget(
            task_id,
            initiator_id=req.initiator_id,
            approved=req.approved,
            new_budget=req.new_budget,
        )
        return OkResponse(message="Budget confirmed")
    except (TaskError, BudgetError) as e:
        raise HTTPException(400, str(e))



@router.post("/tasks/{task_id}/subtask", response_model=TaskResponse, status_code=201)
async def create_subtask(task_id: str, req: CreateSubtaskRequest):
    net = _net()
    if not net.cluster.router.is_local(task_id):
        try:
            result = await net.cluster.router.forward_subtask(
                task_id,
                {
                    "initiator_id": req.initiator_id,
                    "content": req.content,
                    "domains": req.domains,
                    "budget": req.budget,
                    "deadline": req.deadline,
                },
            )
            return TaskResponse(
                id=result.get("subtask_id", ""),
                status=result.get("status", "unclaimed"),
                initiator_id=req.initiator_id,
                domains=req.domains,
                budget=req.budget,
            )
        except Exception as e:
            raise HTTPException(502, f"Forward failed: {e}")
    try:
        sub = await net.create_subtask(
            parent_task_id=task_id,
            initiator_id=req.initiator_id,
            content=req.content,
            domains=req.domains,
            budget=req.budget,
            deadline=req.deadline,
            level=req.level,
        )
        return _task_to_response(sub)
    except (TaskError, BudgetError) as e:
        raise HTTPException(400, str(e))



@router.post("/reputation/events", response_model=ReputationResponse)
async def receive_reputation_event(req: ReputationEventRequest):
    score = await _net().receive_reputation_event(
        req.agent_id, req.event_type, req.server_id,
    )
    return ReputationResponse(agent_id=req.agent_id, score=score)


@router.get("/reputation/{agent_id}", response_model=ReputationResponse)
async def get_reputation(agent_id: str):
    score = _net().reputation.get_score(agent_id)
    return ReputationResponse(agent_id=agent_id, score=score)



# ── Economy (2 endpoints) ────────────────────────────────────────────

@router.get("/economy/balance", response_model=BalanceResponse)
async def get_balance(agent_id: str = Query(...)):
    """Query an agent's account balance (available + frozen)."""
    net = _net()
    account = net.escrow.get_account(agent_id)
    if not account:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return BalanceResponse(
        agent_id=agent_id,
        available=account.available,
        frozen=account.frozen,
    )


@router.post("/economy/deposit", response_model=DepositResponse)
async def deposit(req: DepositRequest):
    """Deposit funds into an agent's account."""
    net = _net()
    account = net.escrow.get_or_create_account(req.agent_id, 0.0)
    account.credit(req.amount)
    return DepositResponse(
        agent_id=req.agent_id,
        deposited=req.amount,
        available=account.available,
        frozen=account.frozen,
    )


# ── Messaging (1 endpoint) ───────────────────────────────────────────

@router.post("/messages")
async def relay_message(req: RelayMessageRequest):
    """Relay a direct message to a target agent via three-layer addressing.

    Routes by to.agent_id: if the agent is connected locally via WebSocket,
    deliver immediately. Otherwise forward to peer nodes via /peer/message.
    """
    from eacn.core.models import PushEvent, PushEventType
    from eacn.network.api.websocket import manager

    net = _net()
    target_agent_id = req.to.agent_id
    sender_agent_id = req.from_.agent_id

    # Build push event
    event = PushEvent(
        type=PushEventType.DIRECT_MESSAGE,
        task_id="",
        recipients=[target_agent_id],
        payload={
            "from": sender_agent_id,
            "content": req.content,
            "to": req.to.model_dump(),
        },
    )

    # Try local delivery first
    if manager.is_connected(target_agent_id):
        delivered = await manager.broadcast_event(event)
        return {"ok": True, "delivered": delivered, "method": "local"}

    # Forward to all peer nodes — they'll try local delivery
    if not net.cluster.standalone:
        members = net.cluster.members.all_online(exclude=net.cluster.node_id)
        if members:
            target_nodes = {m.node_id for m in members}
            await net.cluster.router.forward_push(
                event.type.value,
                event.task_id,
                event.recipients,
                event.payload,
                target_nodes,
            )
            return {"ok": True, "delivered": 0, "method": "forwarded"}

    return {"ok": False, "delivered": 0, "method": "undeliverable",
            "error": f"Agent {target_agent_id} not connected to any node"}


@router.get("/cluster/status")
async def cluster_status():
    """View cluster status: local node info, all known members, cluster mode."""
    net = _net()
    cluster = net.cluster
    local = cluster.local_node
    members = cluster.members.all_nodes()
    agent_counts = cluster.get_agent_counts()
    return {
        "mode": "standalone" if cluster.standalone else "cluster",
        "local": {
            "node_id": local.node_id,
            "endpoint": local.endpoint,
            "domains": local.domains,
            "status": local.status,
            "version": local.version,
            "joined_at": local.joined_at,
        },
        "members": [
            {
                "node_id": n.node_id,
                "endpoint": n.endpoint,
                "domains": n.domains,
                "status": n.status,
                "last_seen": n.last_seen,
                "connected_agents": agent_counts.get(n.node_id, 0),
            }
            for n in members
        ],
        "member_count": len(members),
        "online_count": len(cluster.members.all_online()),
        "seed_nodes": list(cluster.config.seed_nodes),
    }


@router.get("/admin/config")
async def get_config():
    """Read all current hyperparameters."""
    return _net().config.model_dump()


@router.put("/admin/config")
async def update_config(patch: dict):
    """Partially update hyperparameters and persist to config.toml.

    Example: {"reputation": {"max_gain": 0.2}, "economy": {"platform_fee_rate": 0.03}}
    Auto-reloads affected modules and writes to config.toml.
    """
    from eacn.network.config import NetworkConfig, save_config, _deep_merge

    net = _net()
    current = net.config.model_dump()

    for key in patch:
        if key not in current:
            raise HTTPException(400, f"Unknown config key: {key}")

    _deep_merge(current, patch)

    new_config = NetworkConfig(**current)
    net.config = new_config

    # Reload affected modules (preserve state, update config)
    net.reputation.update_config(new_config.reputation)
    net.matcher = type(net.matcher)(config=new_config.matcher)
    net.push.MAX_RETRIES = new_config.push.max_retries
    net.settlement.platform_fee_rate = new_config.economy.platform_fee_rate

    # Persist
    save_config(new_config)

    return new_config.model_dump()



@router.post("/admin/scan-deadlines")
async def scan_deadlines(now: str | None = None):
    expired_ids = await _net().scan_deadlines(now)
    return {"expired": expired_ids}



@router.post("/admin/fund")
async def fund_account(agent_id: str = Body(...), amount: float = Body(...)):
    """Admin: credit an agent's account for testing."""
    account = _net().escrow.get_or_create_account(agent_id, 0.0)
    account.credit(amount)
    return {"agent_id": agent_id, "available": account.available, "frozen": account.frozen}


@router.get("/admin/offline-stats")
async def offline_stats():
    """Query offline message queue stats per agent."""
    from eacn.network.api.websocket import manager
    store = manager._offline_store
    if not store:
        return {"agents": {}, "total": 0}
    counts = await store.count_all()
    return {"agents": counts, "total": sum(counts.values())}


@router.get("/admin/logs")
async def query_logs(
    task_id: str | None = None,
    agent_id: str | None = None,
    fn_name: str | None = None,
    limit: int = Query(default=50, le=500),
):
    entries = _net().logger.get_entries(
        task_id=task_id, agent_id=agent_id, fn_name=fn_name,
    )
    return [e.model_dump() for e in entries[:limit]]
