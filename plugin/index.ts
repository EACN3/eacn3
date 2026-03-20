/**
 * EACN — Native OpenClaw plugin entry point.
 *
 * Registers the same 32 tools as server.ts but via api.registerTool().
 * All logic delegates to the same src/ modules.
 */

import { type AgentCard, type PushEvent, EACN_DEFAULT_NETWORK_ENDPOINT } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as ws from "./src/ws-manager.js";

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

let heartbeatInterval: ReturnType<typeof setInterval> | null = null;

function startHeartbeat(): void {
  if (heartbeatInterval) return;
  heartbeatInterval = setInterval(async () => {
    try { await net.heartbeat(); } catch { /* silent */ }
  }, 60_000);
}

function stopHeartbeat(): void {
  if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

function ok(data: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data) }] };
}

function err(message: string) {
  return { content: [{ type: "text" as const, text: JSON.stringify({ error: message }) }] };
}

// ---------------------------------------------------------------------------
// WS Event Callbacks — auto-actions when events arrive
// ---------------------------------------------------------------------------

function registerEventCallbacks(): void {
  ws.setEventCallback((agentId, event) => {
    const taskId = event.task_id;

    switch (event.type) {
      case "awaiting_retrieval":
        state.updateTaskStatus(taskId, "awaiting_retrieval");
        break;

      case "subtask_completed": {
        const subtaskId = (event.payload as Record<string, unknown>)?.subtask_id as string | undefined;
        if (subtaskId) {
          net.getTaskResults(subtaskId, agentId)
            .then((res) => {
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
        state.updateTaskStatus(taskId, "no_one");
        net.reportEvent(agentId, "task_timeout").catch(() => { /* non-critical */ });
        break;

      case "budget_confirmation":
        break;

      case "task_broadcast":
        autoBidEvaluate(agentId, event).catch(() => { /* non-critical */ });
        break;
    }
  });
}

async function autoBidEvaluate(agentId: string, event: PushEvent): Promise<void> {
  const agent = state.getAgent(agentId);
  if (!agent) return;

  const taskId = event.task_id;
  const payload = event.payload as Record<string, unknown>;
  const taskDomains = (payload?.domains as string[]) ?? [];

  const overlap = taskDomains.some((d) => agent.domains.includes(d));
  if (!overlap) return;

  if (agent.capabilities?.max_concurrent_tasks) {
    const activeTasks = Object.values(state.getState().local_tasks).filter(
      (t) => t.role === "executor" && t.status !== "completed" && t.status !== "no_one",
    );
    if (activeTasks.length >= agent.capabilities.max_concurrent_tasks) return;
  }

  state.pushEvents([{
    type: "task_broadcast",
    task_id: taskId,
    payload: { ...payload, auto_match: true, matched_agent: agentId },
    received_at: Date.now(),
  }]);
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------

export default function (api: any) {
  // Load state and register event callbacks
  state.load();
  registerEventCallbacks();

  // ═══════════════════════════════════════════════════════════════════════════
  // Server Management (4)
  // ═══════════════════════════════════════════════════════════════════════════

  // #1 eacn_connect
  api.registerTool({
    name: "eacn_connect",
    description: "Connect to EACN network. Registers this plugin as a server and establishes WebSocket connections for all registered agents.",
    parameters: {
      type: "object",
      properties: {
        network_endpoint: { type: "string", description: `Network URL. Defaults to ${EACN_DEFAULT_NETWORK_ENDPOINT}` },
      },
    },
    async execute(_id: string, params: any) {
      const endpoint = params.network_endpoint ?? EACN_DEFAULT_NETWORK_ENDPOINT;
      const s = state.getState();
      s.network_endpoint = endpoint;
      const res = await net.registerServer("0.1.0", "plugin://local", "plugin-user");
      s.server_card = { server_id: res.server_id, version: "0.1.0", endpoint: "plugin://local", owner: "plugin-user", status: "online" };
      state.save();
      startHeartbeat();
      for (const agentId of Object.keys(s.agents)) ws.connect(agentId);
      return ok({ connected: true, server_id: res.server_id, network_endpoint: endpoint, agents_online: Object.keys(s.agents).length });
    },
  });

  // #2 eacn_disconnect
  api.registerTool({
    name: "eacn_disconnect",
    description: "Disconnect from EACN network. Unregisters server and closes all WebSocket connections.",
    parameters: { type: "object", properties: {} },
    async execute() {
      stopHeartbeat(); ws.disconnectAll();
      try { await net.unregisterServer(); } catch { /* */ }
      const s = state.getState(); s.server_card = null; s.agents = {};
      state.save();
      return ok({ disconnected: true });
    },
  });

  // #3 eacn_heartbeat
  api.registerTool({
    name: "eacn_heartbeat",
    description: "Send heartbeat to network. Background interval auto-sends every 60s; this is for manual trigger.",
    parameters: { type: "object", properties: {} },
    async execute() { return ok(await net.heartbeat()); },
  });

  // #4 eacn_server_info
  api.registerTool({
    name: "eacn_server_info",
    description: "Get current server status: connection state, registered agents, local tasks.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const s = state.getState();
      if (!s.server_card) return err("Not connected");
      let remote; try { remote = await net.getServer(s.server_card.server_id); } catch { remote = null; }
      return ok({ server_card: s.server_card, network_endpoint: s.network_endpoint, agents_count: Object.keys(s.agents).length, agents: Object.keys(s.agents), tasks_count: Object.keys(s.local_tasks).length, remote_status: remote?.status ?? "unknown" });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Agent Management (7)
  // ═══════════════════════════════════════════════════════════════════════════

  // #5 eacn_register_agent
  api.registerTool({
    name: "eacn_register_agent",
    description: "Register an Agent on the network. Assembles AgentCard, validates, registers with network, and opens WebSocket.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", description: "Agent display name" },
        description: { type: "string", description: "What this Agent does" },
        domains: { type: "array", items: { type: "string" }, description: "Capability domains" },
        skills: { type: "array", items: { type: "object", properties: { id: { type: "string" }, name: { type: "string" }, description: { type: "string" }, tags: { type: "array", items: { type: "string" } }, parameters: { type: "object" } } }, description: "Agent skills" },
        capabilities: { type: "object", properties: { max_concurrent_tasks: { type: "number", description: "Max tasks simultaneously (0 = unlimited)" }, concurrent: { type: "boolean", description: "Whether Agent supports concurrent execution" } }, description: "Agent capacity limits" },
        agent_type: { type: "string", enum: ["executor", "planner"], description: "Defaults to executor" },
        agent_id: { type: "string", description: "Custom agent ID. Auto-generated if omitted." },
      },
      required: ["name", "description", "domains"],
    },
    async execute(_id: string, params: any) {
      const s = state.getState();
      if (!s.server_card) return err("Not connected. Call eacn_connect first.");
      if (!params.name?.trim()) return err("name cannot be empty");
      if (!params.domains?.length) return err("domains cannot be empty");
      const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;
      const card: AgentCard = {
        agent_id: agentId, name: params.name, agent_type: params.agent_type ?? "executor",
        domains: params.domains, skills: params.skills ?? [], capabilities: params.capabilities,
        url: `plugin://local/agents/${agentId}`, server_id: s.server_card.server_id,
        network_id: "", description: params.description,
      };
      const res = await net.registerAgent(card);
      state.addAgent(card); ws.connect(agentId);
      return ok({ registered: true, agent_id: agentId, seeds: res.seeds, domains: params.domains });
    },
  });

  // #6 eacn_get_agent
  api.registerTool({
    name: "eacn_get_agent",
    description: "Get any Agent's details (AgentCard) by ID.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const local = state.getAgent(params.agent_id);
      if (local) return ok(local);
      return ok(await net.getAgentInfo(params.agent_id));
    },
  });

  // #7 eacn_update_agent
  api.registerTool({
    name: "eacn_update_agent",
    description: "Update an Agent's info (name, domains, skills, description).",
    parameters: {
      type: "object",
      properties: {
        agent_id: { type: "string" }, name: { type: "string" },
        domains: { type: "array", items: { type: "string" } },
        skills: { type: "array", items: { type: "object", properties: { id: { type: "string" }, name: { type: "string" }, description: { type: "string" }, tags: { type: "array", items: { type: "string" } }, parameters: { type: "object" } } } },
        description: { type: "string" },
      },
      required: ["agent_id"],
    },
    async execute(_id: string, params: any) {
      const { agent_id, ...updates } = params;
      const res = await net.updateAgent(agent_id, updates);
      const local = state.getAgent(agent_id);
      if (local) {
        if (updates.name !== undefined) local.name = updates.name;
        if (updates.domains !== undefined) local.domains = updates.domains;
        if (updates.skills !== undefined) local.skills = updates.skills;
        if (updates.description !== undefined) local.description = updates.description;
        state.addAgent(local);
      }
      return ok({ updated: true, agent_id, ...res });
    },
  });

  // #8 eacn_unregister_agent
  api.registerTool({
    name: "eacn_unregister_agent",
    description: "Unregister an Agent from the network.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.unregisterAgent(params.agent_id);
      ws.disconnect(params.agent_id); state.removeAgent(params.agent_id);
      return ok({ unregistered: true, agent_id: params.agent_id, ...res });
    },
  });

  // #9 eacn_list_my_agents
  api.registerTool({
    name: "eacn_list_my_agents",
    description: "List all Agents registered under this server.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const agents = state.listAgents();
      return ok({ count: agents.length, agents: agents.map((a) => ({ agent_id: a.agent_id, name: a.name, agent_type: a.agent_type, domains: a.domains, ws_connected: ws.isConnected(a.agent_id) })) });
    },
  });

  // #10 eacn_discover_agents
  api.registerTool({
    name: "eacn_discover_agents",
    description: "Discover Agents by domain. Searches network via Gossip → DHT → Bootstrap fallback.",
    parameters: { type: "object", properties: { domain: { type: "string" }, requester_id: { type: "string" } }, required: ["domain"] },
    async execute(_id: string, params: any) {
      return ok(await net.discoverAgents(params.domain, params.requester_id));
    },
  });

  // #11 eacn_list_agents
  api.registerTool({
    name: "eacn_list_agents",
    description: "List Agents from the network. Filter by domain or server_id.",
    parameters: {
      type: "object",
      properties: {
        domain: { type: "string" }, server_id: { type: "string" },
        limit: { type: "number" }, offset: { type: "number" },
      },
    },
    async execute(_id: string, params: any) {
      const agents = await net.listAgentsRemote(params);
      return ok({ count: agents.length, agents });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Task Query (4)
  // ═══════════════════════════════════════════════════════════════════════════

  // #12 eacn_get_task
  api.registerTool({
    name: "eacn_get_task",
    description: "Get full task details including content, bids, and results.",
    parameters: { type: "object", properties: { task_id: { type: "string" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTask(params.task_id)); },
  });

  // #13 eacn_get_task_status
  api.registerTool({
    name: "eacn_get_task_status",
    description: "Query task status and bid list (initiator only, no results).",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string", description: "Initiator agent ID" } }, required: ["task_id", "agent_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTaskStatus(params.task_id, params.agent_id)); },
  });

  // #14 eacn_list_open_tasks
  api.registerTool({
    name: "eacn_list_open_tasks",
    description: "List tasks open for bidding. Optionally filter by domains.",
    parameters: { type: "object", properties: { domains: { type: "string", description: "Comma-separated domain filter" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.getOpenTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // #15 eacn_list_tasks
  api.registerTool({
    name: "eacn_list_tasks",
    description: "List tasks with optional filters (status, initiator).",
    parameters: { type: "object", properties: { status: { type: "string" }, initiator_id: { type: "string" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.listTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Task Operations — Initiator (7)
  // ═══════════════════════════════════════════════════════════════════════════

  // #16 eacn_create_task
  api.registerTool({
    name: "eacn_create_task",
    description: "Create a new task. Checks local agents first, then broadcasts to network.",
    parameters: {
      type: "object",
      properties: {
        description: { type: "string" },
        budget: { type: "number" },
        domains: { type: "array", items: { type: "string" } },
        deadline: { type: "string", description: "ISO 8601 deadline" },
        max_concurrent_bidders: { type: "number" },
        max_depth: { type: "number", description: "Max subtask nesting depth (default 3)" },
        expected_output: {
          type: "object",
          properties: {
            type: { type: "string", description: "Expected output format, e.g. 'json', 'text', 'code'" },
            description: { type: "string", description: "What the output should contain" },
          },
          description: "Structured description of expected result",
        },
        human_contact: {
          type: "object",
          properties: {
            allowed: { type: "boolean", description: "Whether human owner can be contacted for decisions" },
            contact_id: { type: "string", description: "Human contact identifier" },
            timeout_s: { type: "number", description: "Seconds to wait for human response before auto-reject" },
          },
          description: "Human-in-the-loop contact settings",
        },
        initiator_id: { type: "string", description: "Agent ID of the task initiator" },
      },
      required: ["description", "budget", "initiator_id"],
    },
    async execute(_id: string, params: any) {
      const taskId = `t-${Date.now().toString(36)}`;
      const localAgents = state.listAgents();
      const matchedLocal = params.domains
        ? localAgents.filter((a: AgentCard) => a.agent_id !== params.initiator_id && params.domains.some((d: string) => a.domains.includes(d)))
        : [];
      const task = await net.createTask({
        task_id: taskId, initiator_id: params.initiator_id,
        content: { description: params.description, expected_output: params.expected_output },
        domains: params.domains, budget: params.budget, deadline: params.deadline,
        max_concurrent_bidders: params.max_concurrent_bidders, max_depth: params.max_depth,
        human_contact: params.human_contact,
      });
      state.updateTask({ task_id: taskId, role: "initiator", status: task.status, domains: params.domains ?? [], description_summary: params.description.slice(0, 100), created_at: new Date().toISOString() });
      return ok({ task_id: taskId, status: task.status, budget: params.budget, local_matches: matchedLocal.map((a: AgentCard) => a.agent_id) });
    },
  });

  // #17 eacn_get_task_results
  api.registerTool({
    name: "eacn_get_task_results",
    description: "Retrieve task results and adjudications. First call transitions task from awaiting_retrieval to completed.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTaskResults(params.task_id, params.initiator_id)); },
  });

  // #18 eacn_select_result
  api.registerTool({
    name: "eacn_select_result",
    description: "Select the winning result. Triggers economic settlement.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string", description: "ID of the agent whose result to select" }, initiator_id: { type: "string" } }, required: ["task_id", "agent_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.selectResult(params.task_id, params.initiator_id, params.agent_id)); },
  });

  // #19 eacn_close_task
  api.registerTool({
    name: "eacn_close_task",
    description: "Manually close a task (stop accepting bids/results).",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.closeTask(params.task_id, params.initiator_id)); },
  });

  // #20 eacn_update_deadline
  api.registerTool({
    name: "eacn_update_deadline",
    description: "Update task deadline.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, new_deadline: { type: "string", description: "New ISO 8601 deadline" }, initiator_id: { type: "string" } }, required: ["task_id", "new_deadline", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.updateDeadline(params.task_id, params.initiator_id, params.new_deadline)); },
  });

  // #21 eacn_update_discussions
  api.registerTool({
    name: "eacn_update_discussions",
    description: "Add a discussion message to a task. Synced to all bidders.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, message: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "message", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.updateDiscussions(params.task_id, params.initiator_id, params.message)); },
  });

  // #22 eacn_confirm_budget
  api.registerTool({
    name: "eacn_confirm_budget",
    description: "Respond to a budget confirmation request (when a bid exceeds current budget).",
    parameters: { type: "object", properties: { task_id: { type: "string" }, approved: { type: "boolean" }, new_budget: { type: "number" }, initiator_id: { type: "string" } }, required: ["task_id", "approved", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.confirmBudget(params.task_id, params.initiator_id, params.approved, params.new_budget)); },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Task Operations — Executor (5)
  // ═══════════════════════════════════════════════════════════════════════════

  // #23 eacn_submit_bid
  api.registerTool({
    name: "eacn_submit_bid",
    description: "Submit a bid on a task (confidence + price).",
    parameters: { type: "object", properties: { task_id: { type: "string" }, confidence: { type: "number", description: "0.0-1.0 confidence in ability to complete" }, price: { type: "number", description: "Bid price" }, agent_id: { type: "string" } }, required: ["task_id", "confidence", "price", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.submitBid(params.task_id, params.agent_id, params.confidence, params.price);
      if (res.status && res.status !== "rejected") {
        state.updateTask({ task_id: params.task_id, role: "executor", status: "bidding", domains: [], description_summary: "", created_at: new Date().toISOString() });
      }
      return ok(res);
    },
  });

  // #24 eacn_submit_result
  api.registerTool({
    name: "eacn_submit_result",
    description: "Submit execution result for a task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, content: { type: "object", description: "Result content object" }, agent_id: { type: "string" } }, required: ["task_id", "content", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.submitResult(params.task_id, params.agent_id, params.content);
      try { await net.reportEvent(params.agent_id, "task_completed"); } catch { /* non-critical */ }
      return ok(res);
    },
  });

  // #25 eacn_reject_task
  api.registerTool({
    name: "eacn_reject_task",
    description: "Reject/return a task. Frees the execution slot. Note: rejection affects reputation.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, reason: { type: "string" }, agent_id: { type: "string" } }, required: ["task_id", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.rejectTask(params.task_id, params.agent_id, params.reason);
      try { await net.reportEvent(params.agent_id, "task_rejected"); } catch { /* non-critical */ }
      return ok(res);
    },
  });

  // #26 eacn_create_subtask
  api.registerTool({
    name: "eacn_create_subtask",
    description: "Create a subtask under a parent task. Budget is carved from parent's escrow.",
    parameters: {
      type: "object",
      properties: {
        parent_task_id: { type: "string" }, description: { type: "string" },
        domains: { type: "array", items: { type: "string" } },
        budget: { type: "number" }, deadline: { type: "string" },
        initiator_id: { type: "string", description: "Agent ID of the executor creating the subtask" },
      },
      required: ["parent_task_id", "description", "domains", "budget", "initiator_id"],
    },
    async execute(_id: string, params: any) {
      const task = await net.createSubtask(params.parent_task_id, params.initiator_id, { description: params.description }, params.domains, params.budget, params.deadline);
      return ok({ subtask_id: task.id, parent_task_id: params.parent_task_id, status: task.status, depth: task.depth });
    },
  });

  // #27 eacn_send_message
  api.registerTool({
    name: "eacn_send_message",
    description: "Send a direct message to another Agent (A2A point-to-point).",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Target agent ID" }, content: { type: "string" }, sender_id: { type: "string", description: "Your agent ID" } }, required: ["agent_id", "content", "sender_id"] },
    async execute(_id: string, params: any) {
      return ok({ sent: true, to: params.agent_id, from: params.sender_id, note: "Direct A2A messaging will use WebSocket routing in future versions." });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Reputation (2)
  // ═══════════════════════════════════════════════════════════════════════════

  // #28 eacn_report_event
  api.registerTool({
    name: "eacn_report_event",
    description: "Report a reputation event. Usually called automatically by other tools, but exposed for special cases.",
    parameters: { type: "object", properties: { agent_id: { type: "string" }, event_type: { type: "string", description: "task_completed | task_rejected | task_timeout | bid_declined" } }, required: ["agent_id", "event_type"] },
    async execute(_id: string, params: any) {
      const res = await net.reportEvent(params.agent_id, params.event_type);
      state.updateReputationCache(params.agent_id, res.score);
      return ok(res);
    },
  });

  // #29 eacn_get_reputation
  api.registerTool({
    name: "eacn_get_reputation",
    description: "Query an Agent's global reputation score.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.getReputation(params.agent_id);
      state.updateReputationCache(params.agent_id, res.score);
      return ok(res);
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Economy (2)
  // ═══════════════════════════════════════════════════════════════════════════

  // #30 eacn_get_balance
  api.registerTool({
    name: "eacn_get_balance",
    description: "Query an Agent's account balance: available funds and frozen (escrowed) funds.",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Agent ID to check balance for" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      return ok(await net.getBalance(params.agent_id));
    },
  });

  // #31 eacn_deposit
  api.registerTool({
    name: "eacn_deposit",
    description: "Deposit funds into an Agent's account. Increases available balance.",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Agent ID to deposit funds for" }, amount: { type: "number", description: "Amount to deposit (must be > 0)" } }, required: ["agent_id", "amount"] },
    async execute(_id: string, params: any) {
      return ok(await net.deposit(params.agent_id, params.amount));
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Events (1)
  // ═══════════════════════════════════════════════════════════════════════════

  // #32 eacn_get_events
  api.registerTool({
    name: "eacn_get_events",
    description: "Get pending events. WebSocket connections buffer events in memory; this drains the buffer.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const events = state.drainEvents();
      return ok({ count: events.length, events });
    },
  });
}
