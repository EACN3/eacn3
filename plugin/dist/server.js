/**
 * EACN3 MCP Server — exposes 38 tools via stdio transport.
 *
 * All intelligence lives in Skills (host LLM). This server is just
 * state management + network API wrapper. No adapter, no registry —
 * everything is inline.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { SSEServerTransport } from "@modelcontextprotocol/sdk/server/sse.js";
import http from "http";
import { z } from "zod";
import { EACN3_DEFAULT_NETWORK_ENDPOINT } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as ws from "./src/event-transport.js";
import * as a2a from "./src/a2a-server.js";
import * as rc from "./src/reverse-control.js";
// ---------------------------------------------------------------------------
// Helper: MCP text result
// ---------------------------------------------------------------------------
function ok(data) {
    const result = {
        content: [{ type: "text", text: JSON.stringify(data) }],
    };
    // Fallback directive injection: when sampling is unavailable,
    // append pending event directives to any tool response so the
    // Host LLM sees actionable events without explicit polling.
    const directives = rc.drainDirectives();
    if (directives) {
        result.content.push({ type: "text", text: directives });
    }
    return result;
}
function err(message) {
    return { content: [{ type: "text", text: JSON.stringify({ error: message }) }] };
}
/** Log MCP tool calls to stderr for traceability. */
function logToolCall(toolName, params) {
    const ts = new Date().toISOString();
    console.error(`[MCP] ${ts} CALL ${toolName} params=${JSON.stringify(params)}`);
}
function logToolResult(toolName, success, detail) {
    const ts = new Date().toISOString();
    const tag = success ? "OK" : "ERR";
    console.error(`[MCP] ${ts} ${tag}  ${toolName}${detail ? ` ${detail}` : ""}`);
}
/**
 * Resolve agent ID: use provided value, or auto-inject from state.
 * If only one agent is registered, use it. Otherwise throw.
 * Per agent.md:116 — "agent_id is auto-filled by the communication layer; agents need not provide it"
 */
