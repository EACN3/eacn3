"""Discovery HTTP API routes — Server/Agent lifecycle + discovery queries.

Covers three interaction patterns:
1. Server lifecycle: register → heartbeat → unregister (cascade cleanup)
2. Agent lifecycle: register (Bootstrap + DHT) → update → unregister
3. Discovery query: domain lookup (Gossip → DHT → Bootstrap) + card resolution
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from eacn.network.api.schemas import (
    RegisterServerRequest, RegisterServerResponse, ServerCardResponse,
    RegisterAgentRequest, RegisterAgentResponse, AgentCardResponse,
    UpdateAgentRequest,
    DiscoverResponse,
    OkResponse,
)
from eacn.network.auth import (
    register_agent_token, revoke_agent_token,
    register_server_token, revoke_server_token,
    extract_agent_token, validate_agent_token,
    validate_server_token,
)

discovery_router = APIRouter(prefix="/api/discovery", tags=["discovery"])

import asyncio

_network = None
# Prevent concurrent server unregistration TOCTOU races (#101)
_unregister_locks: dict[str, asyncio.Lock] = {}


def set_discovery_network(network) -> None:
    global _network
    _network = network


def _net():
    if _network is None:
        raise HTTPException(503, "Network not initialized")
    return _network


# ══════════════════════════════════════════════════════════════════════
# Server lifecycle
# ══════════════════════════════════════════════════════════════════════

@discovery_router.post("/servers", response_model=RegisterServerResponse, status_code=201)
async def register_server(req: RegisterServerRequest):
    """New server registers with the network. Returns assigned server_id."""
    server_id = f"srv-{uuid4().hex[:12]}"
    await _net().discovery.register_server(
        server_id=server_id,
        version=req.version,
        endpoint=req.endpoint,
        owner=req.owner,
    )
    token = register_server_token(server_id)
    return RegisterServerResponse(server_id=server_id, status="online", token=token)


@discovery_router.get("/servers/{server_id}", response_model=ServerCardResponse)
async def get_server(server_id: str):
    """Get server card by server_id."""
    card = await _net().discovery.bootstrap.get_server_card(server_id)
    if not card:
        raise HTTPException(404, f"Server {server_id} not found")
    return ServerCardResponse(**card)


@discovery_router.post("/servers/{server_id}/heartbeat", response_model=OkResponse)
async def server_heartbeat(server_id: str):
    """Server heartbeat — marks server and its agents as online.

    If the server is alive, its agents are reachable. Refresh their
    last_fetch_at so the liveness scanner doesn't kill them while the
    host LLM is busy (e.g. executing a long task without polling events).
    """
    card = await _net().discovery.bootstrap.get_server_card(server_id)
    if not card:
        raise HTTPException(404, f"Server {server_id} not found")
    await _net().discovery.bootstrap.set_server_status(server_id, "online")
    # Keep agents alive too — heartbeat proves the server (and its agents) are reachable
    try:
        await _net().db.touch_agents_by_server(server_id)
    except Exception:
        pass  # best-effort; don't fail the heartbeat
    return OkResponse(message="heartbeat ok")


@discovery_router.delete("/servers/{server_id}", response_model=OkResponse)
async def unregister_server(server_id: str):
    """Unregister server — cascade: revoke all its agents from DHT + delete server card."""
    # Per-server lock to prevent TOCTOU races (#101)
    if server_id not in _unregister_locks:
        _unregister_locks[server_id] = asyncio.Lock()
    async with _unregister_locks[server_id]:
        return await _unregister_server_inner(server_id)

async def _unregister_server_inner(server_id: str):
    card = await _net().discovery.bootstrap.get_server_card(server_id)
    if not card:
        raise HTTPException(404, f"Server {server_id} not found")
    # Revoke domains of all agents belonging to this server from cluster
    server_agent_ids = await _net().discovery.bootstrap.get_agent_ids_by_server(server_id)
    server_agent_set = set(server_agent_ids)
    for aid in server_agent_ids:
        agent_card = await _net().discovery.bootstrap.get_agent_card(aid)
        if agent_card:
            for domain in agent_card.get("domains", []):
                others = await _net().discovery.bootstrap._db.query_agent_cards_by_domain(domain)
                still_used = any(
                    c["agent_id"] not in server_agent_set for c in others
                )
                if not still_used:
                    await _net().cluster.revoke_domain(domain)

    await _net().discovery.unregister_server(server_id)
    revoke_server_token(server_id)
    # Revoke tokens for all agents of this server
    for aid in server_agent_ids:
        revoke_agent_token(aid)
    return OkResponse(message=f"Server {server_id} unregistered, agents cascade removed")


# ══════════════════════════════════════════════════════════════════════
# Agent lifecycle
# ══════════════════════════════════════════════════════════════════════

@discovery_router.post("/agents", response_model=RegisterAgentResponse, status_code=201)
async def register_agent(req: RegisterAgentRequest):
    """Register agent: store AgentCard in Bootstrap + announce domains to DHT.

    Returns seed list (same-domain agents for gossip bootstrapping).
    """
    # Validate server exists
    server_card = await _net().discovery.bootstrap.get_server_card(req.server_id)
    if not server_card:
        raise HTTPException(400, f"Server {req.server_id} not registered")

    card = {
        "agent_id": req.agent_id,
        "name": req.name,
        "domains": req.domains,
        "skills": [s.model_dump() for s in req.skills],
        "url": req.url,
        "server_id": req.server_id,
        "description": req.description,
        "tier": req.tier,
    }
    seeds = await _net().discovery.register_agent(card)

    # Sync new domains to cluster DHT so peer nodes know we handle them (#76)
    try:
        for domain in req.domains:
            await _net().cluster.announce_domain(domain)
    except Exception:
        # Rollback discovery registration on cluster failure
        import logging
        logging.getLogger(__name__).warning(
            "Cluster announce failed for agent %s; rolling back registration",
            req.agent_id, exc_info=True,
        )
        await _net().discovery.unregister_agent(req.agent_id)
        raise HTTPException(502, "Agent registration failed: cluster announcement error")

    token = register_agent_token(req.agent_id)
    return RegisterAgentResponse(agent_id=req.agent_id, seeds=seeds, token=token)


@discovery_router.get("/agents/{agent_id}", response_model=AgentCardResponse)
async def get_agent(agent_id: str):
    """Get agent card by agent_id (for push delivery chain resolution)."""
    card = await _net().discovery.bootstrap.get_agent_card(agent_id)
    if not card:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return AgentCardResponse(**card)


@discovery_router.put("/agents/{agent_id}", response_model=OkResponse)
async def update_agent(agent_id: str, req: UpdateAgentRequest):
    """Update agent card fields. Re-announces to DHT if domains changed."""
    card = await _net().discovery.bootstrap.get_agent_card(agent_id)
    if not card:
        raise HTTPException(404, f"Agent {agent_id} not found")

    # Validate server still exists (#102)
    server_card = await _net().discovery.bootstrap.get_server_card(card.get("server_id", ""))
    if not server_card:
        raise HTTPException(400, f"Server {card.get('server_id')} no longer registered")

    old_domains = set(card.get("domains", []))

    # Apply partial updates
    if req.name is not None:
        card["name"] = req.name
    if req.domains is not None:
        card["domains"] = req.domains
    if req.skills is not None:
        card["skills"] = [s.model_dump() for s in req.skills]
    if req.url is not None:
        card["url"] = req.url
    if req.description is not None:
        card["description"] = req.description
    if req.tier is not None:
        card["tier"] = req.tier

    # Persist updated card
    await _net().discovery.bootstrap._db.save_agent_card(card)

    # Re-announce DHT if domains changed
    new_domains = set(card.get("domains", []))
    if new_domains != old_domains:
        # Revoke removed domains from both discovery DHT and cluster DHT (#75)
        for d in old_domains - new_domains:
            await _net().discovery.dht.revoke(d, agent_id)
            # Check if any other agent still uses this domain before cluster revoke
            others = await _net().discovery.bootstrap._db.query_agent_cards_by_domain(d)
            still_used = any(c["agent_id"] != agent_id for c in others)
            if not still_used:
                await _net().cluster.revoke_domain(d)
        # Announce new domains to both discovery and cluster DHT
        for d in new_domains - old_domains:
            await _net().discovery.dht.announce(d, agent_id)
            await _net().cluster.announce_domain(d)
        # Also re-check retained domains for cluster consistency (#75)
        for d in new_domains & old_domains:
            # Ensure cluster DHT still has this domain
            await _net().cluster.announce_domain(d)

    return OkResponse(message="Agent updated")


@discovery_router.delete("/agents/{agent_id}", response_model=OkResponse)
async def unregister_agent(agent_id: str):
    """Unregister agent: revoke from DHT + delete card from Bootstrap."""
    card = await _net().discovery.bootstrap.get_agent_card(agent_id)
    if not card:
        raise HTTPException(404, f"Agent {agent_id} not found")
    # Revoke agent's domains from cluster if no other local agent uses them
    for domain in card.get("domains", []):
        others = await _net().discovery.bootstrap._db.query_agent_cards_by_domain(domain)
        still_used = any(c["agent_id"] != agent_id for c in others)
        if not still_used:
            await _net().cluster.revoke_domain(domain)

    await _net().discovery.unregister_agent(agent_id)
    revoke_agent_token(agent_id)
    return OkResponse(message=f"Agent {agent_id} unregistered")


# ══════════════════════════════════════════════════════════════════════
# Discovery queries
# ══════════════════════════════════════════════════════════════════════

@discovery_router.get("/query", response_model=DiscoverResponse)
async def discover_agents(
    domain: str,
    requester_id: str | None = None,
):
    """Three-layer fallback discovery: Gossip → DHT → Bootstrap.

    If requester_id is provided, gossip (local knowledge) is checked first.
    """
    agent_ids = await _net().discovery.discover(domain, requester_id=requester_id)
    return DiscoverResponse(domain=domain, agent_ids=agent_ids)


@discovery_router.get("/agents", response_model=list[AgentCardResponse])
async def list_agents_by_domain(
    domain: str | None = None,
    server_id: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List agents, optionally filtered by domain or server_id."""
    if domain:
        # Use DHT for domain lookup, then fetch cards
        agent_ids = await _net().discovery.dht.lookup(domain)
        cards = []
        for aid in agent_ids:
            card = await _net().discovery.bootstrap.get_agent_card(aid)
            if card:
                if server_id and card.get("server_id") != server_id:
                    continue
                cards.append(card)
    elif server_id:
        agent_ids = await _net().discovery.bootstrap.get_agent_ids_by_server(server_id)
        cards = []
        for aid in agent_ids:
            card = await _net().discovery.bootstrap.get_agent_card(aid)
            if card:
                cards.append(card)
    else:
        raise HTTPException(400, "At least one of 'domain' or 'server_id' is required")

    cards = cards[offset: offset + limit]
    return [AgentCardResponse(**c) for c in cards]
