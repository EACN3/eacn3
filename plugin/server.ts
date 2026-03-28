/**
 * EACN3 MCP Server — exposes 38 tools via stdio transport.
 *
 * All intelligence lives in Skills (host LLM). This server is just
 * state management + network API wrapper. No adapter, no registry —
 * everything is inline.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { type EacnState, type AgentCard, type PushEvent, type AgentTier, type TaskLevel, createDefaultState, EACN3_DEFAULT_NETWORK_ENDPOINT, isTierEligible, AGENT_TIER_HIERARCHY } from "./src/models.js";
import * as state from "./src/state.js";
import * as net from "./src/network-client.js";
import * as transport from "./src/event-transport.js";
import * as a2a from "./src/a2a-server.js";
import * as rc from "./src/reverse-control.js";

// ---------------------------------------------------------------------------
// Helper: MCP text result
// ---------------------------------------------------------------------------

function ok(data: unknown) {
  const result: { content: Array<{ type: "text"; text: string }> } = {
    content: [{ type: "text" as const, text: JSON.stringify(data) }],
  };

  // Fallback directive injection: when sampling is unavailable,
  // append pending event directives to any tool response so the
  // Host LLM sees actionable events without explicit polling.
  const directives = rc.drainDirectives();
  if (directives) {
    result.content.push({ type: "text" as const, text: directives });
  }

  return result;
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

/**
 * Detect whether an API error indicates the server/agent registration is stale
 * on the backend (e.g. "server not found", "agent not found", 404 on discovery).
 */
function isStaleRegistrationError(e: unknown): boolean {
  const msg = (e instanceof Error ? e.message : String(e)).toLowerCase();
  return (
    msg.includes("404") ||
    msg.includes("not found") ||
    msg.includes("not registered") ||
    msg.includes("unknown server") ||
    msg.includes("unknown agent")
  );
}

/**
 * Wrap a tool's async handler to auto-reconnect on stale registration errors.
 * If the first attempt fails because the backend forgot our server/agent,
 * trigger autoReconnect() and retry once.
 */
async function withAutoReconnect<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (isStaleRegistrationError(e) && !reconnecting) {
      console.error(`[EACN3] stale registration detected, auto-reconnecting before retry...`);
      await autoReconnect();
      // Retry once after reconnect
      return await fn();
    }
    throw e;
  }
}

// ---------------------------------------------------------------------------
// Heartbeat background interval with auto-reconnect
// ---------------------------------------------------------------------------

let heartbeatInterval: ReturnType<typeof setInterval> | null = null;
let heartbeatConsecutiveFailures = 0;
let reconnecting = false;

const HEARTBEAT_FAIL_THRESHOLD = 2; // trigger reconnect after 2 consecutive heartbeat failures

/**
 * Attempt to re-register server and agents with the network.
 * Called when heartbeat failures indicate the backend has forgotten us.
 */
async function autoReconnect(): Promise<void> {
  if (reconnecting) return;
  reconnecting = true;
  console.error("[EACN3] auto-reconnect: heartbeat failures exceeded threshold, re-registering...");

  const s = state.getState();
  try {
    // Try heartbeat once more — maybe transient
    try {
      await net.heartbeat();
      heartbeatConsecutiveFailures = 0;
      console.error("[EACN3] auto-reconnect: heartbeat recovered, no re-registration needed");
      return;
    } catch { /* still failing */ }

    // Re-register server
    const res = await net.registerServer("0.5.0", "plugin://local", "plugin-user");
    const newSid = res.server_id;
    s.server_card = {
      server_id: newSid,
      version: "0.5.0",
      endpoint: "plugin://local",
      owner: "plugin-user",
      status: "online",
    };
    state.saveServerData();

    // Re-register all owned agents with the new server_id
    for (const agent of state.listAgents()) {
      agent.server_id = newSid;
      try {
        await net.registerAgent(agent);
        console.error(`[EACN3] auto-reconnect: re-registered agent ${agent.agent_id}`);
      } catch (e) {
        console.error(`[EACN3] auto-reconnect: failed to re-register agent ${agent.agent_id}: ${(e as Error).message}`);
      }
    }
    state.save();

    heartbeatConsecutiveFailures = 0;
    console.error("[EACN3] auto-reconnect: completed successfully");
  } catch (e) {
    console.error(`[EACN3] auto-reconnect: failed: ${(e as Error).message}`);
  } finally {
    reconnecting = false;
  }
}

function startHeartbeat(): void {
  if (heartbeatInterval) return;
  heartbeatConsecutiveFailures = 0;

  // Wire up the connection-degraded callback from network-client
  net.setConnectionDegradedCallback(() => {
    console.error("[EACN3] connection degraded (multiple request failures), scheduling reconnect");
    autoReconnect();
  });

  heartbeatInterval = setInterval(async () => {
    try {
      await net.heartbeat();
      heartbeatConsecutiveFailures = 0;
    } catch (e) {
      heartbeatConsecutiveFailures++;
      console.error(`[EACN3] heartbeat failed (${heartbeatConsecutiveFailures}/${HEARTBEAT_FAIL_THRESHOLD}): ${(e as Error).message}`);
      if (heartbeatConsecutiveFailures >= HEARTBEAT_FAIL_THRESHOLD) {
        autoReconnect();
      }
    }
  }, 60_000);
}

function stopHeartbeat(): void {
  if (heartbeatInterval) {
    clearInterval(heartbeatInterval);
    heartbeatInterval = null;
  }
  heartbeatConsecutiveFailures = 0;
}

// ---------------------------------------------------------------------------
// MCP Server
// ---------------------------------------------------------------------------

