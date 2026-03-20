/**
 * EACN MCP Server — exposes 29 tools via stdio transport.
 *
 * All intelligence lives in Skills (host LLM). This server is just
 * state management + network API wrapper. No adapter, no registry —
 * everything is inline.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { type EacnState, type AgentCard, type PushEvent, createDefaultState } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as ws from "./src/ws-manager.js";

// ---------------------------------------------------------------------------
// Helper: MCP text result
// ---------------------------------------------------------------------------

function ok(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
}

function err(message: string) {
  return { content: [{ type: "text" as const, text: JSON.stringify({ error: message }) }] };
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

const server = new McpServer({ name: "eacn", version: "0.1.0" });

// ═══════════════════════════════════════════════════════════════════════════
// Server Management (4)
// ═══════════════════════════════════════════════════════════════════════════

// #1 eacn_connect
server.tool(
  "eacn_connect",
  "Connect to EACN network. Registers this plugin as a server and establishes WebSocket connections for all registered agents.",
  {
    network_endpoint: z.string().optional().describe("Network URL. Defaults to https://network.eacn.dev"),
  },
  async (params) => {
    const endpoint = params.network_endpoint ?? "https://network.eacn.dev";
    const s = state.getState();
    s.network_endpoint = endpoint;

    // Register as server
    const res = await net.registerServer("0.1.0", "plugin://local", "plugin-user");
    s.server_card = {
      server_id: res.server_id,
      version: "0.1.0",
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
      agents_online: Object.keys(s.agents).length,
    });
  },
);

// #2 eacn_disconnect
server.tool(
  "eacn_disconnect",
  "Disconnect from EACN network. Unregisters server and closes all WebSocket connections.",
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

// #3 eacn_heartbeat
server.tool(
  "eacn_heartbeat",
  "Send heartbeat to network. Called by /eacn-bounty skill each loop iteration.",
  {},
  async () => {
    const res = await net.heartbeat();
    return ok(res);
  },
);

// #4 eacn_server_info
server.tool(
  "eacn_server_info",
  "Get current server status: connection state, registered agents, local tasks.",
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
// Agent Management (6)
// ═══════════════════════════════════════════════════════════════════════════

// #5 eacn_register_agent
// Inlines: adapter (AgentCard assembly) + registry (validate + persist + DHT)
server.tool(
  "eacn_register_agent",
  "Register an Agent on the network. Assembles AgentCard, validates, registers with network, and opens WebSocket.",
  {
    name: z.string().describe("Agent display name"),
    description: z.string().describe("What this Agent does"),
    domains: z.array(z.string()).describe("Capability domains (e.g. ['translation', 'coding'])"),
    skills: z.array(z.object({
      name: z.string(),
      description: z.string(),
      parameters: z.record(z.string(), z.unknown()).optional(),
    })).optional().describe("Agent skills"),
    agent_type: z.enum(["executor", "planner"]).optional().describe("Defaults to executor"),
    agent_id: z.string().optional().describe("Custom agent ID. Auto-generated if omitted."),
  },
  async (params) => {
    const s = state.getState();
    if (!s.server_card) return err("Not connected. Call eacn_connect first.");

    // Validate
    if (!params.name.trim()) return err("name cannot be empty");
    if (params.domains.length === 0) return err("domains cannot be empty");

    const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;
    const sid = s.server_card.server_id;

    // Assemble AgentCard (what adapter used to do)
    const card: AgentCard = {
      agent_id: agentId,
      name: params.name,
      agent_type: params.agent_type ?? "executor",
      domains: params.domains,
      skills: params.skills ?? [],
      url: `plugin://local/agents/${agentId}`,
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
    });
  },
);

// #6 eacn_get_agent
server.tool(
  "eacn_get_agent",
  "Get any Agent's details (AgentCard) by ID.",
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

// #7 eacn_update_agent
server.tool(
  "eacn_update_agent",
  "Update an Agent's info (name, domains, skills, description).",
  {
    agent_id: z.string(),
    name: z.string().optional(),
    domains: z.array(z.string()).optional(),
    skills: z.array(z.object({
      name: z.string(),
      description: z.string(),
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

// #8 eacn_unregister_agent
server.tool(
  "eacn_unregister_agent",
  "Unregister an Agent from the network.",
  {
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.unregisterAgent(params.agent_id);
    ws.disconnect(params.agent_id);
    state.removeAgent(params.agent_id);
    return ok({ unregistered: true, agent_id: params.agent_id, ...res });
  },
);

// #9 eacn_list_my_agents
server.tool(
  "eacn_list_my_agents",
  "List all Agents registered under this server.",
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

// #10 eacn_discover_agents
server.tool(
  "eacn_discover_agents",
  "Discover Agents by domain. Searches network via Gossip → DHT → Bootstrap fallback.",
  {
    domain: z.string(),
    requester_id: z.string().optional(),
  },
  async (params) => {
    const res = await net.discoverAgents(params.domain, params.requester_id);
    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Query (4)
// ═══════════════════════════════════════════════════════════════════════════

// #11 eacn_get_task
server.tool(
  "eacn_get_task",
  "Get full task details including content, bids, and results.",
  {
    task_id: z.string(),
  },
  async (params) => {
    const task = await net.getTask(params.task_id);
    return ok(task);
  },
);

// #12 eacn_get_task_status
server.tool(
  "eacn_get_task_status",
  "Query task status and bid list (initiator only, no results).",
  {
    task_id: z.string(),
    agent_id: z.string().describe("Initiator agent ID"),
  },
  async (params) => {
    const status = await net.getTaskStatus(params.task_id, params.agent_id);
    return ok(status);
  },
);

// #13 eacn_list_open_tasks
server.tool(
  "eacn_list_open_tasks",
  "List tasks open for bidding. Optionally filter by domains.",
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

// #14 eacn_list_tasks
server.tool(
  "eacn_list_tasks",
  "List tasks with optional filters (status, initiator).",
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

// #15 eacn_create_task
// Inlines matcher: check local agents before hitting network
server.tool(
  "eacn_create_task",
  "Create a new task. Checks local agents first, then broadcasts to network.",
  {
    description: z.string(),
    budget: z.number(),
    domains: z.array(z.string()).optional(),
    deadline: z.string().optional().describe("ISO 8601 deadline"),
    max_concurrent_bidders: z.number().optional(),
    expected_output: z.string().optional(),
    initiator_id: z.string().describe("Agent ID of the task initiator"),
  },
  async (params) => {
    const taskId = `t-${Date.now().toString(36)}`;

    // Local matching (what matcher used to do): check if any local agent covers the domains
    const localAgents = state.listAgents();
    const matchedLocal = params.domains
      ? localAgents.filter((a) =>
          a.agent_id !== params.initiator_id &&
          params.domains!.some((d) => a.domains.includes(d)),
        )
      : [];

    const task = await net.createTask({
      task_id: taskId,
      initiator_id: params.initiator_id,
      content: {
        description: params.description,
        expected_output: params.expected_output,
      },
      domains: params.domains,
      budget: params.budget,
      deadline: params.deadline,
      max_concurrent_bidders: params.max_concurrent_bidders,
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

// #16 eacn_get_task_results
server.tool(
  "eacn_get_task_results",
  "Retrieve task results and adjudications. First call transitions task from awaiting_retrieval to completed.",
  {
    task_id: z.string(),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.getTaskResults(params.task_id, params.initiator_id);
    return ok(res);
  },
);

// #17 eacn_select_result
server.tool(
  "eacn_select_result",
  "Select the winning result. Triggers economic settlement.",
  {
    task_id: z.string(),
    agent_id: z.string().describe("ID of the agent whose result to select"),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.selectResult(params.task_id, params.initiator_id, params.agent_id);
    return ok(res);
  },
);

// #18 eacn_close_task
server.tool(
  "eacn_close_task",
  "Manually close a task (stop accepting bids/results).",
  {
    task_id: z.string(),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.closeTask(params.task_id, params.initiator_id);
    return ok(res);
  },
);

// #19 eacn_update_deadline
server.tool(
  "eacn_update_deadline",
  "Update task deadline.",
  {
    task_id: z.string(),
    new_deadline: z.string().describe("New ISO 8601 deadline"),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.updateDeadline(params.task_id, params.initiator_id, params.new_deadline);
    return ok(res);
  },
);

// #20 eacn_update_discussions
server.tool(
  "eacn_update_discussions",
  "Add a discussion message to a task. Synced to all bidders.",
  {
    task_id: z.string(),
    message: z.string(),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.updateDiscussions(params.task_id, params.initiator_id, params.message);
    return ok(res);
  },
);

// #21 eacn_confirm_budget
server.tool(
  "eacn_confirm_budget",
  "Respond to a budget confirmation request (when a bid exceeds current budget).",
  {
    task_id: z.string(),
    approved: z.boolean(),
    new_budget: z.number().optional(),
    initiator_id: z.string(),
  },
  async (params) => {
    const res = await net.confirmBudget(
      params.task_id, params.initiator_id, params.approved, params.new_budget,
    );
    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Executor (5)
// ═══════════════════════════════════════════════════════════════════════════

// #22 eacn_submit_bid
server.tool(
  "eacn_submit_bid",
  "Submit a bid on a task (confidence + price).",
  {
    task_id: z.string(),
    confidence: z.number().min(0).max(1).describe("0.0-1.0 confidence in ability to complete"),
    price: z.number().describe("Bid price"),
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.submitBid(params.task_id, params.agent_id, params.confidence, params.price);

    // Track locally if accepted
    if (res.status === "accepted") {
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

// #23 eacn_submit_result
// Inlines logger: auto-report reputation event
server.tool(
  "eacn_submit_result",
  "Submit execution result for a task.",
  {
    task_id: z.string(),
    content: z.record(z.string(), z.unknown()).describe("Result content object"),
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.submitResult(params.task_id, params.agent_id, params.content);

    // Auto-report reputation event (what logger used to do)
    try {
      await net.reportEvent(params.agent_id, "task_completed");
    } catch { /* non-critical */ }

    return ok(res);
  },
);

