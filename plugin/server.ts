/**
 * EACN3 MCP Server — exposes 34 tools via stdio transport.
 *
 * All intelligence lives in Skills (host LLM). This server is just
 * state management + network API wrapper. No adapter, no registry —
 * everything is inline.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { type EacnState, type AgentCard, type PushEvent, createDefaultState, EACN3_DEFAULT_NETWORK_ENDPOINT } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as ws from "./src/ws-manager.js";
import * as a2a from "./src/a2a-server.js";

// ---------------------------------------------------------------------------
// Helper: MCP text result
// ---------------------------------------------------------------------------

function ok(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
}

function err(message: string) {
  return { content: [{ type: "text" as const, text: JSON.stringify({ error: message }) }] };
}

/** Log MCP tool calls to stderr for traceability. */
function logToolCall(toolName: string, params: Record<string, unknown>) {
  const ts = new Date().toISOString();
  console.error(`[MCP] ${ts} CALL ${toolName} params=${JSON.stringify(params)}`);
}

function logToolResult(toolName: string, success: boolean, detail?: string) {
  const ts = new Date().toISOString();
  const tag = success ? "OK" : "ERR";
  console.error(`[MCP] ${ts} ${tag}  ${toolName}${detail ? ` ${detail}` : ""}`);
}

/**
 * Resolve agent ID: use provided value, or auto-inject from state.
 * If only one agent is registered, use it. Otherwise throw.
 * Per agent.md:116 — "agent_id is auto-filled by the communication layer; agents need not provide it"
 */
function resolveAgentId(provided?: string): string {
  if (provided) return provided;
  const agents = state.listAgents();
  if (agents.length === 1) return agents[0].agent_id;
  if (agents.length === 0) throw new Error("No agents registered. Call eacn3_register_agent first.");
  throw new Error(`Multiple agents registered (${agents.map(a => a.agent_id).join(", ")}). Specify agent_id explicitly.`);
}

// ---------------------------------------------------------------------------
// Heartbeat background interval
// ---------------------------------------------------------------------------

let heartbeatInterval: ReturnType<typeof setInterval> | null = null;

function startHeartbeat(): void {
  if (heartbeatInterval) return;
  heartbeatInterval = setInterval(async () => {
    try { await net.heartbeat(); } catch { /* silent */ }
  }, 60_000);
}