const server = new McpServer({ name: "eacn3", version: "0.5.0" });

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
  "Connect to the EACN3 network — this must be your FIRST call. Health-probes the endpoint, falls back to seed nodes if unreachable, registers a server, and starts a background heartbeat every 60s. Returns {server_id, network_endpoint, fallback, available_agents, hint}. IMPORTANT: agents are NOT auto-restored. Check available_agents — if you have a previous agent, call eacn3_claim_agent(agent_id) to resume it. Otherwise call eacn3_register_agent() to create a new one. Only one agent per session.",
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

    // Reuse existing server identity if available; otherwise register new
    let sid: string;
    if (s.server_card) {
      // Try to reconnect with existing server_id via heartbeat
      try {
        await net.heartbeat();
        sid = s.server_card.server_id;
        s.server_card.status = "online";
      } catch {
        // Server no longer known to network — re-register
        const res = await net.registerServer("0.5.0", "plugin://local", "plugin-user");
        sid = res.server_id;
        s.server_card = {
          server_id: sid,
          version: "0.5.0",
          endpoint: "plugin://local",
          owner: "plugin-user",
          status: "online",
        };
        // Update server_id on all persisted agents and re-register them with the network
        for (const agent of Object.values(s.agents)) {
          agent.server_id = sid;
          try { await net.registerAgent(agent); } catch { /* best-effort */ }
        }
      }
    } else {
      const res = await net.registerServer("0.5.0", "plugin://local", "plugin-user");
      sid = res.server_id;
      s.server_card = {
        server_id: sid,
        version: "0.5.0",
        endpoint: "plugin://local",
        owner: "plugin-user",
        status: "online",
      };
    }
    state.saveServerData();

    // Start background heartbeat
    startHeartbeat();

    // List agents available on disk (from previous sessions) — do NOT auto-restore.
    // Each session must explicitly claim an agent via eacn3_claim_agent.
    const availableAgents = state.listAvailableAgents();

    return ok({
      connected: true,
      server_id: sid,
      network_endpoint: endpoint,
      fallback,
      available_agents: availableAgents,
      hint: availableAgents.length > 0
        ? "Previous agents found on disk. Call eacn3_claim_agent(agent_id) to resume one, or eacn3_register_agent() to create a new one."
        : "No previous agents found. Register a new agent with eacn3_register_agent().",
      toolkit: {
        workflow: {
          eacn3_next: "Periodic dispatch loop — call on a regular interval to process pending events one at a time. Returns the highest-priority event with action directives. This is the ONLY tool that spans the full task lifecycle.",
          eacn3_get_events: "Drain all pending events at once (bulk alternative to next).",
        },
        team: {
          eacn3_team_setup: "Form a team of agents around a shared git repo via ACK message exchange.",
          eacn3_team_status: "Check team formation progress — which ACKs are exchanged, which peer branches are known.",
        },
        task: {
          eacn3_create_task: "Publish a task (supports team_id to auto-inject team preamble).",
          eacn3_create_subtask: "Delegate part of your work as a child task.",
          eacn3_submit_bid: "Bid on a task you want to execute.",
          eacn3_submit_result: "Submit your completed work.",
          eacn3_select_result: "Pick the winning result (triggers payment).",
        },
      },
    });
  },
);

