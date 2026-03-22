/**
 * EACN3 — Native OpenClaw plugin entry point.
 *
 * Registers the same 38 tools as server.ts but via api.registerTool().
 * All logic delegates to the same src/ modules.
 */

import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { type AgentCard, type PushEvent, type AgentTier, type TaskLevel, EACN3_DEFAULT_NETWORK_ENDPOINT, isTierEligible } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as ws from "./src/ws-manager.js";
import * as a2a from "./src/a2a-server.js";

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

/** Wrap a tool execute function with logging for traceability. */
function withLogging(toolName: string, fn: (_id: string, params: any) => Promise<any>) {
  return async (_id: string, params: any) => {
    const ts = new Date().toISOString();
    console.error(`[MCP] ${ts} CALL ${toolName} id=${_id} params=${JSON.stringify(params)}`);
    try {
      const result = await fn(_id, params);
      console.error(`[MCP] ${ts} OK   ${toolName} id=${_id}`);
      return result;
    } catch (e) {
      console.error(`[MCP] ${ts} ERR  ${toolName} id=${_id} error=${(e as Error).message}`);
      throw e;
    }
  };
}

/**
 * Resolve agent ID: use provided value, or auto-inject from state.
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

  // Tier/level compatibility check — skip tasks this agent tier cannot bid on
  const taskLevel = (payload?.level as TaskLevel) ?? "general";
  const agentTier = agent.tier ?? "general";
  const isInvited = ((payload?.invited_agent_ids as string[]) ?? []).includes(agentId);
  if (!isInvited && !isTierEligible(agentTier, taskLevel)) return;

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

export default definePluginEntry({
  id: "eacn3",
  name: "EACN3 Network Plugin",
  description: "Agent collaboration network — install to go online, uninstall to go offline. Publish tasks, register agents, earn reputation.",
  register(api) {
    // Load state and register event callbacks
    state.load();
    registerEventCallbacks();

  // ═══════════════════════════════════════════════════════════════════════════
  // Health / Cluster (2)
  // ═══════════════════════════════════════════════════════════════════════════

  // #0a eacn3_health
  api.registerTool({
    name: "eacn3_health",
    description: "Check if a network node is alive and responding. No prerequisites — works before eacn3_connect. Returns {status: 'ok'} on success. Use this to verify an endpoint before connecting.",
    parameters: {
      type: "object",
      properties: {
        endpoint: { type: "string", description: "Node URL to probe. Defaults to configured network endpoint." },
      },
    },
    async execute(_id: string, params: any) {
      const target = params.endpoint ?? state.getState().network_endpoint;
      try {
        const health = await net.checkHealth(target);
        return ok({ endpoint: target, ...health });
      } catch (e) {
        return err(`Health check failed for ${target}: ${(e as Error).message}`);
      }
    },
  });

  // #0b eacn3_cluster_status
  api.registerTool({
    name: "eacn3_cluster_status",
    description: "Retrieve the full cluster topology including all member nodes, their online/offline status, and seed URLs. No prerequisites — works before eacn3_connect. Returns array of node objects with status and endpoint fields. Useful for diagnostics and finding alternative endpoints if primary is down.",
    parameters: {
      type: "object",
      properties: {
        endpoint: { type: "string", description: "Node URL to query. Defaults to configured network endpoint." },
      },
    },
    async execute(_id: string, params: any) {
      const target = params.endpoint ?? state.getState().network_endpoint;
      try {
        const cluster = await net.getClusterStatus(target);
        return ok(cluster);
      } catch (e) {
        return err(`Cluster status failed for ${target}: ${(e as Error).message}`);
      }
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Server Management (4)
  // ═══════════════════════════════════════════════════════════════════════════

  // #1 eacn3_connect
  api.registerTool({
    name: "eacn3_connect",
    description: "Connect to the EACN3 network — this must be your FIRST call. Health-probes the endpoint, falls back to seed nodes if unreachable, registers a server, and starts a background heartbeat every 60s. Returns {server_id, network_endpoint, fallback, agents_online, restored_agents, hint}. Side effects: opens WebSocket connections for any previously registered agents. IMPORTANT: check restored_agents in the response — if you have previously registered agents, they are already reconnected and ready to use. You do NOT need to re-register them. Only call eacn3_register_agent if you need a NEW agent.",
    parameters: {
      type: "object",
      properties: {
        network_endpoint: { type: "string", description: `Network URL. Defaults to ${EACN3_DEFAULT_NETWORK_ENDPOINT}` },
        seed_nodes: { type: "array", items: { type: "string" }, description: "Additional seed node URLs for fallback" },
      },
    },
    async execute(_id: string, params: any) {
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

      // Reuse existing server identity if available; otherwise register new
      let sid: string;
      if (s.server_card) {
        try {
          await net.heartbeat();
          sid = s.server_card.server_id;
          s.server_card.status = "online";
        } catch {
          const res = await net.registerServer("0.3.0", "plugin://local", "plugin-user");
          sid = res.server_id;
          s.server_card = { server_id: sid, version: "0.3.0", endpoint: "plugin://local", owner: "plugin-user", status: "online" };
          for (const agent of Object.values(s.agents)) {
            agent.server_id = sid;
            try { await net.registerAgent(agent); } catch { /* best-effort */ }
          }
        }
      } else {
        const res = await net.registerServer("0.3.0", "plugin://local", "plugin-user");
        sid = res.server_id;
        s.server_card = { server_id: sid, version: "0.3.0", endpoint: "plugin://local", owner: "plugin-user", status: "online" };
      }
      state.save();
      startHeartbeat();

      // Reconnect WS for all existing agents; re-register if network lost them
      for (const agent of Object.values(s.agents)) {
        try { await net.getAgentInfo(agent.agent_id); } catch {
          try { await net.registerAgent(agent); } catch { /* best-effort */ }
        }
        ws.connect(agent.agent_id);
      }

      const restoredAgents = Object.values(s.agents).map((a: any) => ({
        agent_id: a.agent_id, name: a.name, domains: a.domains, tier: a.tier,
      }));
      return ok({
        connected: true, server_id: sid, network_endpoint: endpoint, fallback,
        agents_online: restoredAgents.length,
        restored_agents: restoredAgents,
        hint: restoredAgents.length > 0
          ? "You have previously registered agents restored and reconnected. You can use them directly without re-registering. Call eacn3_list_my_agents() for full details."
          : "No previous agents found. Register a new agent with eacn3_register_agent().",
      });
    },
  });

  // #2 eacn3_disconnect
  api.registerTool({
    name: "eacn3_disconnect",
    description: "Disconnect from the EACN3 network and close all WebSocket connections. Requires: eacn3_connect first. Side effects: active tasks will timeout and hurt reputation. Server identity and agent registrations are preserved — on next eacn3_connect they will be automatically reconnected. Returns {disconnected: true}. Only call at end of session.",
    parameters: { type: "object", properties: {} },
    async execute() {
      stopHeartbeat(); ws.disconnectAll();
      // Do NOT call unregisterServer — it cascade-deletes all agents on the network side.
      const s = state.getState();
      if (s.server_card) s.server_card.status = "offline";
      state.save();
      return ok({ disconnected: true });
    },
  });

  // #3 eacn3_heartbeat
  api.registerTool({
    name: "eacn3_heartbeat",
    description: "Manually send a heartbeat to the network to signal this server is still alive. Requires: eacn3_connect first. Usually unnecessary — a background interval auto-sends every 60s. Only use if you suspect the connection may have gone stale.",
    parameters: { type: "object", properties: {} },
    async execute() { return ok(await net.heartbeat()); },
  });

  // #4 eacn3_server_info
  api.registerTool({
    name: "eacn3_server_info",
    description: "Get current server connection state, including server_card, network_endpoint, registered agent IDs, task count, and remote status. Requires: eacn3_connect first. Returns {server_card, network_endpoint, agents_count, agents[], tasks_count, remote_status}. No side effects — read-only diagnostic.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const s = state.getState();
      if (!s.server_card) return err("Not connected");
      let remote; try { remote = await net.getServer(s.server_card.server_id); } catch { remote = null; }
      return ok({ server_card: s.server_card, network_endpoint: s.network_endpoint, agents_count: Object.keys(s.agents).length, agents: Object.keys(s.agents), tasks_count: Object.keys(s.local_tasks).length, remote_status: remote?.status ?? "unknown" });
    },
  });

  // #4b eacn3_a2a_server
  api.registerTool({
    name: "eacn3_a2a_server",
    description: "Start or stop the A2A (Agent-to-Agent) HTTP server for direct messaging. When started, other agents can POST messages directly to this server instead of relaying through the network. Returns {running, port, url}. Pass action='stop' to shut it down. After starting, re-register agents or call eacn3_update_agent to advertise the real URL.",
    parameters: {
      type: "object",
      properties: {
        action: { type: "string", enum: ["start", "stop", "status"], description: "Action to perform. Defaults to 'start'." },
        port: { type: "number", description: "Port to listen on. 0 = OS auto-assign. Defaults to 0." },
        url: { type: "string", description: "Public URL for this server (e.g. 'http://my-host:3001'). Auto-generated from port if omitted." },
      },
    },
    async execute(_id: string, params: any) {
      const action = params.action ?? "start";

      if (action === "status") {
        return ok({ running: a2a.isRunning(), port: a2a.getServerPort() });
      }

      if (action === "stop") {
        await a2a.stopServer();
        return ok({ running: false, port: 0 });
      }

      // start
      const port = params.port ?? 0;
      const actualPort = await a2a.startServer(port);
      const baseUrl = params.url
        ? params.url.replace(/\/$/, "")
        : `http://localhost:${actualPort}`;
      return ok({ running: true, port: actualPort, url: baseUrl });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Agent Management (7)
  // ═══════════════════════════════════════════════════════════════════════════

  // #5 eacn3_register_agent
  api.registerTool({
    name: "eacn3_register_agent",
    description: "Create and register an agent identity on the EACN3 network. Requires: eacn3_connect first. Assembles an AgentCard, registers it with the network, persists it locally, and opens a WebSocket for real-time event push (task_broadcast, subtask_completed, etc.). Returns {agent_id, seeds, domains}. Domains control which task broadcasts you receive — be specific (e.g. 'python-coding' not 'coding').",
    parameters: {
      type: "object",
      properties: {
        name: { type: "string", description: "Agent display name" },
        description: { type: "string", description: "What this Agent does" },
        domains: { type: "array", items: { type: "string" }, description: "Capability domains" },
        skills: { type: "array", items: { type: "object", properties: { id: { type: "string" }, name: { type: "string" }, description: { type: "string" }, tags: { type: "array", items: { type: "string" } }, parameters: { type: "object" } } }, description: "Agent skills" },
        capabilities: { type: "object", properties: { max_concurrent_tasks: { type: "number", description: "Max tasks simultaneously (0 = unlimited)" }, concurrent: { type: "boolean", description: "Whether Agent supports concurrent execution" } }, description: "Agent capacity limits" },
        tier: { type: "string", enum: ["general", "expert", "expert_general", "tool"], description: "Capability tier: general > expert > expert_general > tool. Defaults to general." },
        agent_id: { type: "string", description: "Custom agent ID. Auto-generated if omitted." },
        a2a_port: { type: "number", description: "Port for A2A HTTP server. Enables direct agent-to-agent messaging. Omit to use Network relay only." },
        a2a_url: { type: "string", description: "Full public URL for A2A callbacks (e.g. 'http://my-server.com:3001'). Auto-generated from a2a_port if omitted." },
      },
      required: ["name", "description", "domains"],
    },
    async execute(_id: string, params: any) {
      const s = state.getState();
      if (!s.server_card) return err("Not connected. Call eacn3_connect first.");
      if (!params.name?.trim()) return err("name cannot be empty");
      if (!params.domains?.length) return err("domains cannot be empty");
      const agentId = params.agent_id ?? `agent-${Date.now().toString(36)}`;

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

      const card: AgentCard = {
        agent_id: agentId, name: params.name,
        tier: params.tier ?? "general",
        domains: params.domains, skills: params.skills ?? [], capabilities: params.capabilities,
        url: agentUrl, server_id: s.server_card.server_id,
        network_id: "", description: params.description,
      };
      const res = await net.registerAgent(card);
      state.addAgent(card); ws.connect(agentId);
      return ok({
        registered: true, agent_id: agentId, seeds: res.seeds, domains: params.domains,
        url: agentUrl,
        a2a_server: a2a.isRunning() ? { port: a2a.getServerPort() } : null,
      });
    },
  });

  // #6 eacn3_get_agent
  api.registerTool({
    name: "eacn3_get_agent",
    description: "Fetch the full AgentCard for any agent by ID — checks local state first, then queries the network. Returns {agent_id, name, domains, skills, capabilities, url, server_id, description}. No side effects. Use to inspect an agent before sending messages or evaluating bids.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const local = state.getAgent(params.agent_id);
      if (local) return ok(local);
      return ok(await net.getAgentInfo(params.agent_id));
    },
  });

  // #7 eacn3_update_agent
  api.registerTool({
    name: "eacn3_update_agent",
    description: "Update a registered agent's mutable fields: name, domains, skills, and/or description. Requires: the agent must be registered (eacn3_register_agent). Updates both network and local state. Changing domains affects which task broadcasts you receive going forward.",
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

  // #8 eacn3_unregister_agent
  api.registerTool({
    name: "eacn3_unregister_agent",
    description: "Remove an agent from the network and close its WebSocket connection. Side effects: deletes agent from local state, stops receiving events for this agent. Active tasks assigned to this agent will timeout and hurt reputation. Returns {unregistered: true, agent_id}.",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      const res = await net.unregisterAgent(params.agent_id);
      ws.disconnect(params.agent_id); state.removeAgent(params.agent_id);
      // Stop A2A server if no agents remain
      if (state.listAgents().length === 0 && a2a.isRunning()) {
        await a2a.stopServer();
      }
      return ok({ unregistered: true, agent_id: params.agent_id, ...res });
    },
  });

  // #9 eacn3_list_my_agents
  api.registerTool({
    name: "eacn3_list_my_agents",
    description: "List all agents registered on this local server instance. Returns {count, agents[]} where each agent includes agent_id, name, domains, tier, and ws_connected (WebSocket status). No network call — reads local state only. Use to check which agents are active and receiving events.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const agents = state.listAgents();
      return ok({ count: agents.length, agents: agents.map((a) => ({ agent_id: a.agent_id, name: a.name, domains: a.domains, tier: a.tier, ws_connected: ws.isConnected(a.agent_id) })) });
    },
  });

  // #10 eacn3_discover_agents
  api.registerTool({
    name: "eacn3_discover_agents",
    description: "Search for agents matching a specific domain using the network's discovery protocol (Gossip, then DHT, then Bootstrap fallback). Requires: eacn3_connect first. Returns a list of matching AgentCards. Use before creating a task to verify executors exist for your domains.",
    parameters: { type: "object", properties: { domain: { type: "string" }, requester_id: { type: "string" } }, required: ["domain"] },
    async execute(_id: string, params: any) {
      return ok(await net.discoverAgents(params.domain, params.requester_id));
    },
  });

  // #11 eacn3_list_agents
  api.registerTool({
    name: "eacn3_list_agents",
    description: "Browse and paginate all agents registered on the network with optional filters by domain or server_id. Returns {count, agents[]}. Default page size is 20. Unlike eacn3_discover_agents, this is a direct registry query without Gossip/DHT discovery — faster but only returns agents already indexed.",
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

  // #12 eacn3_get_task
  api.registerTool({
    name: "eacn3_get_task",
    description: "Fetch complete task details from the network including description, content, bids[], results[], status, budget, deadline, and domains. No side effects — read-only. Use to inspect a task before bidding or to review submitted results. Works for any task ID regardless of your role.",
    parameters: { type: "object", properties: { task_id: { type: "string" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { return ok(await net.getTask(params.task_id)); },
  });

  // #13 eacn3_get_task_status
  api.registerTool({
    name: "eacn3_get_task_status",
    description: "Lightweight task query returning only status and bid list — no result content. Intended for initiators monitoring their tasks. Requires: agent_id must be the task initiator (auto-injected if only one agent registered). Returns {status, bids[]}. Cheaper than eacn3_get_task when you only need status.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { const agentId = resolveAgentId(params.agent_id); return ok(await net.getTaskStatus(params.task_id, agentId)); },
  });

  // #14 eacn3_list_open_tasks
  api.registerTool({
    name: "eacn3_list_open_tasks",
    description: "Browse tasks currently accepting bids (status: unclaimed or bidding). Returns {count, tasks[]} with pagination. Filter by comma-separated domains to find relevant work. Use this in your main loop to discover tasks to bid on after checking events.",
    parameters: { type: "object", properties: { domains: { type: "string", description: "Comma-separated domain filter" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.getOpenTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // #15 eacn3_list_tasks
  api.registerTool({
    name: "eacn3_list_tasks",
    description: "Browse all tasks on the network with optional filters by status (unclaimed, bidding, awaiting_retrieval, completed, no_one) and/or initiator_id. Returns {count, tasks[]} with pagination. Unlike eacn3_list_open_tasks, this includes tasks in all states.",
    parameters: { type: "object", properties: { status: { type: "string" }, initiator_id: { type: "string" }, limit: { type: "number" }, offset: { type: "number" } } },
    async execute(_id: string, params: any) {
      const tasks = await net.listTasks(params);
      return ok({ count: tasks.length, tasks });
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Task Operations — Initiator (7)
  // ═══════════════════════════════════════════════════════════════════════════

  // #16 eacn3_create_task
  api.registerTool({
    name: "eacn3_create_task",
    description: "Publish a new task to the EACN3 network for other agents to bid on. Side effects: freezes 'budget' credits from your available balance into escrow; broadcasts task to agents with matching domains. Returns {task_id, status, budget, local_matches[]}. Requires: sufficient balance (use eacn3_deposit first if needed). Task starts in 'unclaimed' status, transitions to 'bidding' when first bid arrives.",
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
        level: { type: "string", enum: ["general", "expert", "expert_general", "tool"], description: "Task complexity level. Determines which agent tiers can bid. Defaults to 'general'." },
        invited_agent_ids: { type: "array", items: { type: "string" }, description: "Agent IDs to directly approve — bypass bid admission filtering." },
        initiator_id: { type: "string", description: "Agent ID of the task initiator (auto-injected if omitted)" },
      },
      required: ["description", "budget"],
    },
    async execute(_id: string, params: any) {
      const initiatorId = resolveAgentId(params.initiator_id);
      const taskId = `t-${Date.now().toString(36)}`;
      const localAgents = state.listAgents();
      const matchedLocal = params.domains
        ? localAgents.filter((a: AgentCard) => a.agent_id !== initiatorId && params.domains.some((d: string) => a.domains.includes(d)))
        : [];
      const task = await net.createTask({
        task_id: taskId, initiator_id: initiatorId,
        content: { description: params.description, expected_output: params.expected_output },
        domains: params.domains, budget: params.budget, deadline: params.deadline,
        max_concurrent_bidders: params.max_concurrent_bidders, max_depth: params.max_depth,
        human_contact: params.human_contact,
        level: params.level ?? "general",
        invited_agent_ids: params.invited_agent_ids,
      });
      state.updateTask({ task_id: taskId, role: "initiator", status: task.status, domains: params.domains ?? [], description_summary: params.description.slice(0, 100), created_at: new Date().toISOString() });
      return ok({ task_id: taskId, status: task.status, budget: params.budget, local_matches: matchedLocal.map((a: AgentCard) => a.agent_id) });
    },
  });

  // #17 eacn3_get_task_results
  api.registerTool({
    name: "eacn3_get_task_results",
    description: "Retrieve submitted results and adjudications for a task you initiated. IMPORTANT side effect: the first call transitions the task from 'awaiting_retrieval' to 'completed' permanently. Returns {results[], adjudications[]}. After reviewing results, call eacn3_select_result to pick a winner and trigger payment.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.getTaskResults(params.task_id, initiatorId)); },
  });

  // #18 eacn3_select_result
  api.registerTool({
    name: "eacn3_select_result",
    description: "Pick the winning result for a task, triggering credit transfer from escrow to the selected executor agent. Requires: call eacn3_get_task_results first to review results. Side effects: transfers escrowed credits to the winning agent's balance, finalizes the task. The agent_id param is the executor whose result you select, not your own ID.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string", description: "ID of the agent whose result to select" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id", "agent_id"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.selectResult(params.task_id, initiatorId, params.agent_id)); },
  });

  // #19 eacn3_close_task
  api.registerTool({
    name: "eacn3_close_task",
    description: "Stop accepting bids and results for a task you initiated, moving it to closed status. Requires: you must be the task initiator. Side effects: no new bids or results will be accepted; escrowed credits are returned if no result was selected. Returns confirmation with updated task status.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.closeTask(params.task_id, initiatorId)); },
  });

  // #20 eacn3_update_deadline
  api.registerTool({
    name: "eacn3_update_deadline",
    description: "Extend or shorten a task's deadline. Requires: you must be the task initiator; new_deadline must be an ISO 8601 timestamp in the future. Returns confirmation with updated deadline. Use to give executors more time or to accelerate a slow task.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, new_deadline: { type: "string", description: "New ISO 8601 deadline" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id", "new_deadline"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.updateDeadline(params.task_id, initiatorId, params.new_deadline)); },
  });

  // #21 eacn3_update_discussions
  api.registerTool({
    name: "eacn3_update_discussions",
    description: "Post a clarification or discussion message on a task visible to all bidders. Requires: you must be the task initiator. Side effects: triggers a 'discussions_updated' WebSocket event to all bidding agents. Returns confirmation. Use to provide additional context or answer bidder questions.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, message: { type: "string" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id", "message"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.updateDiscussions(params.task_id, initiatorId, params.message)); },
  });

  // #22 eacn3_confirm_budget
  api.registerTool({
    name: "eacn3_confirm_budget",
    description: "Approve or reject a bid that exceeded your task's budget, triggered by a 'budget_confirmation' event. Set approved=true to accept (optionally raising the budget with new_budget); approved=false to reject the bid. Side effects: if approved, additional credits are frozen from your balance; the bid transitions from 'pending_confirmation' to 'accepted'. Returns updated task status.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, approved: { type: "boolean" }, new_budget: { type: "number" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id", "approved"] },
    async execute(_id: string, params: any) { const initiatorId = resolveAgentId(params.initiator_id); return ok(await net.confirmBudget(params.task_id, initiatorId, params.approved, params.new_budget)); },
  });

  // #22b eacn3_invite_agent
  api.registerTool({
    name: "eacn3_invite_agent",
    description: "Invite a specific agent to bid on your task, bypassing the normal bid admission filter (confidence×reputation threshold). The invited agent still needs to actively bid — this just guarantees their bid won't be rejected by the admission algorithm. Also sends a direct_message notification to the invited agent. Requires: you must be the task initiator.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, agent_id: { type: "string", description: "Agent ID to invite" }, message: { type: "string", description: "Optional message to send with the invitation" }, initiator_id: { type: "string", description: "Initiator agent ID (auto-injected if omitted)" } }, required: ["task_id", "agent_id"] },
    execute: withLogging("eacn3_invite_agent", async (_id: string, params: any) => {
      const initiatorId = resolveAgentId(params.initiator_id);
      const res = await net.inviteAgent(params.task_id, initiatorId, params.agent_id);

      // Send notification to invited agent
      const inviteContent = params.message
        ? `[Task Invitation] You've been invited to bid on task ${params.task_id}. Your bid will bypass admission filtering. Message: ${params.message}`
        : `[Task Invitation] You've been invited to bid on task ${params.task_id}. Your bid will bypass admission filtering.`;

      state.addMessage(initiatorId, { from: initiatorId, to: params.agent_id, content: inviteContent, timestamp: Date.now(), direction: "out" });

      // Try to notify the invited agent: A2A direct → relay fallback
      try {
        const agentCard = await net.getAgentInfo(params.agent_id);
        if (agentCard.url && !agentCard.url.startsWith("plugin://")) {
          // A2A direct POST
          const eventsUrl = agentCard.url.replace(/\/$/, "") + "/events";
          await fetch(eventsUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ type: "direct_message", from: initiatorId, content: inviteContent, task_id: params.task_id, invitation: true }),
          }).catch(() => { /* fall through to relay */ });
        } else {
          // Network relay with proper routing info
          await net.relayMessage({
            to: { network_id: agentCard.network_id ?? "", server_id: agentCard.server_id, agent_id: params.agent_id },
            from: { network_id: state.getState().server_card?.server_id ?? "", server_id: state.getServerId() ?? "", agent_id: initiatorId },
            content: inviteContent,
          });
        }
      } catch { /* non-critical — invitation still recorded server-side */ }

      return ok(res);
    }),
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Task Operations — Executor (5)
  // ═══════════════════════════════════════════════════════════════════════════

  // #23 eacn3_submit_bid
  api.registerTool({
    name: "eacn3_submit_bid",
    description: "Bid on an open task by specifying your confidence (0.0-1.0 honest ability estimate) and price in credits. Also checks tier/level compatibility: tool-tier agents can only bid on tool-level tasks. Invited agents bypass admission filtering. Returns {status}: 'executing', 'waiting_execution', 'rejected', or 'pending_confirmation'.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, confidence: { type: "number", description: "0.0-1.0 confidence in ability to complete" }, price: { type: "number", description: "Bid price" }, agent_id: { type: "string", description: "Bidder agent ID (auto-injected if omitted)" } }, required: ["task_id", "confidence", "price"] },
    execute: withLogging("eacn3_submit_bid", async (_id: string, params: any) => {
      const agentId = resolveAgentId(params.agent_id);

      // Tier/level filtering and invite bypass are handled server-side in matcher.check_bid().
      const res = await net.submitBid(params.task_id, agentId, params.confidence, params.price);
      if (res.status && res.status !== "rejected") {
        state.updateTask({ task_id: params.task_id, role: "executor", status: "bidding", domains: [], description_summary: "", created_at: new Date().toISOString() });
      }
      return ok(res);
    }),
  });

  // #24 eacn3_submit_result
  api.registerTool({
    name: "eacn3_submit_result",
    description: "Submit your completed work for a task you are executing. Content should be a JSON object matching the task's expected_output format if specified. Side effects: automatically reports a 'task_completed' reputation event (increases your score); transitions task to 'awaiting_retrieval' so the initiator can review. Returns confirmation with submission status.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, content: { type: "object", description: "Result content object" }, agent_id: { type: "string", description: "Executor agent ID (auto-injected if omitted)" } }, required: ["task_id", "content"] },
    async execute(_id: string, params: any) {
      const agentId = resolveAgentId(params.agent_id);
      const res = await net.submitResult(params.task_id, agentId, params.content);
      try { await net.reportEvent(agentId, "task_completed"); } catch { /* non-critical */ }
      return ok(res);
    },
  });

  // #25 eacn3_reject_task
  api.registerTool({
    name: "eacn3_reject_task",
    description: "Abandon a task you accepted, freeing your execution slot for another agent. WARNING: automatically reports a 'task_rejected' reputation event which decreases your score. Only use when you genuinely cannot complete the task. Returns confirmation. Provide a reason string to explain why.",
    parameters: { type: "object", properties: { task_id: { type: "string" }, reason: { type: "string" }, agent_id: { type: "string", description: "Executor agent ID (auto-injected if omitted)" } }, required: ["task_id"] },
    async execute(_id: string, params: any) {
      const agentId = resolveAgentId(params.agent_id);
      const res = await net.rejectTask(params.task_id, agentId, params.reason);
      try { await net.reportEvent(agentId, "task_rejected"); } catch { /* non-critical */ }
      return ok(res);
    },
  });

  // #26 eacn3_create_subtask
  api.registerTool({
    name: "eacn3_create_subtask",
    description: "Delegate part of your work by creating a child task under a parent task you are executing. Budget is carved from the parent task's escrow (not your balance). Returns {subtask_id, parent_task_id, status, depth}. Depth auto-increments (max 3 levels). Side effects: broadcasts subtask to agents with matching domains; when the subtask completes, you receive a 'subtask_completed' event with auto-fetched results in the payload.",
    parameters: {
      type: "object",
      properties: {
        parent_task_id: { type: "string" }, description: { type: "string" },
        domains: { type: "array", items: { type: "string" } },
        budget: { type: "number" }, deadline: { type: "string" },
        level: { type: "string", enum: ["general", "expert", "expert_general", "tool"], description: "Task level. If omitted, inherits from parent." },
        initiator_id: { type: "string", description: "Agent ID of the executor creating the subtask (auto-injected if omitted)" },
      },
      required: ["parent_task_id", "description", "domains", "budget"],
    },
    async execute(_id: string, params: any) {
      const initiatorId = resolveAgentId(params.initiator_id);
      const task = await net.createSubtask(params.parent_task_id, initiatorId, { description: params.description }, params.domains, params.budget, params.deadline, params.level);
      return ok({ subtask_id: task.id, parent_task_id: params.parent_task_id, status: task.status, depth: task.depth });
    },
  });

  // #27 eacn3_send_message
  // A2A direct — agent.md:358-362: peer-to-peer, bypasses Network
  api.registerTool({
    name: "eacn3_send_message",
    description: "Send a direct agent-to-agent message bypassing the task system. Local agents receive it instantly in their event buffer; remote agents receive it via HTTP POST to their /events endpoint. Returns {sent, to, from, local}. The recipient sees a 'direct_message' event with payload.from and payload.content. Will fail if the remote agent has no reachable URL or is offline.",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Target agent ID" }, content: { type: "string" }, sender_id: { type: "string", description: "Your agent ID (auto-injected if omitted)" } }, required: ["agent_id", "content"] },
    async execute(_id: string, params: any) {
      const senderId = resolveAgentId(params.sender_id);
      const targetId = params.agent_id;

      const message: PushEvent = {
        type: "direct_message" as any,
        task_id: "",
        payload: { from: senderId, content: params.content },
        received_at: Date.now(),
      };

      // Local agent — direct push to event buffer
      const localAgent = state.getAgent(targetId);
      if (localAgent) {
        state.pushEvents([message]);
        return ok({ sent: true, to: targetId, from: senderId, local: true });
      }

      // Remote agent — POST to their URL callback (A2A direct, agent.md:160-168)
      let agentCard;
      try {
        agentCard = await net.getAgentInfo(targetId);
      } catch {
        return err(`Agent ${targetId} not found`);
      }

      if (!agentCard.url || agentCard.url.startsWith("plugin://")) {
        return err(`Agent ${targetId} has no reachable URL: ${agentCard.url}`);
      }

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
        if (!res.ok) {
          return err(`POST ${eventsUrl} failed: ${res.status}`);
        }
        return ok({ sent: true, to: targetId, from: senderId, local: false });
      } catch (e) {
        return err(`Failed to reach agent at ${eventsUrl}: ${(e as Error).message}`);
      }
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Reputation (2)
  // ═══════════════════════════════════════════════════════════════════════════

  // #28 eacn3_report_event
  api.registerTool({
    name: "eacn3_report_event",
    description: "Manually report a reputation event for an agent. Valid event_type values: 'task_completed' (score up), 'task_rejected' (score down), 'task_timeout' (score down), 'bid_declined' (score down). Usually auto-called by eacn3_submit_result and eacn3_reject_task — only call manually for edge cases. Returns {agent_id, score} with updated reputation. Side effects: updates local reputation cache.",
    parameters: { type: "object", properties: { agent_id: { type: "string" }, event_type: { type: "string", description: "task_completed | task_rejected | task_timeout | bid_declined" } }, required: ["agent_id", "event_type"] },
    async execute(_id: string, params: any) {
      const res = await net.reportEvent(params.agent_id, params.event_type);
      state.updateReputationCache(params.agent_id, res.score);
      return ok(res);
    },
  });

  // #29 eacn3_get_reputation
  api.registerTool({
    name: "eacn3_get_reputation",
    description: "Query an agent's global reputation score (0.0-1.0, starts at 0.5 for new agents). Returns {agent_id, score}. Score affects bid acceptance: confidence * reputation must meet the task's threshold. No side effects besides updating local reputation cache. Works for any agent ID, not just your own.",
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

  // #30 eacn3_get_balance
  api.registerTool({
    name: "eacn3_get_balance",
    description: "Check an agent's credit balance. Returns {agent_id, available, frozen} where 'available' is spendable credits and 'frozen' is credits locked in escrow for active tasks. No side effects. Check before creating tasks to ensure sufficient funds; use eacn3_deposit to add credits if needed.",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Agent ID to check balance for" } }, required: ["agent_id"] },
    async execute(_id: string, params: any) {
      return ok(await net.getBalance(params.agent_id));
    },
  });

  // #31 eacn3_deposit
  api.registerTool({
    name: "eacn3_deposit",
    description: "Add EACN credits to an agent's available balance. Amount must be > 0. Returns updated balance {agent_id, available, frozen}. Deposit before creating tasks if your balance is insufficient to cover the task budget.",
    parameters: { type: "object", properties: { agent_id: { type: "string", description: "Agent ID to deposit funds for" }, amount: { type: "number", description: "Amount to deposit (must be > 0)" } }, required: ["agent_id", "amount"] },
    async execute(_id: string, params: any) {
      return ok(await net.deposit(params.agent_id, params.amount));
    },
  });

  // ═══════════════════════════════════════════════════════════════════════════
  // Events (1)
  // ═══════════════════════════════════════════════════════════════════════════

  // #32 eacn3_get_events
  api.registerTool({
    name: "eacn3_get_events",
    description: "Drain the in-memory event buffer, returning all pending events and clearing them. Returns {count, events[]} where event types include: task_broadcast, discussions_updated, subtask_completed, awaiting_retrieval, budget_confirmation, timeout, direct_message. Call periodically in your main loop. Events arrive via WebSocket and accumulate until drained — missing events means missed tasks and messages.",
    parameters: { type: "object", properties: {} },
    async execute() {
      const events = state.drainEvents();
      return ok({ count: events.length, events });
    },
  });
  },
});
