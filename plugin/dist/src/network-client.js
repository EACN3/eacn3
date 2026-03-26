/**
 * HTTP client for EACN3 network endpoints (28 APIs).
 *
 * Each method maps 1:1 to a network-api.md endpoint.
 * server_id is injected from local state — callers don't need to pass it.
 */
import { getState, getServerId } from "./state.js";
// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------
function baseUrl() {
    return getState().network_endpoint;
}
function serverId() {
    const id = getServerId();
    if (!id)
        throw new Error("Not connected. Call eacn3_connect first.");
    return id;
}
async function request(method, path, body, query) {
    let url = `${baseUrl()}${path}`;
    if (query) {
        const params = new URLSearchParams(Object.entries(query).filter(([, v]) => v !== undefined && v !== ""));
        const qs = params.toString();
        if (qs)
            url += `?${qs}`;
    }
    const headers = {
        "Content-Type": "application/json",
    };
    // Inject server_id header for authenticated requests
    const sid = getServerId();
    if (sid)
        headers["x-server-id"] = sid;
    const res = await fetch(url, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`${method} ${path} → ${res.status}: ${text}`);
    }
    return (await res.json());
}
// ---------------------------------------------------------------------------
// Health / Cluster (2)
// ---------------------------------------------------------------------------
/**
 * Probe a network endpoint for health. Uses a short timeout so it can be
 * used for fast fail-over. If `endpoint` is omitted, probes the current
 * configured endpoint.
 */
