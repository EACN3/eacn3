/**
 * EACN — Native OpenClaw plugin entry point.
 *
 * Registers the same 29 tools as server.ts but via api.registerTool().
 * All logic delegates to the same src/ modules.
 */

import { type AgentCard } from "./src/models.js";
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
// Plugin entry
// ---------------------------------------------------------------------------

export default function (api: any) {
  // Load state
  state.load();

  // ── #1 eacn_connect ────────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_connect",
    description: "Connect to EACN network.",
    parameters: {
      type: "object",
      properties: {
        network_endpoint: { type: "string", description: "Network URL. Defaults to https://network.eacn.dev" },
      },
    },
    async execute(_id: string, params: any) {
      const endpoint = params.network_endpoint ?? "https://network.eacn.dev";
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

  // ── #2 eacn_disconnect ─────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_disconnect",
    description: "Disconnect from EACN network.",
    parameters: { type: "object", properties: {} },
    async execute() {
      stopHeartbeat(); ws.disconnectAll();
      try { await net.unregisterServer(); } catch { /* */ }
      const s = state.getState(); s.server_card = null; s.agents = {};
      state.save();
      return ok({ disconnected: true });
    },
  });

  // ── #3 eacn_heartbeat ──────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_heartbeat",
    description: "Send heartbeat to network.",
    parameters: { type: "object", properties: {} },
    async execute() { return ok(await net.heartbeat()); },
  });

  // ── #4 eacn_server_info ────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_server_info",
    description: "Get current server status.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const s = state.getState();
      if (!s.server_card) return err("Not connected");
      let remote; try { remote = await net.getServer(s.server_card.server_id); } catch { remote = null; }
      return ok({ server_card: s.server_card, network_endpoint: s.network_endpoint, agents_count: Object.keys(s.agents).length, agents: Object.keys(s.agents), tasks_count: Object.keys(s.local_tasks).length, remote_status: remote?.status ?? "unknown" });
    },
  });

  // ── #5 eacn_register_agent ─────────────────────────────────────────────
  api.registerTool({
    name: "eacn_register_agent",
    description: "Register an Agent on the network.",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string" }, description: { type: "string" },
        domains: { type: "array", items: { type: "string" } },
        skills: { type: "array", items: { type: "object", properties: { name: { type: "string" }, description: { type: "string" }, parameters: { type: "object" } } } },
        agent_type: { type: "string", enum: ["executor", "planner"] },
        agent_id: { type: "string" },
      },
      required: ["name", "description", "domains"],
    },
    async execute(_id: string, params: any) {
      const s = state.getState();
      if (!s.server_card) return err("Not connected");
      if (!params.name?.trim()) return err("name cannot be empty");
      if (!params.domains?.length) return err("domains cannot be empty");
      const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;
      const card: AgentCard = { agent_id: agentId, name: params.name, agent_type: params.agent_type ?? "executor", domains: params.domains, skills: params.skills ?? [], url: `plugin://local/agents/${agentId}`, server_id: s.server_card.server_id, network_id: "", description: params.description };
      const res = await net.registerAgent(card);
      state.addAgent(card); ws.connect(agentId);
      return ok({ registered: true, agent_id: agentId, seeds: res.seeds, domains: params.domains });
    },
  });

  // ── #6 eacn_get_agent ──────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_agent",
    description: "Get Agent details by ID.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const local = state.getAgent(params.agent_id);
      if (local) return ok(local);
      return ok(await net.getAgentInfo(params.agent_id));
    },
  });

  // ── #7 eacn_update_agent ───────────────────────────────────────────────
  api.registerTool({
    name: "eacn_update_agent",
    description: "Update Agent info.",
    parameters: { type: "object", properties: { agent_id: { type: "string" }, name: { type: "string" }, domains: { type: "array", items: { type: "string" } }, skills: { type: "array", items: { type: "object" } }, description: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const { agent_id, ...updates } = params;
      const res = await net.updateAgent(agent_id, updates);
      const local = state.getAgent(agent_id);
      if (local) { Object.assign(local, updates); state.addAgent(local); }
      return ok({ updated: true, agent_id, ...res });
    },
  });

  // ── #8 eacn_unregister_agent ───────────────────────────────────────────
  api.registerTool({
    name: "eacn_unregister_agent",
    description: "Unregister an Agent.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.unregisterAgent(params.agent_id);
      ws.disconnect(params.agent_id); state.removeAgent(params.agent_id);
      return ok({ unregistered: true, agent_id: params.agent_id, ...res });
    },
  });

  // ── #9 eacn_list_my_agents ─────────────────────────────────────────────
  api.registerTool({
    name: "eacn_list_my_agents",
    description: "List all Agents registered under this server.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const agents = state.listAgents();
      return ok({ count: agents.length, agents: agents.map((a) => ({ agent_id: a.agent_id, name: a.name, agent_type: a.agent_type, domains: a.domains, ws_connected: ws.isConnected(a.agent_id) })) });
    },
  });

  // ── #10 eacn_discover_agents ───────────────────────────────────────────
  api.registerTool({
    name: "eacn_discover_agents",
    description: "Discover Agents by domain.",
    parameters: { type: "object", properties: { domain: { type: "string" }, requester_id: { type: "string" } }, required: ["domain"] },
    async execute(_id: string, params: any) {
      return ok(await net.discoverAgents(params.domain, params.requester_id));
    },
  });

  // ── #11 eacn_get_task ──────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_task",
    description: "Get full task details.",
    parameters: { type: "object", properties: { task_id: { type: "string" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTask(params.task_id)); },
  });

  // ── #12 eacn_get_task_status ───────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_task_status",
    description: "Query task status (initiator only).",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string" } }, required: ["task_id", "agent_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTaskStatus(params.task_id, params.agent_id)); },
  });

  // ── #13 eacn_list_open_tasks ───────────────────────────────────────────
  api.registerTool({
    name: "eacn_list_open_tasks",
    description: "List tasks open for bidding.",
    parameters: { type: "object", properties: { domains: { type: "string" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.getOpenTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // ── #14 eacn_list_tasks ────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_list_tasks",
    description: "List tasks with filters.",
    parameters: { type: "object", properties: { status: { type: "string" }, initiator_id: { type: "string" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.listTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // ── #15 eacn_create_task ───────────────────────────────────────────────
  api.registerTool({
    name: "eacn_create_task",
    description: "Create a new task.",
    parameters: { type: "object", properties: { description: { type: "string" }, budget: { type: "number" }, domains: { type: "array", items: { type: "string" } }, deadline: { type: "string" }, max_concurrent_bidders: { type: "number" }, expected_output: { type: "string" }, initiator_id: { type: "string" } }, required: ["description", "budget", "initiator_id"] },
    async execute(_id: string, params: any) {
      const taskId = `t-${Date.now().toString(36)}`;
      const localAgents = state.listAgents();
      const matchedLocal = params.domains ? localAgents.filter((a: AgentCard) => a.agent_id !== params.initiator_id && params.domains.some((d: string) => a.domains.includes(d))) : [];
      const task = await net.createTask({ task_id: taskId, initiator_id: params.initiator_id, content: { description: params.description, expected_output: params.expected_output }, domains: params.domains, budget: params.budget, deadline: params.deadline, max_concurrent_bidders: params.max_concurrent_bidders });
      state.updateTask({ task_id: taskId, role: "initiator", status: task.status, domains: params.domains ?? [], description_summary: params.description.slice(0, 100), created_at: new Date().toISOString() });
      return ok({ task_id: taskId, status: task.status, budget: params.budget, local_matches: matchedLocal.map((a: AgentCard) => a.agent_id) });
    },
  });

  // ── #16 eacn_get_task_results ──────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_task_results",
    description: "Retrieve task results.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTaskResults(params.task_id, params.initiator_id)); },
  });

  // ── #17 eacn_select_result ─────────────────────────────────────────────
  api.registerTool({
    name: "eacn_select_result",
    description: "Select winning result.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "agent_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.selectResult(params.task_id, params.initiator_id, params.agent_id)); },
  });

  // ── #18 eacn_close_task ────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_close_task",
    description: "Close a task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.closeTask(params.task_id, params.initiator_id)); },
  });

  // ── #19 eacn_update_deadline ───────────────────────────────────────────
  api.registerTool({
    name: "eacn_update_deadline",
    description: "Update task deadline.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, new_deadline: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "new_deadline", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.updateDeadline(params.task_id, params.initiator_id, params.new_deadline)); },
  });

  // ── #20 eacn_update_discussions ────────────────────────────────────────
  api.registerTool({
    name: "eacn_update_discussions",
    description: "Add discussion message to task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, message: { type: "string" }, initiator_id: { type: "string" } }, required: ["task_id", "message", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.updateDiscussions(params.task_id, params.initiator_id, params.message)); },
  });

  // ── #21 eacn_confirm_budget ────────────────────────────────────────────
  api.registerTool({
    name: "eacn_confirm_budget",
    description: "Respond to budget confirmation request.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, approved: { type: "boolean" }, new_budget: { type: "number" }, initiator_id: { type: "string" } }, required: ["task_id", "approved", "initiator_id"] },
    async execute(_id: string, params: any) { return ok(await net.confirmBudget(params.task_id, params.initiator_id, params.approved, params.new_budget)); },
  });

  // ── #22 eacn_submit_bid ────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_submit_bid",
    description: "Submit a bid on a task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, confidence: { type: "number" }, price: { type: "number" }, agent_id: { type: "string" } }, required: ["task_id", "confidence", "price", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.submitBid(params.task_id, params.agent_id, params.confidence, params.price);
      if (res.status === "accepted") state.updateTask({ task_id: params.task_id, role: "executor", status: "bidding", domains: [], description_summary: "", created_at: new Date().toISOString() });
      return ok(res);
    },
  });

  // ── #23 eacn_submit_result ─────────────────────────────────────────────
  api.registerTool({
    name: "eacn_submit_result",
    description: "Submit execution result.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, content: { type: "object" }, agent_id: { type: "string" } }, required: ["task_id", "content", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.submitResult(params.task_id, params.agent_id, params.content);
      try { await net.reportEvent(params.agent_id, "task_completed"); } catch { /* */ }
      return ok(res);
    },
  });

  // ── #24 eacn_reject_task ───────────────────────────────────────────────
  api.registerTool({
    name: "eacn_reject_task",
    description: "Reject/return a task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, reason: { type: "string" }, agent_id: { type: "string" } }, required: ["task_id", "agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.rejectTask(params.task_id, params.agent_id, params.reason);
      try { await net.reportEvent(params.agent_id, "task_rejected"); } catch { /* */ }
      return ok(res);
    },
  });

  // ── #25 eacn_create_subtask ────────────────────────────────────────────
  api.registerTool({
    name: "eacn_create_subtask",
    description: "Create subtask under a parent task.",
    parameters: { type: "object", properties: { parent_task_id: { type: "string" }, description: { type: "string" }, domains: { type: "array", items: { type: "string" } }, budget: { type: "number" }, deadline: { type: "string" }, initiator_id: { type: "string" } }, required: ["parent_task_id", "description", "domains", "budget", "initiator_id"] },
    async execute(_id: string, params: any) {
      const task = await net.createSubtask(params.parent_task_id, params.initiator_id, { description: params.description }, params.domains, params.budget, params.deadline);
      return ok({ subtask_id: task.id, parent_task_id: params.parent_task_id, status: task.status, depth: task.depth });
    },
  });

  // ── #26 eacn_send_message ──────────────────────────────────────────────
  api.registerTool({
    name: "eacn_send_message",
    description: "Send direct message to another Agent.",
    parameters: { type: "object", properties: { agent_id: { type: "string" }, content: { type: "string" }, sender_id: { type: "string" } }, required: ["agent_id", "content", "sender_id"] },
    async execute(_id: string, params: any) {
      return ok({ sent: true, to: params.agent_id, from: params.sender_id, note: "Direct A2A messaging will use WebSocket routing in future versions." });
    },
  });

  // ── #27 eacn_report_event ──────────────────────────────────────────────
  api.registerTool({
    name: "eacn_report_event",
    description: "Report a reputation event.",
    parameters: { type: "object", properties: { agent_id: { type: "string" }, event_type: { type: "string" } }, required: ["agent_id", "event_type"] },
    async execute(_id: string, params: any) {
      const res = await net.reportEvent(params.agent_id, params.event_type);
      state.updateReputationCache(params.agent_id, res.score);
      return ok(res);
    },
  });

  // ── #28 eacn_get_reputation ────────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_reputation",
    description: "Query Agent reputation score.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.getReputation(params.agent_id);
      state.updateReputationCache(params.agent_id, res.score);
      return ok(res);
    },
  });

  // ── #29 eacn_get_events ────────────────────────────────────────────────
  api.registerTool({
    name: "eacn_get_events",
    description: "Get pending push events.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const events = state.drainEvents();
      return ok({ count: events.length, events });
    },
  });
}