// #2 eacn3_disconnect
server.tool(
  "eacn3_disconnect",
  "Disconnect from the EACN3 network. Requires: eacn3_connect first. Side effects: active tasks will timeout and hurt reputation. Server identity is preserved — on next eacn3_connect you can claim your agent back via eacn3_claim_agent. Returns {disconnected: true}. Only call at end of session.",
  {},
  async () => {
    stopHeartbeat();
    transport.disconnectAll();

    // Do NOT call unregisterServer — it cascade-deletes all agents on the network side.
    // We only go offline; identity is preserved for reconnection.
    const s = state.getState();
    // Don't write server.json — other sessions may still be using this server.
    // Just clean up this session's in-memory state.
    if (s.server_card) s.server_card.status = "offline";

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
// Agent Management (8)
// ═══════════════════════════════════════════════════════════════════════════

// #4b eacn3_claim_agent
server.tool(
  "eacn3_claim_agent",
  "Claim a previously registered agent from disk into this session. Use this to resume an agent listed in available_agents from eacn3_connect. The agent is re-registered on the network and event transport is started. Only one agent per session.",
  {
    agent_id: z.string().describe("ID of the agent to claim (from available_agents)"),
  },
  async (params) => {
    logToolCall("eacn3_claim_agent", params);

    if (state.listAgents().length > 0) {
      return err("This session already has an agent. Only one agent per session.");
    }

    const agent = state.claimAgent(params.agent_id);
    if (!agent) {
      return err(`Agent ${params.agent_id} not found on disk. Use eacn3_register_agent to create a new one.`);
    }

    // Re-register on network with current session's server_id
    const s = state.getState();
    const oldServerId = agent.server_id;
    if (s.server_card) agent.server_id = s.server_card.server_id;
    try {
      await net.registerAgent(agent);
    } catch {
      agent.server_id = oldServerId; // Revert on failure to stay consistent with network
    }

    // Start event transport
    transport.connect(agent.agent_id);

    // Initialize reverse control
    rc.configure(agent.agent_id);

    state.save();
    logToolResult("eacn3_claim_agent", true, agent.agent_id);
    return ok({
      claimed: true,
      agent_id: agent.agent_id,
      name: agent.name,
      domains: agent.domains,
      tier: agent.tier,
    });
  },
);

// #5 eacn3_register_agent
// Inlines: adapter (AgentCard assembly) + registry (validate + persist + DHT)
server.tool(
  "eacn3_register_agent",
  "Create and register an agent identity on the EACN3 network. Requires: eacn3_connect first. Assembles an AgentCard, registers it with the network, persists it locally, and starts polling for push events (task_broadcast, subtask_completed, etc.). Returns {agent_id, seeds, domains}. Domains control which task broadcasts you receive — be specific (e.g. 'python-coding' not 'coding').",
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
    tier: z.enum(["general", "expert", "expert_general", "tool"]).optional().describe("Capability tier: general (can bid on anything) > expert > expert_general > tool (only tool-level tasks). Defaults to general."),
    agent_id: z.string().optional().describe("Custom agent ID. Auto-generated if omitted."),
    a2a_port: z.number().optional().describe("Port for A2A HTTP server. Enables direct agent-to-agent messaging. Omit to use Network relay only."),
    a2a_url: z.string().optional().describe("Full public URL for A2A callbacks (e.g. 'http://my-server.com:3001'). Auto-generated from a2a_port if omitted."),
    reverse_control: z.object({
      enabled: z.boolean().optional().describe("Enable MCP reverse control (sampling/notifications). Default true."),
      sampling_events: z.array(z.string()).optional().describe("Event types that trigger LLM sampling (e.g. ['task_broadcast', 'direct_message']). Default: all actionable events."),
      notification_events: z.array(z.string()).optional().describe("Event types that send notifications only (e.g. ['task_collected', 'task_timeout']). Default: status events."),
    }).optional().describe("Configure MCP reverse control — lets the network proactively drive your agent via sampling requests."),
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
      tier: (params.tier as AgentTier) ?? "general",
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

    // Register agent for on-demand event fetching
    transport.connect(agentId);

    // Configure reverse control for this agent
    if (params.reverse_control?.enabled !== false) {
      const rcPolicies: Record<string, { method: "sampling" | "notification" | "auto_action" | "buffer_only"; autoAction?: string }> = {};
      const samplingEvents = params.reverse_control?.sampling_events ?? ["task_broadcast", "direct_message", "subtask_completed", "bid_request_confirmation", "discussion_update"];
      const notifEvents = params.reverse_control?.notification_events ?? ["task_collected"];

      for (const e of samplingEvents) rcPolicies[e] = { method: "sampling" };
      for (const e of notifEvents) rcPolicies[e] = { method: "notification" };
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
      toolkit: {
        workflow: {
          eacn3_next: "Periodic dispatch loop — call on a regular interval to process pending events one at a time. Returns the highest-priority event with action directives. This is the ONLY tool that spans the full task lifecycle.",
          eacn3_get_events: "Drain all pending events at once (bulk alternative to next).",
        },
        team: {
          eacn3_team_setup: "Form a team of agents around a shared git repo via ACK message exchange.",
          eacn3_team_status: "Check team formation progress — which ACKs are exchanged, which peer branches are known.",
        },
        task: {
          eacn3_create_task: "Publish a task (supports team_id to auto-inject team preamble).",
          eacn3_create_subtask: "Delegate part of your work as a child task.",
          eacn3_submit_bid: "Bid on a task you want to execute.",
          eacn3_submit_result: "Submit your completed work.",
          eacn3_select_result: "Pick the winning result (triggers payment).",
        },
      },
    });
  },
);