export async function checkHealth(endpoint) {
    const url = `${endpoint ?? baseUrl()}/health`;
    const res = await fetch(url, {
        method: "GET",
        signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) {
        throw new Error(`GET /health → ${res.status}`);
    }
    return (await res.json());
}
/**
 * Get cluster topology: members, seed nodes, online count.
 */
export async function getClusterStatus(endpoint) {
    const url = `${endpoint ?? baseUrl()}/api/cluster/status`;
    const res = await fetch(url, {
        method: "GET",
        signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) {
        throw new Error(`GET /api/cluster/status → ${res.status}`);
    }
    return (await res.json());
}
/**
 * Try to find a healthy endpoint. Probes the primary endpoint first, then
 * falls back to seed nodes discovered from cluster status.
 * Returns the first reachable endpoint URL.
 */
export async function findHealthyEndpoint(primary, seeds) {
    // Try primary first
    try {
        await checkHealth(primary);
        return primary;
    }
    catch { /* primary down, try seeds */ }
    // Try known seeds
    const candidates = seeds ?? [];
    for (const seed of candidates) {
        if (seed === primary)
            continue;
        try {
            await checkHealth(seed);
            return seed;
        }
        catch { /* try next */ }
    }
    // Last resort: try to get cluster info from primary (may have partial connectivity)
    try {
        const cluster = await getClusterStatus(primary);
        for (const member of cluster.members) {
            if (member.endpoint === primary || member.status !== "online")
                continue;
            try {
                await checkHealth(member.endpoint);
                return member.endpoint;
            }
            catch { /* try next */ }
        }
    }
    catch { /* no cluster info available */ }
    throw new Error(`No healthy endpoint found. Tried: ${primary}${candidates.length ? `, ${candidates.join(", ")}` : ""}`);
}
// ---------------------------------------------------------------------------
// Discovery — Server (4)
// ---------------------------------------------------------------------------
export async function registerServer(version, endpoint, owner) {
    return request("POST", "/api/discovery/servers", {
        version,
        endpoint,
        owner,
    });
}
export async function getServer(sid) {
    return request("GET", `/api/discovery/servers/${sid}`);
}
export async function heartbeat() {
    return request("POST", `/api/discovery/servers/${serverId()}/heartbeat`);
}
export async function unregisterServer() {
    return request("DELETE", `/api/discovery/servers/${serverId()}`);
}
// ---------------------------------------------------------------------------
// Discovery — Agent (6)
// ---------------------------------------------------------------------------
export async function registerAgent(agent) {
    return request("POST", "/api/discovery/agents", agent);
}
export async function getAgentInfo(agentId) {
    return request("GET", `/api/discovery/agents/${agentId}`);
}
export async function updateAgent(agentId, updates) {
    return request("PUT", `/api/discovery/agents/${agentId}`, updates);
}
export async function unregisterAgent(agentId) {
    return request("DELETE", `/api/discovery/agents/${agentId}`);
}
export async function discoverAgents(domain, requesterId) {
    const query = { domain };
    if (requesterId)
        query.requester_id = requesterId;
    return request("GET", "/api/discovery/query", undefined, query);
}
export async function listAgentsRemote(opts) {
    const query = {};
    if (opts.domain)
        query.domain = opts.domain;
    if (opts.server_id)
        query.server_id = opts.server_id;
    if (opts.limit !== undefined)
        query.limit = String(opts.limit);
    if (opts.offset !== undefined)
        query.offset = String(opts.offset);
    return request("GET", "/api/discovery/agents", undefined, query);
}
// ---------------------------------------------------------------------------
// Tasks — Query (5)
// ---------------------------------------------------------------------------
export async function createTask(task) {
    return request("POST", "/api/tasks", task);
}
export async function getOpenTasks(opts) {
    const query = {};
    if (opts?.domains)
        query.domains = opts.domains;
    if (opts?.limit !== undefined)
        query.limit = String(opts.limit);
    if (opts?.offset !== undefined)
        query.offset = String(opts.offset);
    return request("GET", "/api/tasks/open", undefined, query);
}
export async function getTask(taskId) {
    return request("GET", `/api/tasks/${taskId}`);
}
export async function getTaskStatus(taskId, agentId) {
    return request("GET", `/api/tasks/${taskId}/status`, undefined, {
        agent_id: agentId,
    });
}
export async function listTasks(opts) {
    const query = {};
    if (opts?.status)
        query.status = opts.status;
    if (opts?.initiator_id)
        query.initiator_id = opts.initiator_id;
    if (opts?.limit !== undefined)
        query.limit = String(opts.limit);
    if (opts?.offset !== undefined)
        query.offset = String(opts.offset);
    return request("GET", "/api/tasks", undefined, query);
}
// ---------------------------------------------------------------------------
// Tasks — Initiator (7)
// ---------------------------------------------------------------------------
export async function getTaskResults(taskId, initiatorId) {
    return request("GET", `/api/tasks/${taskId}/results`, undefined, { initiator_id: initiatorId });
}
export async function selectResult(taskId, initiatorId, agentId, closeTask = false) {
    return request("POST", `/api/tasks/${taskId}/select`, {
        initiator_id: initiatorId,
        agent_id: agentId,
        close_task: closeTask,
    });
}
export async function closeTask(taskId, initiatorId) {
    return request("POST", `/api/tasks/${taskId}/close`, {
        initiator_id: initiatorId,
    });
}
export async function updateDeadline(taskId, initiatorId, deadline) {
    return request("PUT", `/api/tasks/${taskId}/deadline`, {
        initiator_id: initiatorId,
        deadline,
    });
}
export async function updateDiscussions(taskId, initiatorId, message) {
    return request("POST", `/api/tasks/${taskId}/discussions`, {
        initiator_id: initiatorId,
        message,
    });
}
export async function confirmBudget(taskId, initiatorId, approved, newBudget) {
    const body = {
        initiator_id: initiatorId,
        approved,
    };
    if (newBudget !== undefined)
        body.new_budget = newBudget;
    return request("POST", `/api/tasks/${taskId}/confirm-budget`, body);
}
// ---------------------------------------------------------------------------
// Tasks — Executor (4)
// ---------------------------------------------------------------------------
export async function submitBid(taskId, agentId, confidence, price) {
    return request("POST", `/api/tasks/${taskId}/bid`, {
        agent_id: agentId,
        confidence,
        price,
        server_id: serverId(),
    });
}
export async function submitResult(taskId, agentId, content) {
    return request("POST", `/api/tasks/${taskId}/result`, {
        agent_id: agentId,
        content,
    });
}
export async function rejectTask(taskId, agentId, reason) {
    const body = { agent_id: agentId };
    if (reason)
        body.reason = reason;
    return request("POST", `/api/tasks/${taskId}/reject`, body);
}
export async function createSubtask(parentTaskId, initiatorId, content, domains, budget, deadline, level) {
    const body = {
        initiator_id: initiatorId,
        content,
        domains,
        budget,
    };
    if (deadline)
        body.deadline = deadline;
    if (level)
        body.level = level;
    return request("POST", `/api/tasks/${parentTaskId}/subtask`, body);
}
// ---------------------------------------------------------------------------
// Reputation (2)
// ---------------------------------------------------------------------------
export async function reportEvent(agentId, eventType) {
    return request("POST", "/api/reputation/events", {
        agent_id: agentId,
        event_type: eventType,
        server_id: serverId(),
    });
}
export async function getReputation(agentId) {
    return request("GET", `/api/reputation/${agentId}`);
}
// ---------------------------------------------------------------------------
// Economy (2)
// ---------------------------------------------------------------------------
export async function getBalance(agentId) {
    return request("GET", `/api/economy/balance`, undefined, { agent_id: agentId });
}
export async function deposit(agentId, amount) {
    return request("POST", `/api/economy/deposit`, { agent_id: agentId, amount });
}
// ---------------------------------------------------------------------------
// Tasks — Invite (1)
// ---------------------------------------------------------------------------
export async function inviteAgent(taskId, initiatorId, agentId) {
    return request("POST", `/api/tasks/${taskId}/invite`, { initiator_id: initiatorId, agent_id: agentId });
}
/**
 * Send a direct message via Network relay.
 * The Network node routes by three-layer addressing and delivers via WebSocket.
 */
export async function relayMessage(msg) {
    return request("POST", "/api/messages", msg);
}
//# sourceMappingURL=network-client.js.map