// #24 eacn_reject_task
// Inlines logger: auto-report reputation event
server.tool(
  "eacn_reject_task",
  "Reject/return a task. Frees the execution slot. Note: rejection affects reputation.",
  {
    task_id: z.string(),
    reason: z.string().optional(),
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.rejectTask(params.task_id, params.agent_id, params.reason);

    // Auto-report reputation event
    try {
      await net.reportEvent(params.agent_id, "task_rejected");
    } catch { /* non-critical */ }

    return ok(res);
  },
);

// #25 eacn_create_subtask
server.tool(
  "eacn_create_subtask",
  "Create a subtask under a parent task. Budget is carved from parent's escrow.",
  {
    parent_task_id: z.string(),
    description: z.string(),
    domains: z.array(z.string()),
    budget: z.number(),
    deadline: z.string().optional(),
    initiator_id: z.string().describe("Agent ID of the executor creating the subtask"),
  },
  async (params) => {
    const task = await net.createSubtask(
      params.parent_task_id,
      params.initiator_id,
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

// #26 eacn_send_message
server.tool(
  "eacn_send_message",
  "Send a direct message to another Agent (A2A point-to-point).",
  {
    agent_id: z.string().describe("Target agent ID"),
    content: z.string(),
    sender_id: z.string().describe("Your agent ID"),
  },
  async (params) => {
    // A2A direct message — for now, use task discussions as transport
    // Future: direct WebSocket routing
    return ok({
      sent: true,
      to: params.agent_id,
      from: params.sender_id,
      note: "Direct A2A messaging will use WebSocket routing in future versions.",
    });
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Reputation (2)
// ═══════════════════════════════════════════════════════════════════════════

// #27 eacn_report_event
server.tool(
  "eacn_report_event",
  "Report a reputation event. Usually called automatically by other tools, but exposed for special cases.",
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

// #28 eacn_get_reputation
server.tool(
  "eacn_get_reputation",
  "Query an Agent's global reputation score.",
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
// Events (1)
// ═══════════════════════════════════════════════════════════════════════════

// #29 eacn_get_events
server.tool(
  "eacn_get_events",
  "Get pending events. WebSocket connections buffer events in memory; this drains the buffer.",
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

async function main() {
  // Load state on startup
  state.load();

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((e) => {
  console.error("EACN MCP server failed to start:", e);
  process.exit(1);
});