function stopHeartbeat(): void {
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
server.tool(
  "eacn3_health",
  "Check if a network node is alive and responding. No prerequisites — works before eacn3_connect. Returns {status: 'ok'} on success. Use this to verify an endpoint before connecting.",
  {
    endpoint: z.string().optional().describe("Node URL to probe. Defaults to configured network endpoint."),
  },
  async (params) => {
    const target = params.endpoint ?? state.getState().network_endpoint;
    try {
      const health = await net.checkHealth(target);
      return ok({ endpoint: target, ...health });
    } catch (e) {
      return err(`Health check failed for ${target}: ${(e as Error).message}`);
    }
  },
);

// #0b eacn3_cluster_status
server.tool(
  "eacn3_cluster_status",
  "Retrieve the full cluster topology including all member nodes, their online/offline status, and seed URLs. No prerequisites — works before eacn3_connect. Returns array of node objects with status and endpoint fields. Useful for diagnostics and finding alternative endpoints if primary is down.",
  {
    endpoint: z.string().optional().describe("Node URL to query. Defaults to configured network endpoint."),
  },
  async (params) => {
    const target = params.endpoint ?? state.getState().network_endpoint;
    try {
      const cluster = await net.getClusterStatus(target);
      return ok(cluster);
    } catch (e) {
      return err(`Cluster status failed for ${target}: ${(e as Error).message}`);
    }
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Server Management (4)
// ═══════════════════════════════════════════════════════════════════════════

// #1 eacn3_connect
server.tool(
  "eacn3_connect",
  "Connect to the EACN3 network — this must be your FIRST call. Health-probes the endpoint, falls back to seed nodes if unreachable, registers a server, and starts a background heartbeat every 60s. Returns {server_id, network_endpoint, fallback, agents_online}. Side effects: opens WebSocket connections for any previously registered agents. Call eacn3_register_agent next.",
  {
    network_endpoint: z.string().optional().describe(`Network URL. Defaults to ${EACN3_DEFAULT_NETWORK_ENDPOINT}`),
    seed_nodes: z.array(z.string()).optional().describe("Additional seed node URLs for fallback"),
  },
  async (params) => {
    const preferred = params.network_endpoint ?? EACN3_DEFAULT_NETWORK_ENDPOINT;
    const s = state.getState();

    // Health probe + fallback
    let endpoint: string;
    let fallback = false;
    try {
      endpoint = await net.findHealthyEndpoint(preferred, params.seed_nodes);
      fallback = endpoint !== preferred;
    } catch (e) {
      return err(`Cannot reach any network node: ${(e as Error).message}`);
    }

    s.network_endpoint = endpoint;

    // Register as server
    const res = await net.registerServer("0.3.0", "plugin://local", "plugin-user");
    s.server_card = {
      server_id: res.server_id,
      version: "0.3.0",
      endpoint: "plugin://local",
      owner: "plugin-user",
      status: "online",
    };
    state.save();

    // Start background heartbeat
    startHeartbeat();

    // Reconnect WS for all existing agents
    for (const agentId of Object.keys(s.agents)) {
      ws.connect(agentId);
    }

    return ok({
      connected: true,
      server_id: res.server_id,
      network_endpoint: endpoint,
      fallback,
      agents_online: Object.keys(s.agents).length,
    });
  },
);

// #2 eacn3_disconnect
server.tool(
  "eacn3_disconnect",
  "Disconnect from the EACN3 network, unregister the server, and close all WebSocket connections. Requires: eacn3_connect first. Side effects: clears all local agent state; active tasks will timeout and hurt reputation. Returns {disconnected: true}. Only call at end of session.",
  {},
  async () => {
    stopHeartbeat();
    ws.disconnectAll();

    try { await net.unregisterServer(); } catch { /* may already be gone */ }

    const s = state.getState();
    s.server_card = null;
    s.agents = {};
    state.save();

    return ok({ disconnected: true });
  },
);

// #3 eacn3_heartbeat
server.tool(
  "eacn3_heartbeat",
  "Manually send a heartbeat to the network to signal this server is still alive. Requires: eacn3_connect first. Usually unnecessary — a background interval auto-sends every 60s. Only use if you suspect the connection may have gone stale.",
  {},
  async () => {
    const res = await net.heartbeat();
    return ok(res);
  },
);

// #4 eacn3_server_info
server.tool(
  "eacn3_server_info",
  "Get current server connection state, including server_card, network_endpoint, registered agent IDs, task count, and remote status. Requires: eacn3_connect first. Returns {server_card, network_endpoint, agents_count, agents[], tasks_count, remote_status}. No side effects — read-only diagnostic.",
  {},
  async () => {
    const s = state.getState();
    if (!s.server_card) return err("Not connected");

    let remote;
    try {
      remote = await net.getServer(s.server_card.server_id);
    } catch {
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
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Agent Management (7)
// ═══════════════════════════════════════════════════════════════════════════

// #5 eacn3_register_agent
// Inlines: adapter (AgentCard assembly) + registry (validate + persist + DHT)
server.tool(
  "eacn3_register_agent",
  "Create and register an agent identity on the EACN3 network. Requires: eacn3_connect first. Assembles an AgentCard, registers it with the network, persists it locally, and opens a WebSocket for real-time event push (task_broadcast, subtask_completed, etc.). Returns {agent_id, seeds, domains}. Domains control which task broadcasts you receive — be specific (e.g. 'python-coding' not 'coding').",
  {
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
    agent_type: z.enum(["executor", "planner"]).optional().describe("Defaults to executor"),
    agent_id: z.string().optional().describe("Custom agent ID. Auto-generated if omitted."),
    a2a_port: z.number().optional().describe("Port for A2A HTTP server. Enables direct agent-to-agent messaging. Omit to use Network relay only."),
    a2a_url: z.string().optional().describe("Full public URL for A2A callbacks (e.g. 'http://my-server.com:3001'). Auto-generated from a2a_port if omitted."),
  },
  async (params) => {
    const s = state.getState();
    if (!s.server_card) return err("Not connected. Call eacn3_connect first.");

    // Validate
    if (!params.name.trim()) return err("name cannot be empty");
    if (params.domains.length === 0) return err("domains cannot be empty");

    const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;
    const sid = s.server_card.server_id;

    // Determine agent URL: real A2A endpoint or local placeholder
    let agentUrl = `plugin://local/agents/${agentId}`;
    if (params.a2a_port || params.a2a_url) {
      const port = params.a2a_port ?? 0;
      const actualPort = await a2a.startServer(port);
      if (params.a2a_url) {
        agentUrl = `${params.a2a_url.replace(/\/$/, "")}/agents/${agentId}`;
      } else {
        agentUrl = `http://localhost:${actualPort}/agents/${agentId}`;
      }
    }

    // Assemble AgentCard (what adapter used to do)
    const card: AgentCard = {
      agent_id: agentId,
      name: params.name,
      agent_type: params.agent_type ?? "executor",
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

    // Open WebSocket for event push
    ws.connect(agentId);

    return ok({
      registered: true,
      agent_id: agentId,
      seeds: res.seeds,
      domains: params.domains,
      url: agentUrl,
      a2a_server: a2a.isRunning() ? { port: a2a.getServerPort() } : null,
    });
  },
);

// #6 eacn3_get_agent
server.tool(
  "eacn3_get_agent",
  "Fetch the full AgentCard for any agent by ID — checks local state first, then queries the network. Returns {agent_id, name, agent_type, domains, skills, capabilities, url, server_id, description}. No side effects. Use to inspect an agent before sending messages or evaluating bids.",
  {
    agent_id: z.string(),
  },
  async (params) => {
    // Check local first
    const local = state.getAgent(params.agent_id);
    if (local) return ok(local);

    // Fetch from network
    const remote = await net.getAgentInfo(params.agent_id);
    return ok(remote);
  },
);

// #7 eacn3_update_agent
server.tool(
  "eacn3_update_agent",
  "Update a registered agent's mutable fields: name, domains, skills, and/or description. Requires: the agent must be registered (eacn3_register_agent). Updates both network and local state. Changing domains affects which task broadcasts you receive going forward.",
  {
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
  },
  async (params) => {
    const { agent_id, ...updates } = params;
    const res = await net.updateAgent(agent_id, updates);

    // Update local state
    const local = state.getAgent(agent_id);
    if (local) {
      if (updates.name !== undefined) local.name = updates.name;
      if (updates.domains !== undefined) local.domains = updates.domains;
      if (updates.skills !== undefined) local.skills = updates.skills;
      if (updates.description !== undefined) local.description = updates.description;
      state.addAgent(local); // re-save
    }

    return ok({ updated: true, agent_id, ...res });
  },
);

// #8 eacn3_unregister_agent
server.tool(
  "eacn3_unregister_agent",
  "Remove an agent from the network and close its WebSocket connection. Side effects: deletes agent from local state, stops receiving events for this agent. Active tasks assigned to this agent will timeout and hurt reputation. Returns {unregistered: true, agent_id}.",
  {
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.unregisterAgent(params.agent_id);
    ws.disconnect(params.agent_id);
    state.removeAgent(params.agent_id);

    // Stop A2A server if no agents remain
    if (state.listAgents().length === 0 && a2a.isRunning()) {
      await a2a.stopServer();
    }

    return ok({ unregistered: true, agent_id: params.agent_id, ...res });
  },
);

// #9 eacn3_list_my_agents
server.tool(
  "eacn3_list_my_agents",
  "List all agents registered on this local server instance. Returns {count, agents[]} where each agent includes agent_id, name, agent_type, domains, and ws_connected (WebSocket status). No network call — reads local state only. Use to check which agents are active and receiving events.",
  {},
  async () => {
    const agents = state.listAgents();
    return ok({
      count: agents.length,
      agents: agents.map((a) => ({
        agent_id: a.agent_id,
        name: a.name,
        agent_type: a.agent_type,
        domains: a.domains,
        ws_connected: ws.isConnected(a.agent_id),
      })),
    });
  },
);

// #10 eacn3_discover_agents
server.tool(
  "eacn3_discover_agents",
  "Search for agents matching a specific domain using the network's discovery protocol (Gossip, then DHT, then Bootstrap fallback). Requires: eacn3_connect first. Returns a list of matching AgentCards. Use before creating a task to verify executors exist for your domains.",
  {
    domain: z.string(),
    requester_id: z.string().optional(),
  },
  async (params) => {
    const res = await net.discoverAgents(params.domain, params.requester_id);
    return ok(res);
  },
);

// #11 eacn3_list_agents
server.tool(
  "eacn3_list_agents",
  "Browse and paginate all agents registered on the network with optional filters by domain or server_id. Returns {count, agents[]}. Default page size is 20. Unlike eacn3_discover_agents, this is a direct registry query without Gossip/DHT discovery — faster but only returns agents already indexed.",
  {
    domain: z.string().optional(),
    server_id: z.string().optional(),
    limit: z.number().optional(),
    offset: z.number().optional(),
  },
  async (params) => {
    const agents = await net.listAgentsRemote(params);
    return ok({ count: agents.length, agents });
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Query (4)
// ═══════════════════════════════════════════════════════════════════════════

// #12 eacn3_get_task
server.tool(
  "eacn3_get_task",
  "Fetch complete task details from the network including description, content, bids[], results[], status, budget, deadline, and domains. No side effects — read-only. Use to inspect a task before bidding or to review submitted results. Works for any task ID regardless of your role.",
  {
    task_id: z.string(),
  },
  async (params) => {
    const task = await net.getTask(params.task_id);
    return ok(task);
  },
);

// #13 eacn3_get_task_status
server.tool(
  "eacn3_get_task_status",
  "Lightweight task query returning only status and bid list — no result content. Intended for initiators monitoring their tasks. Requires: agent_id must be the task initiator (auto-injected if only one agent registered). Returns {status, bids[]}. Cheaper than eacn3_get_task when you only need status.",
  {
    task_id: z.string(),
    agent_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const status = await net.getTaskStatus(params.task_id, agentId);
    return ok(status);
  },
);

// #14 eacn3_list_open_tasks
server.tool(
  "eacn3_list_open_tasks",
  "Browse tasks currently accepting bids (status: unclaimed or bidding). Returns {count, tasks[]} with pagination. Filter by comma-separated domains to find relevant work. Use this in your main loop to discover tasks to bid on after checking events.",
  {
    domains: z.string().optional().describe("Comma-separated domain filter"),
    limit: z.number().optional(),
    offset: z.number().optional(),
  },
  async (params) => {
    const tasks = await net.getOpenTasks(params);
    return ok({ count: tasks.length, tasks });
  },
);

// #15 eacn3_list_tasks
server.tool(
  "eacn3_list_tasks",
  "Browse all tasks on the network with optional filters by status (unclaimed, bidding, awaiting_retrieval, completed, no_one) and/or initiator_id. Returns {count, tasks[]} with pagination. Unlike eacn3_list_open_tasks, this includes tasks in all states.",
  {
    status: z.string().optional(),
    initiator_id: z.string().optional(),
    limit: z.number().optional(),
    offset: z.number().optional(),
  },
  async (params) => {
    const tasks = await net.listTasks(params);
    return ok({ count: tasks.length, tasks });
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Initiator (7)
// ═══════════════════════════════════════════════════════════════════════════

// #16 eacn3_create_task
// Inlines matcher: check local agents before hitting network
server.tool(
  "eacn3_create_task",
  "Publish a new task to the EACN3 network for other agents to bid on. Side effects: freezes 'budget' credits from your available balance into escrow; broadcasts task to agents with matching domains. Returns {task_id, status, budget, local_matches[]}. Requires: sufficient balance (use eacn3_deposit first if needed). Task starts in 'unclaimed' status, transitions to 'bidding' when first bid arrives.",
  {
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
    initiator_id: z.string().optional().describe("Agent ID of the task initiator (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const taskId = `t-${Date.now().toString(36)}`;

    // Local matching (what matcher used to do): check if any local agent covers the domains
    const localAgents = state.listAgents();
    const matchedLocal = params.domains
      ? localAgents.filter((a) =>
          a.agent_id !== initiatorId &&
          params.domains!.some((d) => a.domains.includes(d)),
        )
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
    });

    // Track locally
    state.updateTask({
      task_id: taskId,
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
  },
);

// #17 eacn3_get_task_results
server.tool(
  "eacn3_get_task_results",
  "Retrieve submitted results and adjudications for a task you initiated. IMPORTANT side effect: the first call transitions the task from 'awaiting_retrieval' to 'completed' permanently. Returns {results[], adjudications[]}. After reviewing results, call eacn3_select_result to pick a winner and trigger payment.",
  {
    task_id: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.getTaskResults(params.task_id, initiatorId);
    return ok(res);
  },
);

// #18 eacn3_select_result
server.tool(
  "eacn3_select_result",
  "Pick the winning result for a task, triggering credit transfer from escrow to the selected executor agent. Requires: call eacn3_get_task_results first to review results. Side effects: transfers escrowed credits to the winning agent's balance, finalizes the task. The agent_id param is the executor whose result you select, not your own ID.",
  {
    task_id: z.string(),
    agent_id: z.string().describe("ID of the agent whose result to select"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.selectResult(params.task_id, initiatorId, params.agent_id);
    return ok(res);
  },
);

// #19 eacn3_close_task
server.tool(
  "eacn3_close_task",
  "Stop accepting bids and results for a task you initiated, moving it to closed status. Requires: you must be the task initiator. Side effects: no new bids or results will be accepted; escrowed credits are returned if no result was selected. Returns confirmation with updated task status.",
  {
    task_id: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.closeTask(params.task_id, initiatorId);
    return ok(res);
  },
);

// #20 eacn3_update_deadline
server.tool(
  "eacn3_update_deadline",
  "Extend or shorten a task's deadline. Requires: you must be the task initiator; new_deadline must be an ISO 8601 timestamp in the future. Returns confirmation with updated deadline. Use to give executors more time or to accelerate a slow task.",
  {
    task_id: z.string(),
    new_deadline: z.string().describe("New ISO 8601 deadline"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.updateDeadline(params.task_id, initiatorId, params.new_deadline);
    return ok(res);
  },
);

// #21 eacn3_update_discussions
server.tool(
  "eacn3_update_discussions",
  "Post a clarification or discussion message on a task visible to all bidders. Requires: you must be the task initiator. Side effects: triggers a 'discussions_updated' WebSocket event to all bidding agents. Returns confirmation. Use to provide additional context or answer bidder questions.",
  {
    task_id: z.string(),
    message: z.string(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.updateDiscussions(params.task_id, initiatorId, params.message);
    return ok(res);
  },
);

// #22 eacn3_confirm_budget
server.tool(
  "eacn3_confirm_budget",
  "Approve or reject a bid that exceeded your task's budget, triggered by a 'budget_confirmation' event. Set approved=true to accept (optionally raising the budget with new_budget); approved=false to reject the bid. Side effects: if approved, additional credits are frozen from your balance; the bid transitions from 'pending_confirmation' to 'accepted'. Returns updated task status.",
  {
    task_id: z.string(),
    approved: z.boolean(),
    new_budget: z.number().optional(),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const res = await net.confirmBudget(
      params.task_id, initiatorId, params.approved, params.new_budget,
    );
    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Executor (5)
// ═══════════════════════════════════════════════════════════════════════════

// #23 eacn3_submit_bid
server.tool(
  "eacn3_submit_bid",
  "Bid on an open task by specifying your confidence (0.0-1.0 honest ability estimate) and price in credits. Server evaluates: confidence * reputation must meet threshold or bid is rejected. Returns {status} which is one of: 'executing' (start work now), 'waiting_execution' (queued, slots full), 'rejected' (threshold not met), or 'pending_confirmation' (price > budget, awaiting initiator approval). Side effects: if accepted, tracks task locally as executor role. If price > budget, initiator gets a 'budget_confirmation' event.",
  {
    task_id: z.string(),
    confidence: z.number().min(0).max(1).describe("0.0-1.0 confidence in ability to complete"),
    price: z.number().describe("Bid price"),
    agent_id: z.string().optional().describe("Bidder agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const res = await net.submitBid(params.task_id, agentId, params.confidence, params.price);

    // Track locally if not rejected (status could be "executing", "waiting_execution", etc.)
    if (res.status && res.status !== "rejected") {
      state.updateTask({
        task_id: params.task_id,
        role: "executor",
        status: "bidding",
        domains: [],
        description_summary: "",
        created_at: new Date().toISOString(),
      });
    }

    return ok(res);
  },
);

// #24 eacn3_submit_result
// Inlines logger: auto-report reputation event
server.tool(
  "eacn3_submit_result",
  "Submit your completed work for a task you are executing. Content should be a JSON object matching the task's expected_output format if specified. Side effects: automatically reports a 'task_completed' reputation event (increases your score); transitions task to 'awaiting_retrieval' so the initiator can review. Returns confirmation with submission status.",
  {
    task_id: z.string(),
    content: z.record(z.string(), z.unknown()).describe("Result content object"),
    agent_id: z.string().optional().describe("Executor agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const res = await net.submitResult(params.task_id, agentId, params.content);

    // Auto-report reputation event (what logger used to do)
    try {
      await net.reportEvent(agentId, "task_completed");
    } catch { /* non-critical */ }

    return ok(res);
  },
);

// #25 eacn3_reject_task
// Inlines logger: auto-report reputation event
server.tool(
  "eacn3_reject_task",
  "Abandon a task you accepted, freeing your execution slot for another agent. WARNING: automatically reports a 'task_rejected' reputation event which decreases your score. Only use when you genuinely cannot complete the task. Returns confirmation. Provide a reason string to explain why.",
  {
    task_id: z.string(),
    reason: z.string().optional(),
    agent_id: z.string().optional().describe("Executor agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const res = await net.rejectTask(params.task_id, agentId, params.reason);

    // Auto-report reputation event
    try {
      await net.reportEvent(agentId, "task_rejected");
    } catch { /* non-critical */ }

    return ok(res);
  },
);

// #26 eacn3_create_subtask
server.tool(
  "eacn3_create_subtask",
  "Delegate part of your work by creating a child task under a parent task you are executing. Budget is carved from the parent task's escrow (not your balance). Returns {subtask_id, parent_task_id, status, depth}. Depth auto-increments (max 3 levels). Side effects: broadcasts subtask to agents with matching domains; when the subtask completes, you receive a 'subtask_completed' event with auto-fetched results in the payload.",
  {
    parent_task_id: z.string(),
    description: z.string(),
    domains: z.array(z.string()),
    budget: z.number(),
    deadline: z.string().optional(),
    initiator_id: z.string().optional().describe("Agent ID of the executor creating the subtask (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const task = await net.createSubtask(
      params.parent_task_id,
      initiatorId,
      { description: params.description },
      params.domains,
      params.budget,
      params.deadline,
    );

    return ok({
      subtask_id: task.id,
      parent_task_id: params.parent_task_id,
      status: task.status,
      depth: task.depth,
    });
  },
);

// #27 eacn3_send_message
// A2A direct + Network relay fallback — agent.md:358-362
server.tool(
  "eacn3_send_message",
  "Send a direct agent-to-agent message. Delivery order: (1) local agent → instant push, (2) remote agent with reachable URL → A2A direct POST, (3) fallback → Network relay via WebSocket. Returns {sent, to, from, method} where method is 'local', 'a2a_direct', or 'relay'. All sent messages are stored in your session history. The recipient sees a 'direct_message' event. Use /eacn3-message to handle received messages.",
  {
    agent_id: z.string().describe("Target agent ID"),
    content: z.string(),
    sender_id: z.string().optional().describe("Your agent ID (auto-injected if omitted)"),
  },
  async (params) => {
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
      state.pushEvents([{
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
    } catch {
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
      } catch {
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
    } catch (e) {
      return err(`All delivery methods failed for ${targetId}: ${(e as Error).message}`);
    }
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Reputation (2)
// ═══════════════════════════════════════════════════════════════════════════

// #28 eacn3_report_event
server.tool(
  "eacn3_report_event",
  "Manually report a reputation event for an agent. Valid event_type values: 'task_completed' (score up), 'task_rejected' (score down), 'task_timeout' (score down), 'bid_declined' (score down). Usually auto-called by eacn3_submit_result and eacn3_reject_task — only call manually for edge cases. Returns {agent_id, score} with updated reputation. Side effects: updates local reputation cache.",
  {
    agent_id: z.string(),
    event_type: z.string().describe("task_completed | task_rejected | task_timeout | bid_declined"),
  },
  async (params) => {
    const res = await net.reportEvent(params.agent_id, params.event_type);
    state.updateReputationCache(params.agent_id, res.score);
    return ok(res);
  },
);

// #29 eacn3_get_reputation
server.tool(
  "eacn3_get_reputation",
  "Query an agent's global reputation score (0.0-1.0, starts at 0.5 for new agents). Returns {agent_id, score}. Score affects bid acceptance: confidence * reputation must meet the task's threshold. No side effects besides updating local reputation cache. Works for any agent ID, not just your own.",
  {
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.getReputation(params.agent_id);
    state.updateReputationCache(params.agent_id, res.score);
    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Economy (2)
// ═══════════════════════════════════════════════════════════════════════════

// #30 eacn3_get_balance
server.tool(
  "eacn3_get_balance",
  "Check an agent's credit balance. Returns {agent_id, available, frozen} where 'available' is spendable credits and 'frozen' is credits locked in escrow for active tasks. No side effects. Check before creating tasks to ensure sufficient funds; use eacn3_deposit to add credits if needed.",
  {
    agent_id: z.string().describe("Agent ID to check balance for"),
  },
  async (params) => {
    const res = await net.getBalance(params.agent_id);
    return ok(res);
  },
);

// #31 eacn3_deposit
server.tool(
  "eacn3_deposit",
  "Add EACN credits to an agent's available balance. Amount must be > 0. Returns updated balance {agent_id, available, frozen}. Deposit before creating tasks if your balance is insufficient to cover the task budget.",
  {
    agent_id: z.string().describe("Agent ID to deposit funds for"),
    amount: z.number().positive().describe("Amount to deposit (must be > 0)"),
  },
  async (params) => {
    const res = await net.deposit(params.agent_id, params.amount);
    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Events (1)
// ═══════════════════════════════════════════════════════════════════════════
// Messaging (2)
// ═══════════════════════════════════════════════════════════════════════════

// #32 eacn3_get_messages
server.tool(
  "eacn3_get_messages",
  "Get the message history between your agent and another agent. Returns {count, messages[]} with each message containing {from, to, content, timestamp, direction}. direction is 'in' (received) or 'out' (sent). Messages are stored per-session, capped at 100 per peer. Use to review conversation context before replying via eacn3_send_message.",
  {
    agent_id: z.string().optional().describe("Your agent ID (auto-injected if only one registered)"),
    peer_agent_id: z.string().describe("The other agent's ID"),
  },
  async (params) => {
    const agentId = params.agent_id ?? resolveAgentId();
    const messages = state.getMessages(agentId, params.peer_agent_id);
    return ok({ count: messages.length, messages });
  },
);

// #33 eacn3_list_sessions
server.tool(
  "eacn3_list_sessions",
  "List all agents you have active message sessions with. Returns {count, peers[]} where each peer is an agent_id. Use to discover ongoing conversations. Check individual sessions with eacn3_get_messages.",
  {
    agent_id: z.string().optional().describe("Your agent ID (auto-injected if only one registered)"),
  },
  async (params) => {
    const agentId = params.agent_id ?? resolveAgentId();
    const peers = state.listSessions(agentId);
    return ok({ count: peers.length, peers });
  },
);

// ═══════════════════════════════════════════════════════════════════════════

// #34 eacn3_get_events
server.tool(
  "eacn3_get_events",
  "Drain the in-memory event buffer, returning all pending events and clearing them. Returns {count, events[]} where event types include: task_broadcast, discussions_updated, subtask_completed, awaiting_retrieval, budget_confirmation, timeout, direct_message. Call periodically in your main loop. Events arrive via WebSocket and accumulate until drained — missing events means missed tasks and messages.",
  {},
  async () => {
    const events = state.drainEvents();
    return ok({
      count: events.length,
      events,
    });
  },
);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// WS Event Callbacks — auto-actions when events arrive
// ---------------------------------------------------------------------------

function registerEventCallbacks(): void {
  ws.setEventCallback((agentId, event) => {
    const taskId = event.task_id;

    switch (event.type) {
      case "awaiting_retrieval":
        // Task has results ready — update local status so dashboard/skills see it
        state.updateTaskStatus(taskId, "awaiting_retrieval");
        break;

      case "subtask_completed": {
        // A subtask we created finished — auto-fetch its results
        const subtaskId = (event.payload as Record<string, unknown>)?.subtask_id as string | undefined;
        if (subtaskId) {
          net.getTaskResults(subtaskId, agentId)
            .then((res) => {
              // Buffer a synthetic event with the results for the skill to pick up
              state.pushEvents([{
                type: "subtask_completed",
                task_id: taskId,
                payload: { subtask_id: subtaskId, results: res.results },
                received_at: Date.now(),
              }]);
            })
            .catch(() => { /* non-critical */ });
        }
        break;
      }

      case "timeout":
        // Task timed out — auto-report reputation event, update local status
        state.updateTaskStatus(taskId, "no_one");
        net.reportEvent(agentId, "task_timeout").catch(() => { /* non-critical */ });
        break;

      case "budget_confirmation":
        // Bid exceeded budget — mark in local state for initiator to handle
        // The event stays in the buffer for /eacn3-bounty to surface
        break;

      case "task_broadcast":
        // New task available — auto-evaluate bid if agent has matching domains
        autoBidEvaluate(agentId, event).catch(() => { /* non-critical */ });
        break;

      case "direct_message": {
        // Another agent sent a direct message — store in session
        const payload = event.payload as Record<string, unknown>;
        const from = payload?.from as string | undefined;
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

async function autoBidEvaluate(agentId: string, event: PushEvent): Promise<void> {
  const agent = state.getAgent(agentId);
  if (!agent) return;

  const taskId = event.task_id;
  const payload = event.payload as Record<string, unknown>;
  const taskDomains = (payload?.domains as string[]) ?? [];

  // Domain overlap check — skip if no overlap
  const overlap = taskDomains.some((d) => agent.domains.includes(d));
  if (!overlap) return;

  // Capacity check — skip if at max concurrent tasks
  if (agent.capabilities?.max_concurrent_tasks) {
    const activeTasks = Object.values(state.getState().local_tasks).filter(
      (t) => t.role === "executor" && t.status !== "completed" && t.status !== "no_one",
    );
    if (activeTasks.length >= agent.capabilities.max_concurrent_tasks) return;
  }

  // Passed auto-filter — enrich the buffered event with a hint
  // The skill layer (/eacn3-bounty) will see this and can fast-track bidding
  state.pushEvents([{
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

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error("EACN3 MCP server failed to start:", e);
  process.exit(1);
});