function resolveAgentId(provided) {
    if (provided)
        return provided;
    const agents = state.listAgents();
    if (agents.length === 1)
        return agents[0].agent_id;
    if (agents.length === 0)
        throw new Error("No agents registered. Call eacn3_register_agent first.");
    throw new Error(`Multiple agents registered (${agents.map(a => a.agent_id).join(", ")}). Specify agent_id explicitly.`);
}
// ---------------------------------------------------------------------------
// Heartbeat background interval
// ---------------------------------------------------------------------------
let heartbeatInterval = null;
function startHeartbeat() {
    if (heartbeatInterval)
        return;
    heartbeatInterval = setInterval(async () => {
        try {
            await net.heartbeat();
        }
        catch { /* silent */ }
    }, 60_000);
}
function stopHeartbeat() {
    if (heartbeatInterval) {
        clearInterval(heartbeatInterval);
        heartbeatInterval = null;
    }
}
// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------
const server = new McpServer({ name: "eacn3", version: "0.3.0" });
// ═══════════════════════════════════════════════════════════════════════════
// Health / Cluster (2)
// ═══════════════════════════════════════════════════════════════════════════
// #0a eacn3_health
server.tool("eacn3_health", "Check if a network node is alive and responding. No prerequisites — works before eacn3_connect. Returns {status: 'ok'} on success. Use this to verify an endpoint before connecting.", {
    endpoint: z.string().optional().describe("Node URL to probe. Defaults to configured network endpoint."),
}, async (params) => {
    const target = params.endpoint ?? state.getState().network_endpoint;
    try {
        const health = await net.checkHealth(target);
        return ok({ endpoint: target, ...health });
    }
    catch (e) {
        return err(`Health check failed for ${target}: ${e.message}`);
    }
});
// #0b eacn3_cluster_status
server.tool("eacn3_cluster_status", "Retrieve the full cluster topology including all member nodes, their online/offline status, and seed URLs. No prerequisites — works before eacn3_connect. Returns array of node objects with status and endpoint fields. Useful for diagnostics and finding alternative endpoints if primary is down.", {
    endpoint: z.string().optional().describe("Node URL to query. Defaults to configured network endpoint."),
}, async (params) => {
    const target = params.endpoint ?? state.getState().network_endpoint;
    try {
        const cluster = await net.getClusterStatus(target);
        return ok(cluster);
    }
    catch (e) {
        return err(`Cluster status failed for ${target}: ${e.message}`);
    }
});
// ═══════════════════════════════════════════════════════════════════════════
// Server Management (4)
// ═══════════════════════════════════════════════════════════════════════════
// #1 eacn3_connect
server.tool("eacn3_connect", "Connect to the EACN3 network — this must be your FIRST call. Health-probes the endpoint, falls back to seed nodes if unreachable, registers a server, and starts a background heartbeat every 60s. Returns {server_id, network_endpoint, fallback, agents_online, restored_agents, hint}. Side effects: starts event polling for any previously registered agents. IMPORTANT: check restored_agents in the response — if you have previously registered agents, they are already reconnected and ready to use. You do NOT need to re-register them. Only call eacn3_register_agent if you need a NEW agent.", {
    network_endpoint: z.string().optional().describe(`Network URL. Defaults to ${EACN3_DEFAULT_NETWORK_ENDPOINT}`),
    seed_nodes: z.array(z.string()).optional().describe("Additional seed node URLs for fallback"),
}, async (params) => {
    const preferred = params.network_endpoint ?? EACN3_DEFAULT_NETWORK_ENDPOINT;
    const s = state.getState();
    // Health probe + fallback
    let endpoint;
    let fallback = false;
    try {
        endpoint = await net.findHealthyEndpoint(preferred, params.seed_nodes);
        fallback = endpoint !== preferred;
    }
    catch (e) {
        return err(`Cannot reach any network node: ${e.message}`);
    }
    s.network_endpoint = endpoint;
    // Reuse existing server identity if available; otherwise register new
    let sid;
    if (s.server_card) {
        // Try to reconnect with existing server_id via heartbeat
        try {
            await net.heartbeat();
            sid = s.server_card.server_id;
            s.server_card.status = "online";
        }
        catch {
            // Server no longer known to network — re-register
            const res = await net.registerServer("0.3.0", "plugin://local", "plugin-user");
            sid = res.server_id;
            s.server_card = {
                server_id: sid,
                version: "0.3.0",
                endpoint: "plugin://local",
                owner: "plugin-user",
                status: "online",
            };
            // Update server_id on all persisted agents and re-register them with the network
            for (const agent of Object.values(s.agents)) {
                agent.server_id = sid;
                try {
                    await net.registerAgent(agent);
                }
                catch { /* best-effort */ }
            }
        }
    }
    else {
        const res = await net.registerServer("0.3.0", "plugin://local", "plugin-user");
        sid = res.server_id;
        s.server_card = {
            server_id: sid,
            version: "0.3.0",
            endpoint: "plugin://local",
            owner: "plugin-user",
            status: "online",
        };
    }
    state.save();
    // Start background heartbeat
    startHeartbeat();
    // Reconnect WS for all existing agents; re-register if network lost them
    for (const agent of Object.values(s.agents)) {
        try {
            await net.getAgentInfo(agent.agent_id);
        }
        catch {
            // Agent not found on network (e.g. server restarted with in-memory DB)
            try {
                await net.registerAgent(agent);
            }
            catch { /* best-effort */ }
        }
        ws.connect(agent.agent_id);
    }
    const restoredAgents = Object.values(s.agents).map((a) => ({
        agent_id: a.agent_id,
        name: a.name,
        domains: a.domains,
        tier: a.tier,
    }));
    return ok({
        connected: true,
        server_id: sid,
        network_endpoint: endpoint,
        fallback,
        agents_online: restoredAgents.length,
        restored_agents: restoredAgents,
        hint: restoredAgents.length > 0
            ? "You have previously registered agents restored and reconnected. You can use them directly without re-registering. Call eacn3_list_my_agents() for full details."
            : "No previous agents found. Register a new agent with eacn3_register_agent().",
    });
});
// #2 eacn3_disconnect
server.tool("eacn3_disconnect", "Disconnect from the EACN3 network and stop event polling. Requires: eacn3_connect first. Side effects: active tasks will timeout and hurt reputation. Server identity and agent registrations are preserved — on next eacn3_connect they will be automatically reconnected. Returns {disconnected: true}. Only call at end of session.", {}, async () => {
    stopHeartbeat();
    ws.disconnectAll();
    // Do NOT call unregisterServer — it cascade-deletes all agents on the network side.
    // We only go offline; identity is preserved for reconnection.
    const s = state.getState();
    if (s.server_card) {
        s.server_card.status = "offline";
    }
    state.save();
    return ok({ disconnected: true });
});
// #3 eacn3_heartbeat
server.tool("eacn3_heartbeat", "Manually send a heartbeat to the network to signal this server is still alive. Requires: eacn3_connect first. Usually unnecessary — a background interval auto-sends every 60s. Only use if you suspect the connection may have gone stale.", {}, async () => {
    const res = await net.heartbeat();
    return ok(res);
});
// #4 eacn3_server_info
server.tool("eacn3_server_info", "Get current server connection state, including server_card, network_endpoint, registered agent IDs, task count, and remote status. Requires: eacn3_connect first. Returns {server_card, network_endpoint, agents_count, agents[], tasks_count, remote_status}. No side effects — read-only diagnostic.", {}, async () => {
    const s = state.getState();
    if (!s.server_card)
        return err("Not connected");
    let remote;
    try {
        remote = await net.getServer(s.server_card.server_id);
    }
    catch {
        remote = null;
    }
    return ok({
        server_card: s.server_card,
        network_endpoint: s.network_endpoint,
        agents_count: Object.keys(s.agents).length,
        agents: Object.keys(s.agents),
        tasks_count: Object.keys(s.local_tasks).length,
        remote_status: remote?.status ?? "unknown",
    });
});
// ═══════════════════════════════════════════════════════════════════════════
// Agent Management (7)
// ═══════════════════════════════════════════════════════════════════════════
// #5 eacn3_register_agent
// Inlines: adapter (AgentCard assembly) + registry (validate + persist + DHT)
server.tool("eacn3_register_agent", "Create and register an agent identity on the EACN3 network. Requires: eacn3_connect first. Assembles an AgentCard, registers it with the network, persists it locally, and starts polling for push events (task_broadcast, subtask_completed, etc.). Returns {agent_id, seeds, domains}. Domains control which task broadcasts you receive — be specific (e.g. 'python-coding' not 'coding').", {
    name: z.string().describe("Agent display name"),
    description: z.string().describe("What this Agent does"),
    domains: z.array(z.string()).describe("Capability domains (e.g. ['translation', 'coding'])"),
    skills: z.array(z.object({
        id: z.string().optional(),
        name: z.string(),
        description: z.string(),
        tags: z.array(z.string()).optional(),
        parameters: z.record(z.string(), z.unknown()).optional(),
    })).optional().describe("Agent skills"),
    capabilities: z.object({
        max_concurrent_tasks: z.number().describe("Max tasks this Agent can handle simultaneously (0 = unlimited)"),
        concurrent: z.boolean().describe("Whether this Agent supports concurrent execution"),
    }).optional().describe("Agent capacity limits"),
    tier: z.enum(["general", "expert", "expert_general", "tool"]).optional().describe("Capability tier: general (can bid on anything) > expert > expert_general > tool (only tool-level tasks). Defaults to general."),
    agent_id: z.string().optional().describe("Custom agent ID. Auto-generated if omitted."),
    a2a_port: z.number().optional().describe("Port for A2A HTTP server. Enables direct agent-to-agent messaging. Omit to use Network relay only."),
    a2a_url: z.string().optional().describe("Full public URL for A2A callbacks (e.g. 'http://my-server.com:3001'). Auto-generated from a2a_port if omitted."),
    reverse_control: z.object({
        enabled: z.boolean().optional().describe("Enable MCP reverse control (sampling/notifications). Default true."),
        sampling_events: z.array(z.string()).optional().describe("Event types that trigger LLM sampling (e.g. ['task_broadcast', 'direct_message']). Default: all actionable events."),
        notification_events: z.array(z.string()).optional().describe("Event types that send notifications only (e.g. ['task_collected', 'task_timeout']). Default: status events."),
    }).optional().describe("Configure MCP reverse control — lets the network proactively drive your agent via sampling requests."),
}, async (params) => {
    const s = state.getState();
    if (!s.server_card)
        return err("Not connected. Call eacn3_connect first.");
    // Validate
    if (!params.name.trim())
        return err("name cannot be empty");
    if (params.domains.length === 0)
        return err("domains cannot be empty");
    const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;
    const sid = s.server_card.server_id;
    // Determine agent URL: real A2A endpoint or local placeholder
    let agentUrl = `plugin://local/agents/${agentId}`;
    if (params.a2a_port || params.a2a_url) {
        const port = params.a2a_port ?? 0;
        const actualPort = await a2a.startServer(port);
        if (params.a2a_url) {
            agentUrl = `${params.a2a_url.replace(/\/$/, "")}/agents/${agentId}`;
        }
        else {
            agentUrl = `http://localhost:${actualPort}/agents/${agentId}`;
        }
    }
    // Assemble AgentCard (what adapter used to do)
    const card = {
        agent_id: agentId,
        name: params.name,
        tier: params.tier ?? "general",
        domains: params.domains,
        skills: params.skills ?? [],
        capabilities: params.capabilities,
        url: agentUrl,
        server_id: sid,
        network_id: "",
        description: params.description,
    };
    // Register with network (what registry used to do)
    const res = await net.registerAgent(card);
    // Persist locally
    state.addAgent(card);
    // Start polling for push events
    ws.connect(agentId);
    // Configure reverse control for this agent
    if (params.reverse_control?.enabled !== false) {
        const rcPolicies = {};
        const samplingEvents = params.reverse_control?.sampling_events ?? ["task_broadcast", "direct_message", "subtask_completed", "bid_request_confirmation", "discussion_update"];
        const notifEvents = params.reverse_control?.notification_events ?? ["task_collected"];
        for (const e of samplingEvents)
            rcPolicies[e] = { method: "sampling" };
        for (const e of notifEvents)
            rcPolicies[e] = { method: "notification" };
        rcPolicies["task_timeout"] = { method: "auto_action", autoAction: "report_and_close" };
        rc.configure(agentId, { enabled: true, policies: rcPolicies });
    }
    const rcStatus = rc.getStatus();
    return ok({
        registered: true,
        agent_id: agentId,
        seeds: res.seeds,
        domains: params.domains,
        url: agentUrl,
        a2a_server: a2a.isRunning() ? { port: a2a.getServerPort() } : null,
        reverse_control: {
            enabled: params.reverse_control?.enabled !== false,
            sampling_available: rcStatus.samplingAvailable,
            fallback: rcStatus.samplingAvailable ? "none" : "directive_injection",
        },
    });
});
// #6 eacn3_get_agent
server.tool("eacn3_get_agent", "Fetch the full AgentCard for any agent by ID — checks local state first, then queries the network. Returns {agent_id, name, domains, skills, capabilities, url, server_id, description}. No side effects. Use to inspect an agent before sending messages or evaluating bids.", {
    agent_id: z.string(),
}, async (params) => {
    // Check local first
    const local = state.getAgent(params.agent_id);
    if (local)
        return ok(local);
    // Fetch from network
    const remote = await net.getAgentInfo(params.agent_id);
    return ok(remote);
});
// #7 eacn3_update_agent
server.tool("eacn3_update_agent", "Update a registered agent's mutable fields: name, domains, skills, and/or description. Requires: the agent must be registered (eacn3_register_agent). Updates both network and local state. Changing domains affects which task broadcasts you receive going forward.", {
    agent_id: z.string(),
    name: z.string().optional(),
    domains: z.array(z.string()).optional(),
    skills: z.array(z.object({
        id: z.string().optional(),
        name: z.string(),
        description: z.string(),
        tags: z.array(z.string()).optional(),
        parameters: z.record(z.string(), z.unknown()).optional(),
    })).optional(),
    description: z.string().optional(),
}, async (params) => {
    const { agent_id, ...updates } = params;
    const res = await net.updateAgent(agent_id, updates);
    // Update local state
    const local = state.getAgent(agent_id);
    if (local) {
        if (updates.name !== undefined)
            local.name = updates.name;
        if (updates.domains !== undefined)
            local.domains = updates.domains;
        if (updates.skills !== undefined)
            local.skills = updates.skills;
        if (updates.description !== undefined)
            local.description = updates.description;
        state.addAgent(local); // re-save
    }
    return ok({ updated: true, agent_id, ...res });
});
// #8 eacn3_unregister_agent
server.tool("eacn3_unregister_agent", "Remove an agent from the network and stop its event polling. Side effects: deletes agent from local state, stops receiving events for this agent. Active tasks assigned to this agent will timeout and hurt reputation. Returns {unregistered: true, agent_id}.", {
    agent_id: z.string(),
}, async (params) => {
    const res = await net.unregisterAgent(params.agent_id);
    ws.disconnect(params.agent_id);
    rc.unconfigure(params.agent_id);
    state.removeAgent(params.agent_id);
    // Stop A2A server if no agents remain
    if (state.listAgents().length === 0 && a2a.isRunning()) {
        await a2a.stopServer();
    }
    return ok({ unregistered: true, agent_id: params.agent_id, ...res });
});
// #9 eacn3_list_my_agents
server.tool("eacn3_list_my_agents", "List all agents registered on this local server instance. Returns {count, agents[]} where each agent includes agent_id, name, domains, tier, and polling_active (event polling status). No network call — reads local state only. Use to check which agents are active and receiving events.", {}, async () => {
    const agents = state.listAgents();
    return ok({
        count: agents.length,
        agents: agents.map((a) => ({
            agent_id: a.agent_id,
            name: a.name,
            domains: a.domains,
            connected: ws.isConnected(a.agent_id),
            transport: ws.getTransportStatus(a.agent_id),
        })),
    });
});
// #10 eacn3_discover_agents
server.tool("eacn3_discover_agents", "Search for agents matching a specific domain using the network's discovery protocol (Gossip, then DHT, then Bootstrap fallback). Requires: eacn3_connect first. Returns a list of matching AgentCards. Use before creating a task to verify executors exist for your domains.", {
    domain: z.string(),
    requester_id: z.string().optional(),
}, async (params) => {
    const res = await net.discoverAgents(params.domain, params.requester_id);
    return ok(res);
});
// #11 eacn3_list_agents
server.tool("eacn3_list_agents", "Browse and paginate all agents registered on the network with optional filters by domain or server_id. Returns {count, agents[]}. Default page size is 20. Unlike eacn3_discover_agents, this is a direct registry query without Gossip/DHT discovery — faster but only returns agents already indexed.", {
    domain: z.string().optional(),
    server_id: z.string().optional(),
    limit: z.number().optional(),
    offset: z.number().optional(),
}, async (params) => {
    const agents = await net.listAgentsRemote(params);
    return ok({ count: agents.length, agents });
});
// ═══════════════════════════════════════════════════════════════════════════
// Task Query (4)
// ═══════════════════════════════════════════════════════════════════════════
// #12 eacn3_get_task
server.tool("eacn3_get_task", "Fetch complete task details from the network including description, content, bids[], results[], status, budget, deadline, and domains. No side effects — read-only. Use to inspect a task before bidding or to review submitted results. Works for any task ID regardless of your role.", {
    task_id: z.string(),
}, async (params) => {
    const task = await net.getTask(params.task_id);
    return ok(task);
});
// #13 eacn3_get_task_status
server.tool("eacn3_get_task_status", "Lightweight task query returning only status and bid list — no result content. Intended for initiators monitoring their tasks. Requires: agent_id must be the task initiator (auto-injected if only one agent registered). Returns {status, bids[]}. Cheaper than eacn3_get_task when you only need status.", {
    task_id: z.string(),
    agent_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const status = await net.getTaskStatus(params.task_id, agentId);
    return ok(status);
});
// #14 eacn3_list_open_tasks
server.tool("eacn3_list_open_tasks", "Browse tasks currently accepting bids (status: unclaimed or bidding). Returns {count, tasks[]} with pagination. Filter by comma-separated domains to find relevant work. Use this in your main loop to discover tasks to bid on after checking events.", {
    domains: z.string().optional().describe("Comma-separated domain filter"),
    limit: z.number().optional(),
    offset: z.number().optional(),
}, async (params) => {
    const tasks = await net.getOpenTasks(params);
    return ok({ count: tasks.length, tasks });
});
// #15 eacn3_list_tasks
server.tool("eacn3_list_tasks", "Browse all tasks on the network with optional filters by status (unclaimed, bidding, awaiting_retrieval, completed, no_one) and/or initiator_id. Returns {count, tasks[]} with pagination. Unlike eacn3_list_open_tasks, this includes tasks in all states.", {
    status: z.string().optional(),
    initiator_id: z.string().optional(),
    limit: z.number().optional(),
    offset: z.number().optional(),
}, async (params) => {
    const tasks = await net.listTasks(params);
    return ok({ count: tasks.length, tasks });
});
// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Initiator (7)
// ═══════════════════════════════════════════════════════════════════════════
// #16 eacn3_create_task
// Inlines matcher: check local agents before hitting network
server.tool("eacn3_create_task", "Publish a new task to the EACN3 network for other agents to bid on. Side effects: freezes 'budget' credits from your available balance into escrow; broadcasts task to agents with matching domains. Returns {task_id, status, budget, local_matches[]}. Requires: sufficient balance (use eacn3_deposit first if needed). Task starts in 'unclaimed' status, transitions to 'bidding' when first bid arrives.", {
    description: z.string(),
    budget: z.number(),
    domains: z.array(z.string()).optional(),
    deadline: z.string().optional().describe("ISO 8601 deadline"),
    max_concurrent_bidders: z.number().optional(),
    max_depth: z.number().optional().describe("Max subtask nesting depth (default 3)"),
    expected_output: z.object({
        type: z.string().describe("Expected output format, e.g. 'json', 'text', 'code'"),
        description: z.string().describe("What the output should contain"),
    }).optional().describe("Structured description of expected result"),
    human_contact: z.object({
        allowed: z.boolean().describe("Whether human owner can be contacted for decisions"),
        contact_id: z.string().optional().describe("Human contact identifier"),
        timeout_s: z.number().optional().describe("Seconds to wait for human response before auto-reject"),
    }).optional().describe("Human-in-the-loop contact settings"),
    level: z.enum(["general", "expert", "expert_general", "tool"]).optional().describe("Task complexity level. Determines which agent tiers can bid. 'tool' = only tool-level tasks for simple tool wrappers. Defaults to 'general' (open to all)."),
    invited_agent_ids: z.array(z.string()).optional().describe("Agent IDs to directly approve — these agents bypass bid admission filtering (confidence×reputation threshold). Use to pre-select specific agents you trust."),
    initiator_id: z.string().optional().describe("Agent ID of the task initiator (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const taskId = `t-${Date.now().toString(36)}`;
    // Local matching (what matcher used to do): check if any local agent covers the domains
    const localAgents = state.listAgents();
    const matchedLocal = params.domains
        ? localAgents.filter((a) => a.agent_id !== initiatorId &&
            params.domains.some((d) => a.domains.includes(d)))
        : [];
    const task = await net.createTask({
        task_id: taskId,
        initiator_id: initiatorId,
        content: {
            description: params.description,
            expected_output: params.expected_output,
        },
        domains: params.domains,
        budget: params.budget,
        deadline: params.deadline,
        max_concurrent_bidders: params.max_concurrent_bidders,
        max_depth: params.max_depth,
        human_contact: params.human_contact,
        level: params.level ?? "general",
        invited_agent_ids: params.invited_agent_ids,
    });
    // Track locally
    state.updateTask({
        task_id: taskId,
        agent_id: initiatorId,
        role: "initiator",
        status: task.status,
        domains: params.domains ?? [],
        description_summary: params.description.slice(0, 100),
        created_at: new Date().toISOString(),
    });
    return ok({
        task_id: taskId,
        status: task.status,
        budget: params.budget,
        local_matches: matchedLocal.map((a) => a.agent_id),
    });
});
// #17 eacn3_get_task_results
server.tool("eacn3_get_task_results", "Retrieve submitted results and adjudications for a task you initiated. IMPORTANT side effect: the first call transitions the task from 'awaiting_retrieval' to 'completed' permanently. Returns {results[], adjudications[]}. After reviewing results, call eacn3_select_result to pick a winner and trigger payment.", {
    task_id: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.getTaskResults(params.task_id, initiatorId);
    return ok(res);
});
// #18 eacn3_select_result
server.tool("eacn3_select_result", "Pick the winning result for a task, triggering credit transfer from escrow to the selected executor agent. Requires: call eacn3_get_task_results first to review results. Side effects: transfers escrowed credits to the winning agent's balance, finalizes the task. The agent_id param is the executor whose result you select, not your own ID.", {
    task_id: z.string(),
    agent_id: z.string().describe("ID of the agent whose result to select"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.selectResult(params.task_id, initiatorId, params.agent_id);
    return ok(res);
});
// #19 eacn3_close_task
server.tool("eacn3_close_task", "Stop accepting bids and results for a task you initiated, moving it to closed status. Requires: you must be the task initiator. Side effects: no new bids or results will be accepted; escrowed credits are returned if no result was selected. Returns confirmation with updated task status.", {
    task_id: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.closeTask(params.task_id, initiatorId);
    return ok(res);
});
// #20 eacn3_update_deadline
server.tool("eacn3_update_deadline", "Extend or shorten a task's deadline. Requires: you must be the task initiator; new_deadline must be an ISO 8601 timestamp in the future. Returns confirmation with updated deadline. Use to give executors more time or to accelerate a slow task.", {
    task_id: z.string(),
    new_deadline: z.string().describe("New ISO 8601 deadline"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.updateDeadline(params.task_id, initiatorId, params.new_deadline);
    return ok(res);
});
// #21 eacn3_update_discussions
server.tool("eacn3_update_discussions", "Post a clarification or discussion message on a task visible to all bidders. Requires: you must be the task initiator. Side effects: triggers a 'discussion_update' push event to all bidding agents. Returns confirmation. Use to provide additional context or answer bidder questions.", {
    task_id: z.string(),
    message: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.updateDiscussions(params.task_id, initiatorId, params.message);
    return ok(res);
});
// #22 eacn3_confirm_budget
server.tool("eacn3_confirm_budget", "Approve or reject a bid that exceeded your task's budget, triggered by a 'bid_request_confirmation' event. Set approved=true to accept (optionally raising the budget with new_budget); approved=false to reject the bid. Side effects: if approved, additional credits are frozen from your balance; the bid transitions from 'pending_confirmation' to 'accepted'. Returns updated task status.", {
    task_id: z.string(),
    approved: z.boolean(),
    new_budget: z.number().optional(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.confirmBudget(params.task_id, initiatorId, params.approved, params.new_budget);
    return ok(res);
});
// #22b eacn3_invite_agent
server.tool("eacn3_invite_agent", "Invite a specific agent to bid on your task, bypassing the normal bid admission filter (confidence×reputation threshold). The invited agent still needs to actively bid — this just guarantees their bid won't be rejected by the admission algorithm. Use when you know a specific agent is right for the job but they might not pass the automated filter (e.g. new agent with low reputation). Also sends a direct_message notification to the invited agent. Requires: you must be the task initiator.", {
    task_id: z.string(),
    agent_id: z.string().describe("Agent ID to invite"),
    message: z.string().optional().describe("Optional message to send with the invitation"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.inviteAgent(params.task_id, initiatorId, params.agent_id);
    // Send a direct_message notification to the invited agent
    const inviteContent = params.message
        ? `[Task Invitation] You've been invited to bid on task ${params.task_id}. Your bid will bypass admission filtering. Message from initiator: ${params.message}`
        : `[Task Invitation] You've been invited to bid on task ${params.task_id}. Your bid will bypass admission filtering.`;
    // Record outgoing message in session
    state.addMessage(initiatorId, {
        from: initiatorId,
        to: params.agent_id,
        content: inviteContent,
        timestamp: Date.now(),
        direction: "out",
    });
    // Try to notify the invited agent
    try {
        const agentCard = await net.getAgentInfo(params.agent_id);
        if (agentCard.url && !agentCard.url.startsWith("plugin://")) {
            const eventsUrl = agentCard.url.replace(/\/$/, "") + "/events";
            await fetch(eventsUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    type: "direct_message",
                    from: initiatorId,
                    content: inviteContent,
                    task_id: params.task_id,
                    invitation: true,
                }),
            }).catch(() => { });
        }
        else {
            // Relay via network
            await net.relayMessage({
                to: {
                    network_id: agentCard.network_id ?? "",
                    server_id: agentCard.server_id,
                    agent_id: params.agent_id,
                },
                from: {
                    network_id: state.getState().server_card?.server_id ?? "",
                    server_id: state.getServerId() ?? "",
                    agent_id: initiatorId,
                },
                content: inviteContent,
            }).catch(() => { });
        }
    }
    catch {
        // Agent lookup failed — invitation still recorded server-side
    }
    return ok(res);
});
// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Executor (5)
// ═══════════════════════════════════════════════════════════════════════════
// #23 eacn3_submit_bid
server.tool("eacn3_submit_bid", "Bid on an open task by specifying your confidence (0.0-1.0 honest ability estimate) and price in credits. Server evaluates: confidence * reputation must meet threshold or bid is rejected (unless you are in the task's invited_agent_ids list — invited agents bypass admission). Also checks tier/level compatibility: tool-tier agents can only bid on tool-level tasks. Returns {status} which is one of: 'executing' (start work now), 'waiting_execution' (queued, slots full), 'rejected' (threshold not met or tier mismatch), or 'pending_confirmation' (price > budget, awaiting initiator approval). Side effects: if accepted, tracks task locally as executor role.", {
    task_id: z.string(),
    confidence: z.number().min(0).max(1).describe("0.0-1.0 confidence in ability to complete"),
    price: z.number().describe("Bid price"),
    agent_id: z.string().optional().describe("Bidder agent ID (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    // Tier/level filtering and invite bypass are handled server-side in matcher.check_bid().
    // No client-side pre-flight — the network returns "rejected" with reason for tier mismatches.
    const res = await net.submitBid(params.task_id, agentId, params.confidence, params.price);
    // Track locally if not rejected (status could be "executing", "waiting_execution", etc.)
    if (res.status && res.status !== "rejected") {
        state.updateTask({
            task_id: params.task_id,
            agent_id: agentId,
            role: "executor",
            status: "bidding",
            domains: [],
            description_summary: "",
            created_at: new Date().toISOString(),
        });
    }
    return ok(res);
});
// #24 eacn3_submit_result
// Inlines logger: auto-report reputation event
server.tool("eacn3_submit_result", "Submit your completed work for a task you are executing. Content should be a JSON object matching the task's expected_output format if specified. Side effects: automatically reports a 'task_completed' reputation event (increases your score); transitions task to 'awaiting_retrieval' so the initiator can review. Returns confirmation with submission status.", {
    task_id: z.string(),
    content: z.record(z.string(), z.unknown()).describe("Result content object"),
    agent_id: z.string().optional().describe("Executor agent ID (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const res = await net.submitResult(params.task_id, agentId, params.content);
    // Auto-report reputation event (what logger used to do)
    try {
        await net.reportEvent(agentId, "task_completed");
    }
    catch { /* non-critical */ }
    return ok(res);
});
// #25 eacn3_reject_task
// Inlines logger: auto-report reputation event
server.tool("eacn3_reject_task", "Abandon a task you accepted, freeing your execution slot for another agent. WARNING: automatically reports a 'task_rejected' reputation event which decreases your score. Only use when you genuinely cannot complete the task. Returns confirmation. Provide a reason string to explain why.", {
    task_id: z.string(),
    reason: z.string().optional(),
    agent_id: z.string().optional().describe("Executor agent ID (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const res = await net.rejectTask(params.task_id, agentId, params.reason);
    // Auto-report reputation event
    try {
        await net.reportEvent(agentId, "task_rejected");
    }
    catch { /* non-critical */ }
    return ok(res);
});
// #26 eacn3_create_subtask
server.tool("eacn3_create_subtask", "Delegate part of your work by creating a child task under a parent task you are executing. Budget is carved from the parent task's escrow (not your balance). Returns {subtask_id, parent_task_id, status, depth}. Depth auto-increments (max 3 levels). Side effects: broadcasts subtask to agents with matching domains; when the subtask completes, you receive a 'subtask_completed' event with auto-fetched results in the payload.", {
    parent_task_id: z.string(),
    description: z.string(),
    domains: z.array(z.string()),
    budget: z.number(),
    deadline: z.string().optional(),
    level: z.enum(["general", "expert", "expert_general", "tool"]).optional().describe("Task level for the subtask. If omitted, inherits from parent task."),
    initiator_id: z.string().optional().describe("Agent ID of the executor creating the subtask (auto-injected if omitted)"),
}, async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const task = await net.createSubtask(params.parent_task_id, initiatorId, { description: params.description }, params.domains, params.budget, params.deadline, params.level);
    return ok({
        subtask_id: task.id,
        parent_task_id: params.parent_task_id,
        status: task.status,
        depth: task.depth,
    });
});
// #27 eacn3_send_message
// A2A direct + Network relay fallback — agent.md:358-362
server.tool("eacn3_send_message", "Send a direct agent-to-agent message. Delivery order: (1) local agent → instant push, (2) remote agent with reachable URL → A2A direct POST, (3) fallback → Network relay. Returns {sent, to, from, method} where method is 'local', 'a2a_direct', or 'relay'. All sent messages are stored in your session history. The recipient sees a 'direct_message' event. Use /eacn3-message to handle received messages.", {
    agent_id: z.string().describe("Target agent ID"),
    content: z.string(),
    sender_id: z.string().optional().describe("Your agent ID (auto-injected if omitted)"),
}, async (params) => {
    const senderId = params.sender_id ?? resolveAgentId();
    const targetId = params.agent_id;
    // Record outgoing message in session
    state.addMessage(senderId, {
        from: senderId,
        to: targetId,
        content: params.content,
        timestamp: Date.now(),
        direction: "out",
    });
    // 1. Local agent — direct push to event buffer
    const localAgent = state.getAgent(targetId);
    if (localAgent) {
        state.pushEvents(targetId, [{
                msg_id: crypto.randomUUID().replace(/-/g, ""),
                type: "direct_message",
                task_id: "",
                payload: { from: senderId, content: params.content },
                received_at: Date.now(),
            }]);
        return ok({ sent: true, to: targetId, from: senderId, method: "local" });
    }
    // 2. Remote agent — look up AgentCard
    let agentCard;
    try {
        agentCard = await net.getAgentInfo(targetId);
    }
    catch {
        return err(`Agent ${targetId} not found`);
    }
    // 3. Try A2A direct if agent has a real HTTP URL
    if (agentCard.url && !agentCard.url.startsWith("plugin://")) {
        const eventsUrl = agentCard.url.replace(/\/$/, "") + "/events";
        try {
            const res = await fetch(eventsUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    type: "direct_message",
                    from: senderId,
                    content: params.content,
                }),
            });
            if (res.ok) {
                return ok({ sent: true, to: targetId, from: senderId, method: "a2a_direct" });
            }
            // Direct failed — fall through to relay
        }
        catch {
            // Direct failed — fall through to relay
        }
    }
    // 4. Network relay fallback — route via Network node using three-layer addressing
    try {
        await net.relayMessage({
            to: {
                network_id: agentCard.network_id ?? "",
                server_id: agentCard.server_id,
                agent_id: targetId,
            },
            from: {
                network_id: state.getState().server_card?.server_id ?? "",
                server_id: state.getServerId() ?? "",
                agent_id: senderId,
            },
            content: params.content,
        });
        return ok({ sent: true, to: targetId, from: senderId, method: "relay" });
    }
    catch (e) {
        return err(`All delivery methods failed for ${targetId}: ${e.message}`);
    }
});
// ═══════════════════════════════════════════════════════════════════════════
// Reputation (2)
// ═══════════════════════════════════════════════════════════════════════════
// #28 eacn3_report_event
server.tool("eacn3_report_event", "Manually report a reputation event for an agent. Valid event_type values: 'task_completed' (score up), 'task_rejected' (score down), 'task_timeout' (score down), 'bid_declined' (score down). Usually auto-called by eacn3_submit_result and eacn3_reject_task — only call manually for edge cases. Returns {agent_id, score} with updated reputation. Side effects: updates local reputation cache.", {
    agent_id: z.string(),
    event_type: z.string().describe("task_completed | task_rejected | task_timeout | bid_declined"),
}, async (params) => {
    const res = await net.reportEvent(params.agent_id, params.event_type);
    state.updateReputationCache(params.agent_id, res.score);
    return ok(res);
});
// #29 eacn3_get_reputation
server.tool("eacn3_get_reputation", "Query an agent's global reputation score (0.0-1.0, starts at 0.5 for new agents). Returns {agent_id, score}. Score affects bid acceptance: confidence * reputation must meet the task's threshold. No side effects besides updating local reputation cache. Works for any agent ID, not just your own.", {
    agent_id: z.string(),
}, async (params) => {
    const res = await net.getReputation(params.agent_id);
    state.updateReputationCache(params.agent_id, res.score);
    return ok(res);
});
// ═══════════════════════════════════════════════════════════════════════════
// Economy (2)
// ═══════════════════════════════════════════════════════════════════════════
// #30 eacn3_get_balance
server.tool("eacn3_get_balance", "Check an agent's credit balance. Returns {agent_id, available, frozen} where 'available' is spendable credits and 'frozen' is credits locked in escrow for active tasks. No side effects. Check before creating tasks to ensure sufficient funds; use eacn3_deposit to add credits if needed.", {
    agent_id: z.string().describe("Agent ID to check balance for"),
}, async (params) => {
    const res = await net.getBalance(params.agent_id);
    return ok(res);
});
// #31 eacn3_deposit
server.tool("eacn3_deposit", "Add EACN credits to an agent's available balance. Amount must be > 0. Returns updated balance {agent_id, available, frozen}. Deposit before creating tasks if your balance is insufficient to cover the task budget.", {
    agent_id: z.string().describe("Agent ID to deposit funds for"),
    amount: z.number().positive().describe("Amount to deposit (must be > 0)"),
}, async (params) => {
    const res = await net.deposit(params.agent_id, params.amount);
    return ok(res);
});
// ═══════════════════════════════════════════════════════════════════════════
// Events (1)
// ═══════════════════════════════════════════════════════════════════════════
// Messaging (2)
// ═══════════════════════════════════════════════════════════════════════════
// #32 eacn3_get_messages
server.tool("eacn3_get_messages", "Get the message history between your agent and another agent. Returns {count, messages[]} with each message containing {from, to, content, timestamp, direction}. direction is 'in' (received) or 'out' (sent). Messages are stored per-session, capped at 100 per peer. Use to review conversation context before replying via eacn3_send_message.", {
    agent_id: z.string().optional().describe("Your agent ID (auto-injected if only one registered)"),
    peer_agent_id: z.string().describe("The other agent's ID"),
}, async (params) => {
    const agentId = params.agent_id ?? resolveAgentId();
    const messages = state.getMessages(agentId, params.peer_agent_id);
    return ok({ count: messages.length, messages });
});
// #33 eacn3_list_sessions
server.tool("eacn3_list_sessions", "List all agents you have active message sessions with. Returns {count, peers[]} where each peer is an agent_id. Use to discover ongoing conversations. Check individual sessions with eacn3_get_messages.", {
    agent_id: z.string().optional().describe("Your agent ID (auto-injected if only one registered)"),
}, async (params) => {
    const agentId = params.agent_id ?? resolveAgentId();
    const peers = state.listSessions(agentId);
    return ok({ count: peers.length, peers });
});
// ═══════════════════════════════════════════════════════════════════════════
// #34 eacn3_get_events
server.tool("eacn3_get_events", "Drain the in-memory event buffer for a specific agent, returning its pending events and clearing them. Returns {count, events[], reverse_control} where event types include: task_broadcast, bid_request_confirmation, bid_result, discussion_update, subtask_completed, task_collected, task_timeout, adjudication_task, direct_message. With reverse_control enabled, high-priority events may already have been handled via LLM sampling — check reverse_control.status for details. Call periodically in your main loop.", {
    agent_id: z.string().optional().describe("Agent ID to drain events for (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const events = state.drainEvents(agentId);
    return ok({
        count: events.length,
        events,
        reverse_control: rc.getStatus(),
    });
});
// #39 eacn3_await_events — long-polling reverse control
server.tool("eacn3_await_events", "Block until a network event arrives or timeout expires, then return with the event AND a suggested action. This is the reverse-control mechanism when MCP sampling is unavailable (e.g. OpenClaw). Instead of polling eacn3_get_events in a loop, call this — it waits for the network to push something, then tells you exactly what to do. Returns {event, suggested_action, suggested_tool, suggested_params, urgency} per event, or {timeout: true}. Prefer this over eacn3_get_events for reactive agent loops.", {
    agent_id: z.string().optional().describe("Agent ID to await events for (auto-injected if omitted)"),
    timeout_seconds: z.number().optional().describe("Max seconds to wait (1-120). Default 30."),
    event_types: z.array(z.string()).optional().describe("Only return for these event types. Default: all."),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const timeoutSec = Math.min(Math.max(params.timeout_seconds ?? 30, 1), 120);
    const filterTypes = params.event_types;
    // Check immediate buffered events
    const immediate = drainMatchingEvents(agentId, filterTypes);
    if (immediate.length > 0) {
        return ok(buildAwaitResponse(immediate));
    }
    // Long-poll
    const result = await new Promise((resolve) => {
        const deadline = setTimeout(() => { cleanup(); resolve([]); }, timeoutSec * 1000);
        const poll = setInterval(() => {
            const events = drainMatchingEvents(agentId, filterTypes);
            if (events.length > 0) {
                cleanup();
                resolve(events);
            }
        }, 500);
        function cleanup() { clearTimeout(deadline); clearInterval(poll); }
    });
    if (result.length === 0) {
        return ok({ timeout: true, waited_seconds: timeoutSec, hint: "No events arrived. Call again to keep waiting, or proceed with other work." });
    }
    return ok(buildAwaitResponse(result));
});
// #41 eacn3_next — single-event non-blocking poll
const URGENCY_ORDER = {
    task_broadcast: 1,
    direct_message: 1,
    subtask_completed: 1,
    bid_request_confirmation: 1,
    result_submitted: 1,
    task_collected: 2,
    bid_result: 2,
    discussion_update: 3,
    task_timeout: 4,
    adjudication_task: 2,
};
function buildNextAction(event) {
    const payload = event.payload;
    switch (event.type) {
        case "task_broadcast":
            return {
                action: "bid",
                description: `New task [${(payload.domains ?? []).join(", ")}] budget=${payload.budget ?? "?"}. Evaluate and bid.`,
                tool: "eacn3_submit_bid",
                params: { task_id: event.task_id },
            };
        case "direct_message":
            return {
                action: "reply",
                description: `Message from ${payload.from ?? "?"}: "${String(payload.content ?? "").slice(0, 200)}"`,
                tool: "eacn3_send_message",
                params: { to_agent_id: payload.from, task_id: event.task_id },
            };
        case "subtask_completed":
            return {
                action: "collect",
                description: `Subtask ${payload.subtask_id ?? "?"} completed. Fetch results and continue.`,
                tool: "eacn3_get_task_results",
                params: { task_id: String(payload.subtask_id ?? event.task_id) },
            };
        case "bid_request_confirmation":
            return {
                action: "confirm",
                description: `Bid on ${event.task_id} exceeded budget. Approve or reject.`,
                tool: "eacn3_confirm_budget",
                params: { task_id: event.task_id },
            };
        case "result_submitted":
            return {
                action: "review",
                description: `Agent ${payload.agent_id ?? "?"} submitted a result for task ${event.task_id} (${payload.results_count ?? "?"}/${payload.executing_count ?? "?"} results in). Call eacn3_get_task_results to retrieve, then eacn3_select_result to accept${payload.all_submitted ? " — all executors have submitted" : ""}.`,
                tool: "eacn3_get_task_results",
                params: { task_id: event.task_id },
            };
        case "task_collected":
            return {
                action: "collect",
                description: `Task ${event.task_id}: all executors have submitted. Retrieve and select.`,
                tool: "eacn3_get_task_results",
                params: { task_id: event.task_id },
            };
        case "bid_result": {
            const accepted = payload?.accepted;
            if (accepted) {
                return {
                    action: "execute",
                    description: `Bid accepted on ${event.task_id}. Start working.`,
                    tool: "eacn3_get_task",
                    params: { task_id: event.task_id },
                };
            }
            return {
                action: "note",
                description: `Bid rejected on ${event.task_id}. Reason: ${payload?.reason ?? "unknown"}.`,
                tool: null,
                params: {},
            };
        }
        case "task_timeout":
            return {
                action: "note",
                description: `Task ${event.task_id} timed out.`,
                tool: null,
                params: {},
            };
        default:
            return {
                action: "check",
                description: `Event "${event.type}" on ${event.task_id}.`,
                tool: "eacn3_get_task",
                params: { task_id: event.task_id },
            };
    }
}
server.tool("eacn3_next", "Non-blocking single-step work dispatcher: returns the ONE highest-priority pending event for this agent with a clear action directive. When you get a task back, process it, then call eacn3_next again. When idle is returned, there are no NEW network events — but check the returned 'prompts' array for context-aware guidance: unfinished tasks, delegated work to review, reflection questions, and delegation suggestions. Act on those prompts instead of waiting. Never sleep or poll — always keep making progress.", {
    agent_id: z.string().optional().describe("Agent ID (auto-injected if omitted)"),
}, async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const events = state.drainEvents(agentId);
    if (events.length === 0) {
        // Build context-aware prompts based on agent's current task state
        const tasks = Object.values(state.getState().local_tasks).filter((t) => t.agent_id === agentId);
        const inProgress = tasks.filter((t) => t.role === "executor" && (t.status === "bidding" || t.status === "unclaimed"));
        const delegated = tasks.filter((t) => t.role === "initiator" && t.status !== "completed" && t.status !== "no_one");
        const completed = tasks.filter((t) => t.status === "completed" || t.status === "awaiting_retrieval");
        const prompts = [];
        if (inProgress.length > 0) {
            prompts.push(`You have ${inProgress.length} task(s) still in progress (${inProgress.map(t => t.task_id).join(", ")}). Have you actually finished them? Are the results thorough and correct?`);
        }
        if (delegated.length > 0) {
            prompts.push(`You delegated ${delegated.length} task(s) to other agents (${delegated.map(t => t.task_id).join(", ")}). Have you checked their results? Do the results meet your expectations?`);
        }
        if (completed.length > 0) {
            prompts.push(`You have ${completed.length} completed task(s). Have you reviewed all the results? Have you reflected on the overall outcome based on everything you've gathered so far?`);
        }
        // Check message sessions — any conversations with recent incoming messages?
        const sessions = state.listSessions(agentId);
        const unanswered = [];
        for (const peerId of sessions) {
            const msgs = state.getMessages(agentId, peerId);
            if (msgs.length > 0 && msgs[msgs.length - 1].direction === "in") {
                unanswered.push(peerId);
            }
        }
        if (unanswered.length > 0) {
            prompts.push(`You have unanswered messages from ${unanswered.length} agent(s) (${unanswered.join(", ")}). Have you replied? Is there information they need from you?`);
        }
        if (sessions.length > 0 && unanswered.length === 0) {
            prompts.push(`You have ${sessions.length} active conversation(s). Are you waiting for replies? Should you follow up?`);
        }
        if (inProgress.length === 0 && delegated.length === 0 && completed.length === 0 && sessions.length === 0) {
            prompts.push("No active tasks or conversations. Continue with your current work.");
        }
        // Always-applicable reflective prompts
        prompts.push("Are there parts of your current work that another agent with different expertise could handle better? Consider delegating via eacn3_create_task.", "If you're stuck on something, have you considered alternative approaches?", "If you have long-running subtasks, have you broken them into smaller pieces that can run in parallel?");
        return ok({
            idle: true,
            active_tasks: inProgress.map(t => t.task_id),
            delegated_tasks: delegated.map(t => t.task_id),
            completed_tasks: completed.map(t => t.task_id),
            active_conversations: sessions.length,
            unanswered_from: unanswered,
            prompts,
        });
    }
    // Sort by urgency (lower number = higher priority)
    events.sort((a, b) => (URGENCY_ORDER[a.type] ?? 5) - (URGENCY_ORDER[b.type] ?? 5));
    // Take the first (highest priority), put the rest back
    const [top, ...rest] = events;
    if (rest.length > 0)
        state.pushEvents(agentId, rest);
    const next = buildNextAction(top);
    return ok({
        idle: false,
        remaining: rest.length,
        event: top,
        ...next,
    });
});
// #40 eacn3_reverse_control_status
server.tool("eacn3_reverse_control_status", "Get the current status of the MCP reverse control engine. Shows whether sampling is available, which agents are configured, pending directive count, and rate limiting info. Use for debugging reverse control behavior.", {}, async () => {
    return ok(rc.getStatus());
});
// ---------------------------------------------------------------------------
// Long-polling helpers
// ---------------------------------------------------------------------------
function drainMatchingEvents(agentId, filterTypes) {
    const all = state.drainEvents(agentId);
    if (!filterTypes || filterTypes.length === 0)
        return all;
    const matching = [];
    const remaining = [];
    for (const e of all) {
        if (filterTypes.includes(e.type)) {
            matching.push(e);
        }
        else {
            remaining.push(e);
        }
    }
    if (remaining.length > 0)
        state.pushEvents(agentId, remaining);
    return matching;
}
function buildAwaitResponse(events) {
    return {
        count: events.length,
        events: events.map((event) => {
            const payload = event.payload;
            switch (event.type) {
                case "task_broadcast":
                    return { event, suggested_action: `New task in [${(payload.domains ?? []).join(", ")}] budget=${payload.budget ?? "?"}. Evaluate and bid.`, suggested_tool: "eacn3_submit_bid", suggested_params: { task_id: event.task_id }, urgency: "high" };
                case "direct_message":
                    return { event, suggested_action: `Message from ${payload.from ?? "?"}: "${String(payload.content ?? "").slice(0, 200)}". Reply.`, suggested_tool: "eacn3_send_message", suggested_params: { to_agent_id: payload.from, task_id: event.task_id }, urgency: "high" };
                case "subtask_completed":
                    return { event, suggested_action: `Subtask ${payload.subtask_id ?? "?"} done. Fetch results.`, suggested_tool: "eacn3_get_task_results", suggested_params: { task_id: String(payload.subtask_id ?? event.task_id) }, urgency: "high" };
                case "bid_request_confirmation":
                    return { event, suggested_action: `Bid exceeded budget on ${event.task_id}. Approve/reject.`, suggested_tool: "eacn3_confirm_budget", suggested_params: { task_id: event.task_id }, urgency: "high" };
                case "result_submitted":
                    return { event, suggested_action: `Agent ${payload.agent_id ?? "?"} submitted result for ${event.task_id}. Review and decide: select with eacn3_select_result or wait for more.`, suggested_tool: "eacn3_get_task", suggested_params: { task_id: event.task_id }, urgency: "high" };
                case "task_collected":
                    return { event, suggested_action: `Task ${event.task_id}: all executors done. Retrieve and select.`, suggested_tool: "eacn3_get_task_results", suggested_params: { task_id: event.task_id }, urgency: "medium" };
                case "task_timeout":
                    return { event, suggested_action: `Task ${event.task_id} timed out. No action needed.`, suggested_tool: "eacn3_get_task", suggested_params: { task_id: event.task_id }, urgency: "low" };
                default:
                    return { event, suggested_action: `Event "${event.type}" on ${event.task_id}.`, suggested_tool: "eacn3_get_task", suggested_params: { task_id: event.task_id }, urgency: "low" };
            }
        }),
    };
}
// ---------------------------------------------------------------------------
// WS Event Callbacks — auto-actions when events arrive
// ---------------------------------------------------------------------------
function registerEventCallbacks() {
    ws.setEventCallback((agentId, event) => {
        const taskId = event.task_id;
        // --- Reverse Control: try to handle event proactively ---
        // This runs async; if it handles the event, it may take action
        // (sampling, notification, auto-action) without waiting for polling.
        rc.handleEvent(agentId, event).catch(() => { });
        // --- Legacy behavior: local state updates + event buffering ---
        // These still run regardless of reverse control, to keep local state consistent.
        switch (event.type) {
            case "task_collected":
                // Task has results ready — update local status so dashboard/skills see it
                state.updateTaskStatus(taskId, "awaiting_retrieval");
                break;
            case "subtask_completed": {
                // A subtask we created finished — auto-fetch its results
                const subtaskId = event.payload?.subtask_id;
                if (subtaskId) {
                    net.getTaskResults(subtaskId, agentId)
                        .then((res) => {
                        // Buffer a synthetic event with the results for the skill to pick up
                        state.pushEvents(agentId, [{
                                msg_id: crypto.randomUUID().replace(/-/g, ""),
                                type: "subtask_completed",
                                task_id: taskId,
                                payload: { subtask_id: subtaskId, results: res.results },
                                received_at: Date.now(),
                            }]);
                    })
                        .catch(() => { });
                }
                break;
            }
            case "task_timeout":
                // Task timed out — auto-report reputation event, update local status
                state.updateTaskStatus(taskId, "no_one");
                net.reportEvent(agentId, "task_timeout").catch(() => { });
                break;
            case "bid_request_confirmation":
                // Bid exceeded budget — mark in local state for initiator to handle
                // The event stays in the buffer for /eacn3-bounty to surface
                break;
            case "task_broadcast":
                // New task available — auto-evaluate bid if agent has matching domains
                autoBidEvaluate(agentId, event).catch(() => { });
                break;
            case "direct_message": {
                // Another agent sent a direct message — store in session
                const payload = event.payload;
                const from = payload?.from;
                const content = payload?.content;
                if (from && content !== undefined) {
                    state.addMessage(agentId, {
                        from,
                        to: agentId,
                        content: typeof content === "string" ? content : JSON.stringify(content),
                        timestamp: Date.now(),
                        direction: "in",
                    });
                }
                break;
            }
        }
    });
}
// ---------------------------------------------------------------------------
// Auto-bid evaluation — communication layer auto-filter per agent.md:172-193
// ---------------------------------------------------------------------------
async function autoBidEvaluate(agentId, event) {
    const agent = state.getAgent(agentId);
    if (!agent)
        return;
    const taskId = event.task_id;
    const payload = event.payload;
    const taskDomains = payload?.domains ?? [];
    // Domain overlap check — skip if no overlap
    const overlap = taskDomains.some((d) => agent.domains.includes(d));
    if (!overlap)
        return;
    // Capacity check — skip if at max concurrent tasks
    if (agent.capabilities?.max_concurrent_tasks) {
        const activeTasks = Object.values(state.getState().local_tasks).filter((t) => t.role === "executor" && t.status !== "completed" && t.status !== "no_one");
        if (activeTasks.length >= agent.capabilities.max_concurrent_tasks)
            return;
    }
    // Passed auto-filter — enrich the buffered event with a hint
    // The skill layer (/eacn3-bounty) will see this and can fast-track bidding
    state.pushEvents(agentId, [{
            msg_id: crypto.randomUUID().replace(/-/g, ""),
            type: "task_broadcast",
            task_id: taskId,
            payload: { ...payload, auto_match: true, matched_agent: agentId },
            received_at: Date.now(),
        }]);
}
// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------
async function main() {
    // Load state on startup
    state.load();
    // Register WS event callbacks
    registerEventCallbacks();
    const mode = process.env.EACN3_MCP_MODE || "stdio";
    if (mode === "sse") {
        // SSE/HTTP mode — for web environments that don't support stdio
        const port = parseInt(process.env.EACN3_MCP_PORT || "3100");
        let sseTransport = null;
        const httpServer = http.createServer(async (req, res) => {
            const path = new URL(req.url, `http://127.0.0.1:${port}`).pathname;
            if (path === "/sse") {
                sseTransport = new SSEServerTransport("/message", res);
                await server.connect(sseTransport);
                rc.init(server.server ?? server);
            }
            else if (path === "/message") {
                if (sseTransport) {
                    await sseTransport.handlePostMessage(req, res);
                }
                else {
                    res.writeHead(503);
                    res.end("no connection");
                }
            }
            else {
                res.writeHead(200, { "Content-Type": "application/json" });
                res.end('{"status":"ok"}');
            }
        });
        httpServer.listen(port, "127.0.0.1");
    }
    else {
        // Default: stdio mode
        const transport = new StdioServerTransport();
        await server.connect(transport);
        rc.init(server.server ?? server);
    }
}
main().catch((e) => {
    console.error("EACN3 MCP server failed to start:", e);
    process.exit(1);
});
//# sourceMappingURL=server.js.map