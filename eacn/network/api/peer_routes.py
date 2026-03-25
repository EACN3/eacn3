"""Peer-to-peer routes for cluster node communication.

All endpoints use the /peer/ prefix and are NOT exposed to Servers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

peer_router = APIRouter(prefix="/peer", tags=["peer"])

_cluster = None
_network = None


def set_peer_cluster(cluster) -> None:
    global _cluster
    _cluster = cluster


def set_peer_network(network) -> None:
    global _network
    _network = network


def _cs():
    if _cluster is None:
        raise HTTPException(503, "Cluster not initialized")
    return _cluster


def _net():
    if _network is None:
        raise HTTPException(503, "Network not initialized")
    return _network


# ── Schemas ──────────────────────────────────────────────────────────

class OkResponse(BaseModel):
    ok: bool = True


class JoinRequest(BaseModel):
    node_card: dict[str, Any]


class LeaveRequest(BaseModel):
    node_id: str


class HeartbeatRequest(BaseModel):
    node_id: str
    domains: list[str] = Field(default_factory=list)
    timestamp: str
    connected_agents: int = 0


class DHTStoreRequest(BaseModel):
    domain: str = Field(min_length=1)
    node_id: str = Field(min_length=1)


class DHTRevokeRequest(BaseModel):
    domain: str = Field(min_length=1)
    node_id: str = Field(min_length=1)


class GossipExchangeRequest(BaseModel):
    from_node: dict[str, Any]
    known: list[dict[str, Any]] = Field(default_factory=list)


class TaskBroadcastRequest(BaseModel):
    task_id: str
    origin: str
    initiator_id: str
    domains: list[str] = Field(default_factory=list)
    type: str = "normal"
    budget: float = 0.0
    deadline: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    max_concurrent_bidders: int = 5
    level: str | None = None
    invited_agent_ids: list[str] = Field(default_factory=list)


class TaskBidRequest(BaseModel):
    task_id: str
    agent_id: str
    server_id: str = ""
    confidence: float
    price: float
    from_node: str


class TaskRejectRequest(BaseModel):
    task_id: str
    agent_id: str
    from_node: str


class TaskResultRequest(BaseModel):
    task_id: str
    agent_id: str
    content: Any
    from_node: str


class TaskSubtaskRequest(BaseModel):
    parent_task_id: str
    subtask_data: dict[str, Any]
    from_node: str


class TaskStatusRequest(BaseModel):
    task_id: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PushRequest(BaseModel):
    type: str
    task_id: str
    recipients: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Membership (3 endpoints) ────────────────────────────────────────

@peer_router.post("/join")
async def peer_join(req: JoinRequest):
    from eacn.network.cluster.node import NodeCard
    try:
        card = NodeCard.from_dict(req.node_card)
        nodes = _cs().handle_join(card)
        return {"nodes": [n.to_dict() for n in nodes]}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(409, str(e))


@peer_router.post("/leave", response_model=OkResponse)
async def peer_leave(req: LeaveRequest):
    _cs().handle_leave(req.node_id)
    return OkResponse()


@peer_router.post("/heartbeat", response_model=OkResponse)
async def peer_heartbeat(req: HeartbeatRequest):
    cs = _cs()
    if not cs.members.contains(req.node_id):
        raise HTTPException(404, f"Node {req.node_id} not found")
    cs.handle_heartbeat(req.node_id, req.domains, req.timestamp,
                        connected_agents=req.connected_agents)
    return OkResponse()


# ── DHT (3 endpoints) ───────────────────────────────────────────────

@peer_router.post("/dht/store", response_model=OkResponse)
async def peer_dht_store(req: DHTStoreRequest):
    await _cs().dht.handle_store(req.domain, req.node_id)
    return OkResponse()


@peer_router.delete("/dht/revoke", response_model=OkResponse)
async def peer_dht_revoke(req: DHTRevokeRequest):
    await _cs().dht.handle_revoke(req.domain, req.node_id)
    return OkResponse()


@peer_router.get("/dht/lookup")
async def peer_dht_lookup(domain: str):
    node_ids = await _cs().dht.handle_lookup(domain)
    return {"domain": domain, "node_ids": node_ids}


# ── Gossip (1 endpoint) ─────────────────────────────────────────────

@peer_router.post("/gossip/exchange")
async def peer_gossip_exchange(req: GossipExchangeRequest):
    from eacn.network.cluster.node import NodeCard
    from_node = NodeCard.from_dict(req.from_node)
    known_cards = [NodeCard.from_dict(n) for n in req.known]
    result_cards = await _cs().gossip.handle_exchange(from_node, known_cards)
    return {"known": [c.to_dict() for c in result_cards]}


# ── Task forwarding (7 endpoints) ───────────────────────────────────

@peer_router.post("/task/broadcast", response_model=OkResponse)
async def peer_task_broadcast(req: TaskBroadcastRequest):
    cs = _cs()
    if cs.router.get_route(req.task_id) is not None:
        return OkResponse()  # Idempotent

    cs.handle_broadcast(req.model_dump())
    # Track the origin node as a participant so subsequent push events
    # (BID_RESULT, DISCUSSION_UPDATE, etc.) can be forwarded back to it
    cs.router.add_participant(req.task_id, req.origin)

    net = _net()
    all_agent_ids: set[str] = set()
    for domain in req.domains:
        all_agent_ids.update(await net.discovery.discover(domain))

    if all_agent_ids:
        from eacn.core.models import Task, TaskType, TaskLevel
        task_type = TaskType(req.type) if req.type else TaskType.NORMAL
        task_level = TaskLevel(req.level) if req.level else TaskLevel.GENERAL
        task = Task(
            id=req.task_id, content=req.content, initiator_id=req.initiator_id,
            domains=req.domains, budget=req.budget, deadline=req.deadline,
            max_concurrent_bidders=req.max_concurrent_bidders,
            type=task_type, level=task_level,
            invited_agent_ids=req.invited_agent_ids,
        )
        await net.push.broadcast_task(task, list(all_agent_ids))
    return OkResponse()


@peer_router.post("/task/bid")
async def peer_task_bid(req: TaskBidRequest):
    from eacn.core.exceptions import TaskError, BudgetError
    net, cs = _net(), _cs()
    try:
        bid_status = await net.submit_bid(
            task_id=req.task_id, agent_id=req.agent_id,
            confidence=req.confidence, price=req.price,
            server_id=req.server_id or None,
        )
        cs.router.add_participant(req.task_id, req.from_node)
        return {"status": bid_status.value,
                "bid": {"agent_id": req.agent_id, "status": bid_status.value}}
    except (TaskError, BudgetError) as e:
        raise HTTPException(400, str(e))


@peer_router.post("/task/reject", response_model=OkResponse)
async def peer_task_reject(req: TaskRejectRequest):
    from eacn.core.exceptions import TaskError
    try:
        await _net().reject_task(task_id=req.task_id, agent_id=req.agent_id)
        return OkResponse()
    except TaskError as e:
        raise HTTPException(400, str(e))


@peer_router.post("/task/result", response_model=OkResponse)
async def peer_task_result(req: TaskResultRequest):
    from eacn.core.exceptions import TaskError
    try:
        await _net().submit_result(
            task_id=req.task_id, agent_id=req.agent_id, content=req.content,
        )
        return OkResponse()
    except TaskError as e:
        raise HTTPException(400, str(e))


@peer_router.post("/task/subtask")
async def peer_task_subtask(req: TaskSubtaskRequest):
    from eacn.core.exceptions import TaskError, BudgetError
    data = req.subtask_data
    try:
        subtask = await _net().create_subtask(
            parent_task_id=req.parent_task_id,
            initiator_id=data.get("initiator_id", ""),
            content=data.get("content", {}),
            domains=data.get("domains", []),
            budget=data.get("budget", 0.0),
            deadline=data.get("deadline"),
            level=data.get("level"),
        )
        return {"subtask_id": subtask.id, "status": subtask.status.value}
    except (TaskError, BudgetError) as e:
        raise HTTPException(400, str(e))


@peer_router.post("/task/status", response_model=OkResponse)
async def peer_task_status(req: TaskStatusRequest):
    await _cs().handle_status_notification(req.task_id, req.status, req.payload)
    return OkResponse()


@peer_router.post("/push")
async def peer_push(req: PushRequest):
    delivered = await _cs().handle_push(
        req.type, req.task_id, req.recipients, req.payload,
    )
    return {"ok": True, "delivered": delivered}
