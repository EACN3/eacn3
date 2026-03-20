/**
 * HTTP client for EACN network endpoints (28 APIs).
 *
 * Each method maps 1:1 to a network-api.md endpoint.
 * server_id is injected from local state — callers don't need to pass it.
 */

import {
  type ServerCard,
  type AgentCard,
  type Task,
  type Bid,
  type ReputationScore,
  type RegisterServerResponse,
  type RegisterAgentResponse,
  type BidResponse,
  type DiscoverResponse,
  type TaskResultsResponse,
} from "./models.js";
import { getState, getServerId } from "./state.js";

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

function baseUrl(): string {
  return getState().network_endpoint;
}

function serverId(): string {
  const id = getServerId();
  if (!id) throw new Error("Not connected. Call eacn_connect first.");
  return id;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  query?: Record<string, string>,
): Promise<T> {
  let url = `${baseUrl()}${path}`;
  if (query) {
    const params = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== undefined && v !== ""),
    );
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  // Inject server_id header for authenticated requests
  const sid = getServerId();
  if (sid) headers["x-server-id"] = sid;

  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${method} ${path} → ${res.status}: ${text}`);
  }

  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Discovery — Server (4)
// ---------------------------------------------------------------------------

export async function registerServer(
  version: string,
  endpoint: string,
  owner: string,
): Promise<RegisterServerResponse> {
  return request<RegisterServerResponse>("POST", "/api/discovery/servers", {
    version,
    endpoint,
    owner,
  });
}

export async function getServer(sid: string): Promise<ServerCard> {
  return request<ServerCard>("GET", `/api/discovery/servers/${sid}`);
}

export async function heartbeat(): Promise<{ ok: boolean; message: string }> {
  return request("POST", `/api/discovery/servers/${serverId()}/heartbeat`);
}

export async function unregisterServer(): Promise<{
  ok: boolean;
  message: string;
}> {
  return request("DELETE", `/api/discovery/servers/${serverId()}`);
}

// ---------------------------------------------------------------------------
// Discovery — Agent (6)
// ---------------------------------------------------------------------------

export async function registerAgent(
  agent: Omit<AgentCard, "network_id">,
): Promise<RegisterAgentResponse> {
  return request<RegisterAgentResponse>(
    "POST",
    "/api/discovery/agents",
    agent,
  );
}

export async function getAgentInfo(agentId: string): Promise<AgentCard> {
  return request<AgentCard>("GET", `/api/discovery/agents/${agentId}`);
}

export async function updateAgent(
  agentId: string,
  updates: Partial<Pick<AgentCard, "name" | "domains" | "skills" | "url" | "description">>,
): Promise<{ ok: boolean; message: string }> {
  return request("PUT", `/api/discovery/agents/${agentId}`, updates);
}

export async function unregisterAgent(
  agentId: string,
): Promise<{ ok: boolean; message: string }> {
  return request("DELETE", `/api/discovery/agents/${agentId}`);
}

export async function discoverAgents(
  domain: string,
  requesterId?: string,
): Promise<DiscoverResponse> {
  const query: Record<string, string> = { domain };
  if (requesterId) query.requester_id = requesterId;
  return request<DiscoverResponse>("GET", "/api/discovery/query", undefined, query);
}

export async function listAgentsRemote(opts: {
  domain?: string;
  server_id?: string;
  limit?: number;
  offset?: number;
}): Promise<AgentCard[]> {
  const query: Record<string, string> = {};
  if (opts.domain) query.domain = opts.domain;
  if (opts.server_id) query.server_id = opts.server_id;
  if (opts.limit !== undefined) query.limit = String(opts.limit);
  if (opts.offset !== undefined) query.offset = String(opts.offset);
  return request<AgentCard[]>("GET", "/api/discovery/agents", undefined, query);
}

// ---------------------------------------------------------------------------
// Tasks — Query (5)
// ---------------------------------------------------------------------------

export async function createTask(task: {
  task_id: string;
  initiator_id: string;
  content: { description: string; expected_output?: string };
  domains?: string[];
  budget: number;
  deadline?: string;
  max_concurrent_bidders?: number;
  max_depth?: number;
}): Promise<Task> {
  return request<Task>("POST", "/api/tasks", task);
}

export async function getOpenTasks(opts?: {
  domains?: string;
  limit?: number;
  offset?: number;
}): Promise<Task[]> {
  const query: Record<string, string> = {};
  if (opts?.domains) query.domains = opts.domains;
  if (opts?.limit !== undefined) query.limit = String(opts.limit);
  if (opts?.offset !== undefined) query.offset = String(opts.offset);
  return request<Task[]>("GET", "/api/tasks/open", undefined, query);
}

export async function getTask(taskId: string): Promise<Task> {
  return request<Task>("GET", `/api/tasks/${taskId}`);
}

export async function getTaskStatus(
  taskId: string,
  agentId: string,
): Promise<Task> {
  return request<Task>("GET", `/api/tasks/${taskId}/status`, undefined, {
    agent_id: agentId,
  });
}

export async function listTasks(opts?: {
  status?: string;
  initiator_id?: string;
  limit?: number;
  offset?: number;
}): Promise<Task[]> {
  const query: Record<string, string> = {};
  if (opts?.status) query.status = opts.status;
  if (opts?.initiator_id) query.initiator_id = opts.initiator_id;
  if (opts?.limit !== undefined) query.limit = String(opts.limit);
  if (opts?.offset !== undefined) query.offset = String(opts.offset);
  return request<Task[]>("GET", "/api/tasks", undefined, query);
}

// ---------------------------------------------------------------------------
// Tasks — Initiator (7)
// ---------------------------------------------------------------------------

export async function getTaskResults(
  taskId: string,
  initiatorId: string,
): Promise<TaskResultsResponse> {
  return request<TaskResultsResponse>(
    "GET",
    `/api/tasks/${taskId}/results`,
    undefined,
    { initiator_id: initiatorId },
  );
}

export async function selectResult(
  taskId: string,
  initiatorId: string,
  agentId: string,
): Promise<{ ok: boolean; message: string }> {
  return request("POST", `/api/tasks/${taskId}/select`, {
    initiator_id: initiatorId,
    agent_id: agentId,
  });
}

export async function closeTask(
  taskId: string,
  initiatorId: string,
): Promise<Task> {
  return request<Task>("POST", `/api/tasks/${taskId}/close`, {
    initiator_id: initiatorId,
  });
}

export async function updateDeadline(
  taskId: string,
  initiatorId: string,
  deadline: string,
): Promise<Task> {
  return request<Task>("PUT", `/api/tasks/${taskId}/deadline`, {
    initiator_id: initiatorId,
    deadline,
  });
}

export async function updateDiscussions(
  taskId: string,
  initiatorId: string,
  message: string,
): Promise<Task> {
  return request<Task>("POST", `/api/tasks/${taskId}/discussions`, {
    initiator_id: initiatorId,
    message,
  });
}

export async function confirmBudget(
  taskId: string,
  initiatorId: string,
  approved: boolean,
  newBudget?: number,
): Promise<{ ok: boolean; message: string }> {
  const body: Record<string, unknown> = {
    initiator_id: initiatorId,
    approved,
  };
  if (newBudget !== undefined) body.new_budget = newBudget;
  return request("POST", `/api/tasks/${taskId}/confirm-budget`, body);
}

// ---------------------------------------------------------------------------
// Tasks — Executor (4)
// ---------------------------------------------------------------------------

export async function submitBid(
  taskId: string,
  agentId: string,
  confidence: number,
  price: number,
): Promise<BidResponse> {
  return request<BidResponse>("POST", `/api/tasks/${taskId}/bid`, {
    agent_id: agentId,
    confidence,
    price,
    server_id: serverId(),
  });
}

export async function submitResult(
  taskId: string,
  agentId: string,
  content: Record<string, unknown>,
): Promise<{ ok: boolean; message: string }> {
  return request("POST", `/api/tasks/${taskId}/result`, {
    agent_id: agentId,
    content,
  });
}

export async function rejectTask(
  taskId: string,
  agentId: string,
  reason?: string,
): Promise<{ ok: boolean; message: string }> {
  const body: Record<string, unknown> = { agent_id: agentId };
  if (reason) body.reason = reason;
  return request("POST", `/api/tasks/${taskId}/reject`, body);
}

export async function createSubtask(
  parentTaskId: string,
  initiatorId: string,
  content: { description: string },
  domains: string[],
  budget: number,
  deadline?: string,
): Promise<Task> {
  const body: Record<string, unknown> = {
    initiator_id: initiatorId,
    content,
    domains,
    budget,
  };
  if (deadline) body.deadline = deadline;
  return request<Task>("POST", `/api/tasks/${parentTaskId}/subtask`, body);
}

// ---------------------------------------------------------------------------
// Reputation (2)
// ---------------------------------------------------------------------------

export async function reportEvent(
  agentId: string,
  eventType: string,
): Promise<ReputationScore> {
  return request<ReputationScore>("POST", "/api/reputation/events", {
    agent_id: agentId,
    event_type: eventType,
    server_id: serverId(),
  });
}

export async function getReputation(
  agentId: string,
): Promise<ReputationScore> {
  return request<ReputationScore>(
    "GET",
    `/api/reputation/${agentId}`,
  );
}