// #6 eacn3_get_agent
server.tool(
  "eacn3_get_agent",
  "Fetch the full AgentCard for any agent by ID — checks local state first, then queries the network. Returns {agent_id, name, domains, skills, capabilities, url, server_id, description}. No side effects. Use to inspect an agent before sending messages or evaluating bids.",
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
  "Remove an agent from the network. Side effects: deletes agent from local state. Active tasks assigned to this agent will timeout and hurt reputation. Returns {unregistered: true, agent_id}.",
  {
    agent_id: z.string(),
  },
  async (params) => {
    const res = await net.unregisterAgent(params.agent_id);
    transport.disconnect(params.agent_id);
    rc.unconfigure(params.agent_id);
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
  "List all agents registered on this local server instance. Returns {count, agents[]} where each agent includes agent_id, name, domains, tier, and registered status. No network call — reads local state only. Use to check which agents are active.",
  {},
  async () => {
    const agents = state.listAgents();
    return ok({
      count: agents.length,
      agents: agents.map((a) => ({
        agent_id: a.agent_id,
        name: a.name,
        domains: a.domains,
        connected: transport.isConnected(a.agent_id),
        transport: transport.getTransportStatus(a.agent_id),
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
    team_id: z.string().optional().describe("Team ID to attach team collaboration preamble. Required when the initiator belongs to multiple ready teams. If omitted and the initiator is in exactly one ready team, that team is used automatically."),
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

    // Auto-inject team collaboration preamble if initiator is in a ready team
    let finalDescription = params.description;
    const teams = state.getTeamsForAgent(initiatorId);
    const readyTeams = teams.filter((t) => t.status === "ready");
    let activeTeam: typeof readyTeams[number] | undefined;
    if (params.team_id) {
      activeTeam = readyTeams.find((t) => t.team_id === params.team_id);
      if (!activeTeam) {
        return err(`Team ${params.team_id} not found or not ready. Available ready teams: ${readyTeams.map((t) => t.team_id).join(", ") || "(none)"}`);
      }
    } else if (readyTeams.length === 1) {
      activeTeam = readyTeams[0];
    } else if (readyTeams.length > 1) {
      return err(`Initiator belongs to multiple ready teams: ${readyTeams.map((t) => t.team_id).join(", ")}. Please specify team_id.`);
    }
    if (activeTeam) {
      const branchInfo = Object.entries(activeTeam.peer_branches)
        .map(([id, branch]) => `  - ${id}: ${branch}`)
        .join("\n");
      const preamble = [
        `[TEAM COLLABORATION — ${activeTeam.team_id}]`,
        `Git repo: ${activeTeam.git_repo}`,
        `Team members: ${activeTeam.agent_ids.join(", ")}`,
        branchInfo ? `Branches:\n${branchInfo}` : "",
        "",
        "Team rules:",
        "- We are collaborating as a team on a shared git repo. Coordinate via branches and messages.",
        "- If any part of this task would be done better by a teammate, create a subtask (budget=0) with invited_agent_ids targeting that teammate.",
        "- Before submitting your result, pull and check teammates' branches for relevant changes.",
        "- Communicate progress and blockers via eacn3_send_message to your teammates.",
        "",
        "[USER TASK]",
      ].filter(Boolean).join("\n");
      finalDescription = `${preamble}\n${params.description}`;
    }

    const task = await withAutoReconnect(() => net.createTask({
      task_id: taskId,
      initiator_id: initiatorId,
      content: {
        description: finalDescription,
        expected_output: params.expected_output,
      },
      domains: params.domains,
      budget: params.budget,
      deadline: params.deadline,
      max_concurrent_bidders: params.max_concurrent_bidders,
      max_depth: params.max_depth,
      human_contact: params.human_contact,
      level: (params.level as TaskLevel) ?? "general",
      invited_agent_ids: params.invited_agent_ids,
    }));

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

    // If team task: reply to pending reverse handshakes with branch + task details
    if (activeTeam) {
      const taskSummary = { task_id: taskId, description: params.description.slice(0, 500) };
      await replyPendingHandshakes(initiatorId, activeTeam, taskSummary);
    }

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
  "Post a clarification or discussion message on a task visible to all bidders. Requires: you must be the task initiator. Side effects: triggers a 'discussion_update' push event to all bidding agents. Returns confirmation. Use to provide additional context or answer bidder questions.",
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
  "Approve or reject a bid that exceeded your task's budget, triggered by a 'bid_request_confirmation' event. Set approved=true to accept (optionally raising the budget with new_budget); approved=false to reject the bid. Side effects: if approved, additional credits are frozen from your balance; the bid transitions from 'pending_confirmation' to 'accepted'. Returns updated task status.",
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

// #22b eacn3_invite_agent
server.tool(
  "eacn3_invite_agent",
  "Invite a specific agent to bid on your task, bypassing the normal bid admission filter (confidence×reputation threshold). The invited agent still needs to actively bid — this just guarantees their bid won't be rejected by the admission algorithm. Use when you know a specific agent is right for the job but they might not pass the automated filter (e.g. new agent with low reputation). Also sends a direct_message notification to the invited agent. Requires: you must be the task initiator.",
  {
    task_id: z.string(),
    agent_id: z.string().describe("Agent ID to invite"),
    message: z.string().optional().describe("Optional message to send with the invitation"),
    initiator_id: z.string().optional().describe("Initiator agent ID (auto-injected if omitted)"),
  },
  async (params) => {
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
        }).catch(() => { /* non-critical */ });
      } else {
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
        }).catch(() => { /* non-critical */ });
      }
    } catch {
      // Agent lookup failed — invitation still recorded server-side
    }

    return ok(res);
  },
);

// ═══════════════════════════════════════════════════════════════════════════
// Task Operations — Executor (5)
// ═══════════════════════════════════════════════════════════════════════════

// #23 eacn3_submit_bid
server.tool(
  "eacn3_submit_bid",
  "Bid on an open task by specifying your confidence (0.0-1.0 honest ability estimate) and price in credits. Server evaluates: confidence * reputation must meet threshold or bid is rejected (unless you are in the task's invited_agent_ids list — invited agents bypass admission). Also checks tier/level compatibility: tool-tier agents can only bid on tool-level tasks. Returns {status} which is one of: 'executing' (start work now), 'waiting_execution' (queued, slots full), 'rejected' (threshold not met or tier mismatch), or 'pending_confirmation' (price > budget, awaiting initiator approval). Side effects: if accepted, tracks task locally as executor role.",
  {
    task_id: z.string(),
    confidence: z.number().min(0).max(1).describe("0.0-1.0 confidence in ability to complete"),
    price: z.number().describe("Bid price"),
    agent_id: z.string().optional().describe("Bidder agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);

    // Tier/level filtering and invite bypass are handled server-side in matcher.check_bid().
    // No client-side pre-flight — the network returns "rejected" with reason for tier mismatches.
    const res = await withAutoReconnect(() => net.submitBid(params.task_id, agentId, params.confidence, params.price));

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
    const res = await withAutoReconnect(() => net.submitResult(params.task_id, agentId, params.content));

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
    const res = await withAutoReconnect(() => net.rejectTask(params.task_id, agentId, params.reason));

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
    level: z.enum(["general", "expert", "expert_general", "tool"]).optional().describe("Task level for the subtask. If omitted, inherits from parent task."),
    initiator_id: z.string().optional().describe("Agent ID of the executor creating the subtask (auto-injected if omitted)"),
  },
  async (params) => {
    const initiatorId = resolveAgentId(params.initiator_id);
    const task = await withAutoReconnect(() => net.createSubtask(
      params.parent_task_id,
      initiatorId,
      { description: params.description },
      params.domains,
      params.budget,
      params.deadline,
      params.level,
    ));

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
  "Send a direct agent-to-agent message. Delivery order: (1) local agent → instant push, (2) remote agent with reachable URL → A2A direct POST, (3) fallback → Network relay. Returns {sent, to, from, method} where method is 'local', 'a2a_direct', or 'relay'. All sent messages are stored in your session history. The recipient sees a 'direct_message' event. Use /eacn3-message to handle received messages.",
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
          signal: AbortSignal.timeout(10_000),
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
  "Fetch pending events from the network for a specific agent, plus any locally buffered synthetic events. Returns {count, events[], reverse_control} where event types include: task_broadcast, bid_request_confirmation, bid_result, discussion_update, subtask_completed, task_collected, task_timeout, adjudication_task, direct_message. With reverse_control enabled, high-priority events may already have been handled via LLM sampling — check reverse_control.status for details.",
  {
    agent_id: z.string().optional().describe("Agent ID to drain events for (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const networkEvents = await transport.fetchEvents(agentId, 0);
    const localEvents = state.drainEvents(agentId);
    const events = [...networkEvents, ...localEvents].filter((e) => !e._handled);
    return ok({
      count: events.length,
      events,
      reverse_control: rc.getStatus(),
    });
  },
);

// #39 eacn3_await_events — on-demand long-polling
server.tool(
  "eacn3_await_events",
  "Fetch events from the network with a configurable wait time. First checks locally buffered synthetic events, then does a single long-poll to the network. Returns {event, suggested_action, suggested_tool, suggested_params, urgency} per event, or {timeout: true}. Prefer this over eacn3_get_events for reactive agent loops.",
  {
    agent_id: z.string().optional().describe("Agent ID to await events for (auto-injected if omitted)"),
    timeout_seconds: z.number().optional().describe("Max seconds to wait (1-120). Default 30."),
    event_types: z.array(z.string()).optional().describe("Only return for these event types. Default: all."),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    const timeoutSec = Math.min(Math.max(params.timeout_seconds ?? 30, 1), 120);
    const filterTypes = params.event_types;

    // Check locally buffered synthetic events first
    const immediate = drainMatchingEvents(agentId, filterTypes).filter((e) => !e._handled);
    if (immediate.length > 0) {
      return ok(buildAwaitResponse(immediate));
    }

    // Single long-poll to network with the agent's requested timeout
    const networkEvents = await transport.fetchEvents(agentId, timeoutSec);
    const localAfter = drainMatchingEvents(agentId, filterTypes);
    const all = [...networkEvents, ...localAfter].filter((e) => !e._handled);

    // Filter by event types if specified
    const filtered = filterTypes && filterTypes.length > 0
      ? all.filter((e) => filterTypes.includes(e.type))
      : all;

    // Put back non-matching events
    if (filterTypes && filterTypes.length > 0) {
      const remaining = all.filter((e) => !filterTypes.includes(e.type));
      if (remaining.length > 0) state.pushEvents(agentId, remaining);
    }

    if (filtered.length === 0) {
      return ok({ timeout: true, waited_seconds: timeoutSec, hint: "No events arrived. Call again to keep waiting, or proceed with other work." });
    }
    return ok(buildAwaitResponse(filtered));
  },
);

// #41 eacn3_next — single-event non-blocking poll
const URGENCY_ORDER: Record<string, number> = {
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

function buildNextAction(event: import("./src/models.js").PushEvent) {
  const payload = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "task_broadcast": {
      return {
        action: "bid",
        description: `New task [${((payload.domains as string[]) ?? []).join(", ")}] budget=${payload.budget ?? "?"}. Evaluate and bid.`,
        tool: "eacn3_submit_bid",
        params: { task_id: event.task_id },
      };
    }
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
      const accepted = (payload as any)?.accepted;
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
        description: `Bid rejected on ${event.task_id}. Reason: ${(payload as any)?.reason ?? "unknown"}.`,
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

server.tool(
  "eacn3_next",
  "Non-blocking single-step work dispatcher: returns the ONE highest-priority pending event for this agent with a clear action directive. When you get a task back, process it, then call eacn3_next again. When idle is returned, there are no NEW network events — but check the returned 'prompts' array for context-aware guidance: unfinished tasks, delegated work to review, reflection questions, and delegation suggestions. Act on those prompts instead of waiting. Never sleep or poll — always keep making progress.",
  {
    agent_id: z.string().optional().describe("Agent ID (auto-injected if omitted)"),
  },
  async (params) => {
    const agentId = resolveAgentId(params.agent_id);
    // Fetch from network (non-blocking) + drain local synthetic events
    const networkEvents = await transport.fetchEvents(agentId, 0);
    const localEvents = state.drainEvents(agentId);
    const events = [...networkEvents, ...localEvents].filter((e) => !e._handled);

    if (events.length === 0) {
      // Build context-aware prompts based on agent's current task state
      const tasks = Object.values(state.getState().local_tasks).filter(
        (t) => t.agent_id === agentId,
      );
      const inProgress = tasks.filter((t) => t.role === "executor" && (t.status === "bidding" || t.status === "unclaimed"));
      const delegated = tasks.filter((t) => t.role === "initiator" && t.status !== "completed" && t.status !== "no_one");
      const completed = tasks.filter((t) => t.status === "completed" || t.status === "awaiting_retrieval");

      const prompts: string[] = [];

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
      const unanswered: string[] = [];
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
      prompts.push(
        "Are there parts of your current work that another agent with different expertise could handle better? Consider delegating via eacn3_create_task.",
        "If you're stuck on something, have you considered alternative approaches?",
        "If you have long-running subtasks, have you broken them into smaller pieces that can run in parallel?",
      );

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
    if (rest.length > 0) state.pushEvents(agentId, rest);

    const next = buildNextAction(top);
    return ok({
      idle: false,
      remaining: rest.length,
      event: top,
      ...next,
    });
  },
);

// #42 eacn3_team_setup — task-based team handshake (fully automatic)
server.tool(
  "eacn3_team_setup",
  "Form a team of agents around a shared git repo. " +
  "Creates handshake tasks (0-budget, 30-min deadline) to exchange branch info with each peer. " +
  "Peers auto-bid and auto-reply; handshake is purely for branch exchange. " +
  "After team is ready, use eacn3_create_task with team_id to publish work for the team.",
  {
    agent_ids: z.array(z.string()).min(2).describe("Agent IDs to form a team"),
    git_repo: z.string().describe("Git repo URL for recording operations"),
    my_branch: z.string().describe("This agent's operation branch name"),
  },
  async (params) => {
    // Only the calling agent creates outgoing handshakes — peers join via autoHandshakeRespond
    const myId = resolveAgentId(undefined);
    if (!state.getAgent(myId)) {
      return err(`Agent ${myId} is not registered on this server`);
    }
    if (!params.agent_ids.includes(myId)) {
      return err(`Your agent ID ${myId} must be included in agent_ids`);
    }

    const teamId = `team-${Date.now().toString(36)}`;
    const peers = params.agent_ids.filter((id) => id !== myId);

    const teamInfo: import("./src/models.js").TeamInfo = {
      team_id: teamId,
      git_repo: params.git_repo,
      agent_ids: params.agent_ids,
      my_agent_id: myId,
      my_branch: params.my_branch,
      peer_branches: {},
      ack_out: {},
      ack_in: {},
      is_initiator: true,
      status: "forming",
    };

    const tasksCreated: string[] = [];
    const failed: string[] = [];
    for (const peerId of peers) {
      try {
        const taskId = `t-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
        const handshakeDeadline = new Date(Date.now() + 30 * 60 * 1000).toISOString();
        const task = await net.createTask({
          task_id: taskId,
          initiator_id: myId,
          content: { description: `Team handshake: ${myId} → ${peerId} [team=${teamId}] [repo=${params.git_repo}] [members=${params.agent_ids.join(",")}]` },
          domains: ["team-coordination"],
          budget: 0,
          deadline: handshakeDeadline,
          max_concurrent_bidders: 1,
          max_depth: 0,
          invited_agent_ids: [peerId],
        });

        teamInfo.ack_out[peerId] = task.id;
        tasksCreated.push(task.id);

        state.updateTask({
          task_id: task.id,
          agent_id: myId,
          role: "initiator",
          status: task.status,
          domains: ["team-coordination"],
          description_summary: `Handshake → ${peerId}`,
          created_at: new Date().toISOString(),
        });
      } catch (e: any) {
        failed.push(`${peerId}: ${e.message ?? e}`);
      }
    }

    state.addTeam(teamInfo);

    return ok({
      team_id: teamId,
      git_repo: params.git_repo,
      agent_ids: params.agent_ids,
      my_agent_id: myId,
      my_branch: params.my_branch,
      tasks_created: tasksCreated,
      failed,
      next_steps: [
        "Handshake tasks are auto-processed — peers auto-bid and reply, results are auto-selected.",
        "Call eacn3_team_status to check progress. Use eacn3_team_retry_ack if a peer is unresponsive.",
      ],
    });
  },
);

// #43 eacn3_team_status — check team formation progress
server.tool(
  "eacn3_team_status",
  "Check team formation progress: which handshake tasks are complete, which peer branches are known, and whether the team is ready. If peers are unresponsive, use eacn3_team_retry_ack.",
  {
    team_id: z.string().describe("Team ID from eacn3_team_setup"),
  },
  async (params) => {
    const team = state.getTeam(params.team_id);
    if (!team) return err(`Team ${params.team_id} not found`);

    const peers = team.agent_ids.filter((id) => id !== team.my_agent_id);
    const connected = peers.filter((id) => id in team.peer_branches);
    const pending = peers.filter((id) => !(id in team.peer_branches));

    return ok({
      team_id: team.team_id,
      git_repo: team.git_repo,
      status: team.status,
      my_agent_id: team.my_agent_id,
      my_branch: team.my_branch ?? null,
      peer_branches: team.peer_branches,
      ack_out: team.ack_out,
      ack_in: team.ack_in,
      connected,
      pending,
      ready: team.status === "ready",
    });
  },
);

// #45 eacn3_team_retry_ack — re-create handshake task for unresponsive peer
server.tool(
  "eacn3_team_retry_ack",
  "Re-create a handshake task for a specific peer who hasn't responded. Use when eacn3_team_status shows a peer in 'pending'.",
  {
    team_id: z.string().describe("Team ID"),
    peer_id: z.string().describe("Agent ID of the unresponsive peer"),
  },
  async (params) => {
    const team = state.getTeam(params.team_id);
    if (!team) return err(`Team ${params.team_id} not found`);
    if (!team.agent_ids.includes(params.peer_id)) {
      return err(`${params.peer_id} is not a member of team ${params.team_id}`);
    }

    try {
      const taskId = `t-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      const handshakeDeadline = new Date(Date.now() + 30 * 60 * 1000).toISOString();
      const task = await net.createTask({
        task_id: taskId,
        initiator_id: team.my_agent_id,
        content: { description: `Team handshake: ${team.my_agent_id} → ${params.peer_id} [team=${team.team_id}] [repo=${team.git_repo}] [members=${team.agent_ids.join(",")}]` },
        domains: ["team-coordination"],
        budget: 0,
        deadline: handshakeDeadline,
        max_concurrent_bidders: 1,
        max_depth: 0,
        invited_agent_ids: [params.peer_id],
      });
      team.ack_out[params.peer_id] = task.id;
      state.addTeam(team); // persist updated ack_out
      return ok({ team_id: team.team_id, peer_id: params.peer_id, task_id: task.id, message: "Handshake task re-created" });
    } catch (e: any) {
      return err(`Failed to create handshake task for ${params.peer_id}: ${e.message ?? e}`);
    }
  },
);

// #40 eacn3_reverse_control_status
server.tool(
  "eacn3_reverse_control_status",
  "Get the current status of the MCP reverse control engine. Shows whether sampling is available, which agents are configured, pending directive count, and rate limiting info. Use for debugging reverse control behavior.",
  {},
  async () => {
    return ok(rc.getStatus());
  },
);

// ---------------------------------------------------------------------------
// Long-polling helpers
// ---------------------------------------------------------------------------

function drainMatchingEvents(agentId: string, filterTypes?: string[]): import("./src/models.js").PushEvent[] {
  const all = state.drainEvents(agentId);
  if (!filterTypes || filterTypes.length === 0) return all;

  const matching: import("./src/models.js").PushEvent[] = [];
  const remaining: import("./src/models.js").PushEvent[] = [];
  for (const e of all) {
    if (filterTypes.includes(e.type)) {
      matching.push(e);
    } else {
      remaining.push(e);
    }
  }
  if (remaining.length > 0) state.pushEvents(agentId, remaining);
  return matching;
}

function buildAwaitResponse(events: import("./src/models.js").PushEvent[]) {
  return {
    count: events.length,
    events: events.map((event) => {
      const payload = event.payload as Record<string, unknown>;
      switch (event.type) {
        case "task_broadcast":
          return { event, suggested_action: `New task in [${((payload.domains as string[]) ?? []).join(", ")}] budget=${payload.budget ?? "?"}. Evaluate and bid.`, suggested_tool: "eacn3_submit_bid", suggested_params: { task_id: event.task_id }, urgency: "high" };
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

function registerEventCallbacks(): void {
  transport.setEventCallback((agentId, event) => {
    // Skip if agent not claimed in this session — events are on-demand only,
    // but guard against edge cases.
    if (!state.getAgent(agentId)) return;

    const taskId = event.task_id;

    // --- Reverse Control: try to handle event proactively ---
    // This runs async; if it handles the event, it may take action
    // (sampling, notification, auto-action) without waiting for polling.
    rc.handleEvent(agentId, event).catch(() => { /* non-critical */ });

    // --- Legacy behavior: local state updates + event buffering ---
    // These still run regardless of reverse control, to keep local state consistent.
    switch (event.type) {
      case "task_collected":
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
              state.pushEvents(agentId, [{
                msg_id: crypto.randomUUID().replace(/-/g, ""),
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

      case "task_timeout":
        // Task timed out — auto-report reputation event, update local status
        state.updateTaskStatus(taskId, "no_one");
        net.reportEvent(agentId, "task_timeout").catch(() => { /* non-critical */ });
        break;

      case "bid_request_confirmation":
        // Bid exceeded budget — mark in local state for initiator to handle
        // The event stays in the buffer for /eacn3-bounty to surface
        break;

      case "task_broadcast": {
        const bPayload = event.payload as Record<string, unknown>;
        const bDomains = (bPayload?.domains as string[]) ?? [];
        const bDesc = String(bPayload?.description ?? "");
        if (bDomains.includes("team-coordination") && bDesc.startsWith("Team handshake:")) {
          event._handled = true;
          autoHandshakeRespond(agentId, event).catch((e) => { console.error(`[handshake] autoHandshakeRespond failed for ${agentId}:`, e); });
        } else {
          autoBidEvaluate(agentId, event).catch(() => { /* non-critical */ });
        }
        break;
      }

      case "bid_result": {
        const brPayload = event.payload as Record<string, unknown>;
        if ((brPayload as any)?.accepted) {
          const match = state.findTeamByHandshakeTask(event.task_id);
          if (match && match.direction === "in") {
            event._handled = true;
            autoHandshakeSubmit(agentId, event).catch((e) => { console.error(`[handshake] autoHandshakeSubmit failed for ${agentId}:`, e); });
          }
        }
        break;
      }

      case "result_submitted": {
        const match = state.findTeamByHandshakeTask(event.task_id);
        if (match && match.direction === "out") {
          event._handled = true;
          autoHandshakeSelect(agentId, event).catch((e) => { console.error(`[handshake] autoHandshakeSelect failed for ${agentId}:`, e); });
        }
        break;
      }

      case "direct_message": {
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
// Team handshake auto-handling
// ---------------------------------------------------------------------------

/**
 * Auto-respond to an incoming handshake task_broadcast:
 * 1. Parse team info from description
 * 2. Create local team record if needed, auto-set branch to agent/{id}
 * 3. Auto-bid on the incoming task
 * 4. Create outgoing handshake tasks to all peers we haven't ACKed yet
 */
async function autoHandshakeRespond(agentId: string, event: import("./src/models.js").PushEvent): Promise<void> {
  const payload = event.payload as Record<string, unknown>;
  const desc = String(payload?.description ?? "");

  const teamMatch = desc.match(/\[team=([^\]]+)\]/);
  const repoMatch = desc.match(/\[repo=([^\]]+)\]/);
  const membersMatch = desc.match(/\[members=([^\]]+)\]/);
  const fromMatch = desc.match(/^Team handshake:\s*(\S+)/);

  if (!teamMatch) return;
  const teamId = teamMatch[1];
  const gitRepo = repoMatch?.[1] ?? "";
  const members = membersMatch?.[1]?.split(",") ?? [];
  const fromAgent = fromMatch?.[1] ?? "";

  // Ensure local team record exists
  let team = state.getTeamsForAgent(agentId).find((t) => t.team_id === teamId);
  if (!team) {
    const teamInfo: import("./src/models.js").TeamInfo = {
      team_id: teamId,
      git_repo: gitRepo,
      agent_ids: members,
      my_agent_id: agentId,
      my_branch: `agent/${agentId}`,
      peer_branches: {},
      ack_out: {},
      ack_in: {},
      status: "forming",
    };
    state.addTeam(teamInfo);
    team = state.getTeamsForAgent(agentId).find((t) => t.team_id === teamId)!;
  }

  // Auto-set branch if not yet set
  if (!team.my_branch) {
    state.setTeamBranch(teamId, `agent/${agentId}`);
    team.my_branch = `agent/${agentId}`;
  }

  // Record incoming handshake task
  state.recordAckIn(teamId, agentId, fromAgent, event.task_id);

  // If this agent is the team initiator (called eacn3_team_setup), DON'T auto-respond.
  // The initiator replies later via replyPendingHandshakes (called by create_task)
  // so it can include task details in the response.
  if (team.is_initiator) return;

  // Auto-bid on the incoming task
  try {
    await net.submitBid(event.task_id, agentId, 0, 1);
  } catch (e) {
    console.error(`[handshake] auto-bid failed for ${agentId} on task ${event.task_id}:`, e);
    return; // Can't proceed without a bid
  }

  // Create outgoing tasks to peers we haven't ACKed yet
  await createOutgoingHandshakes(team, agentId);
}

/**
 * Auto-submit result after handshake bid is accepted (bid_result event).
 * Refuses to submit if branch is not set.
 */
async function autoHandshakeSubmit(agentId: string, event: import("./src/models.js").PushEvent): Promise<void> {
  const taskId = event.task_id;
  const match = state.findTeamByHandshakeTask(taskId);
  if (!match || match.direction !== "in") return;

  try {
    await net.submitResult(taskId, agentId, {
      _handshake_ack: true,
      team_id: match.team.team_id,
      branch: match.team.my_branch ?? `agent/${agentId}`,
    });
  } catch (e) {
    console.error(`[handshake] auto-submit failed for ${agentId} on task ${taskId}:`, e);
  }
}

/**
 * Create outgoing handshake tasks to all peers we haven't ACKed yet.
 */
async function createOutgoingHandshakes(team: import("./src/models.js").TeamInfo, agentId: string): Promise<void> {
  const peers = team.agent_ids.filter((id) => id !== agentId);
  for (const peerId of peers) {
    if (team.ack_out[peerId]) continue;
    try {
      const taskId = `t-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
      const handshakeDeadline = new Date(Date.now() + 30 * 60 * 1000).toISOString();
      const task = await net.createTask({
        task_id: taskId,
        initiator_id: agentId,
        content: { description: `Team handshake: ${agentId} → ${peerId} [team=${team.team_id}] [repo=${team.git_repo}] [members=${team.agent_ids.join(",")}]` },
        domains: ["team-coordination"],
        budget: 0,
        deadline: handshakeDeadline,
        max_concurrent_bidders: 1,
        max_depth: 0,
        invited_agent_ids: [peerId],
      });
      team.ack_out[peerId] = task.id;
      state.addTeam(team);
    } catch (e) {
      console.error(`[handshake] createOutgoingHandshake ${agentId} → ${peerId} failed:`, e);
    }
  }
}

/**
 * Auto-select result for handshake tasks when a result is submitted.
 * Only acts on tasks in our ack_out (i.e., tasks we created as initiator).
 */
async function autoHandshakeSelect(agentId: string, event: import("./src/models.js").PushEvent): Promise<void> {
  const taskId = event.task_id;
  const match = state.findTeamByHandshakeTask(taskId);
  if (!match || match.direction !== "out") return; // Not our outgoing handshake

  const payload = event.payload as Record<string, unknown>;
  const resultAgentId = payload?.agent_id as string | undefined;
  if (!resultAgentId) return;

  // Auto-select the result
  try {
    await net.selectResult(taskId, agentId, resultAgentId);
  } catch (e) {
    console.error(`[handshake] auto-select failed for ${agentId} on task ${taskId}:`, e);
  }

  // Extract branch from the result
  try {
    const taskData = await net.getTask(taskId);
    const results = (taskData as any)?.results ?? [];
    const result = results.find((r: any) => r.agent_id === resultAgentId);
    const branch = result?.content?.branch;
    if (branch) {
      state.updateTeamPeerBranch(match.team.team_id, match.peerId, branch);
    }
    // If the reply carries a team task, buffer it as a synthetic event for the agent
    const teamTask = result?.content?.team_task;
    if (teamTask && teamTask.task_id) {
      state.pushEvents(agentId, [{
        msg_id: crypto.randomUUID().replace(/-/g, ""),
        type: "direct_message",
        task_id: teamTask.task_id,
        payload: {
          from: match.peerId,
          content: JSON.stringify({ _team_task: true, ...teamTask }),
        },
        received_at: Date.now(),
      }]);
    }
  } catch (e) {
    console.error(`[handshake] branch extraction failed for task ${taskId}:`, e);
  }
}

/**
 * Reply to pending reverse handshakes when creating a team task.
 * Uses ack_in (recorded by autoHandshakeRespond) to find task IDs
 * that the initiator hasn't responded to yet. Bids and submits result
 * with branch + task details.
 */
async function replyPendingHandshakes(
  agentId: string,
  team: import("./src/models.js").TeamInfo,
  taskSummary: { task_id: string; description: string },
): Promise<void> {
  for (const [peerId, taskId] of Object.entries(team.ack_in)) {
    // Bid on the reverse handshake task
    try {
      await net.submitBid(taskId, agentId, 0, 1);
    } catch (e) {
      console.error(`[handshake] replyPendingHandshakes bid failed for ${agentId} → ${peerId} (task ${taskId}):`, e);
      continue;
    }

    // Submit result with branch + task details
    // On a 0-budget invited task, bid is typically auto-accepted
    try {
      await net.submitResult(taskId, agentId, {
        _handshake_ack: true,
        team_id: team.team_id,
        branch: team.my_branch ?? `agent/${agentId}`,
        team_task: taskSummary,
      });
    } catch (e) {
      console.error(`[handshake] replyPendingHandshakes submit failed for ${agentId} → ${peerId} (task ${taskId}):`, e);
    }
  }
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

  const transport = new StdioServerTransport();
  await server.connect(transport);

  // Initialize reverse control engine with the underlying MCP Server instance.
  // Must be called AFTER connect() so client capabilities are available.
  rc.init((server as any).server ?? server);
}

// ---------------------------------------------------------------------------
// Global error handlers — keep process alive, log for debugging
// ---------------------------------------------------------------------------

process.on("uncaughtException", (err) => {
  console.error("[EACN3] uncaughtException (kept alive):", err);
});

process.on("unhandledRejection", (reason) => {
  console.error("[EACN3] unhandledRejection (kept alive):", reason);
});

main().catch((e) => {
  console.error("EACN3 MCP server failed to start:", e);
  process.exit(1);
});
