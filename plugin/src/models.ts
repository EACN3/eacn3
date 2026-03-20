/**
 * EACN data models — TypeScript interfaces matching network-api.md structures.
 */

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

export interface ServerCard {
  server_id: string;
  version: string;
  endpoint: string;
  owner: string;
  status: "online" | "offline";
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

export interface AgentSkill {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
}

export interface AgentCard {
  agent_id: string;
  name: string;
  agent_type: "executor" | "planner";
  domains: string[];
  skills: AgentSkill[];
  url: string;
  server_id: string;
  network_id: string;
  description: string;
}

// ---------------------------------------------------------------------------
// Task
// ---------------------------------------------------------------------------

export interface ExpectedOutput {
  type: string;
  description: string;
}

export interface TaskContent {
  description: string;
  expected_output?: ExpectedOutput | null;
  attachments?: Array<{ type: string; content: string }>;
  discussions?: Array<{
    initiator_id: string;
    messages: Array<{ role: string; message: string }>;
  }>;
}

export interface HumanContact {
  allowed: boolean;
  contact_id?: string;
  timeout_s?: number;
}

export type TaskStatus =
  | "unclaimed"
  | "bidding"
  | "awaiting_retrieval"
  | "completed"
  | "no_one";

export type TaskType = "normal" | "adjudication";

export interface Task {
  id: string;
  status: TaskStatus;
  type: TaskType;
  initiator_id: string;
  server_id?: string;
  domains: string[];
  budget: number;
  remaining_budget: number;
  deadline: string;
  depth: number;
  parent_id: string | null;
  child_ids: string[];
  content: TaskContent;
  bids: Bid[];
  results: Result[];
  max_concurrent_bidders: number;
  budget_locked: boolean;
  human_contact?: HumanContact;
  created_at?: string;
}

// ---------------------------------------------------------------------------
// Bid
// ---------------------------------------------------------------------------

export type BidStatus =
  | "waiting_execution"
  | "executing"
  | "waiting_subtasks"
  | "submitted"
  | "rejected"
  | "timeout"
  | "declined";

export interface Bid {
  id: string;
  task_id: string;
  agent_id: string;
  server_id: string;
  confidence: number;
  price: number;
  status: BidStatus;
  started_at: string;
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------

export interface Result {
  id: string;
  task_id: string;
  submitter_id: string;
  content: Record<string, unknown>;
  selected: boolean;
  adjudications: unknown[];
  submitted_at: string;
}

// ---------------------------------------------------------------------------
// WebSocket Push Events
// ---------------------------------------------------------------------------

export type PushEventType =
  | "task_broadcast"
  | "discussions_updated"
  | "subtask_completed"
  | "awaiting_retrieval"
  | "budget_confirmation"
  | "timeout";

export interface PushEvent {
  type: PushEventType;
  task_id: string;
  payload: Record<string, unknown>;
  received_at: number; // timestamp ms, added by ws-manager
}

// ---------------------------------------------------------------------------
// Reputation
// ---------------------------------------------------------------------------

export type ReputationEventType =
  | "task_completed"
  | "task_rejected"
  | "task_timeout"
  | "bid_declined";

export interface ReputationScore {
  agent_id: string;
  score: number;
}

// ---------------------------------------------------------------------------
// Network API response types
// ---------------------------------------------------------------------------

export interface RegisterServerResponse {
  server_id: string;
  status: string;
}

export interface RegisterAgentResponse {
  agent_id: string;
  seeds: string[];
}

export interface BidResponse {
  status: "accepted" | "rejected" | "waiting" | "pending_confirmation";
  task_id: string;
  agent_id: string;
}

export interface DiscoverResponse {
  domain: string;
  agent_ids: string[];
}

export interface TaskResultsResponse {
  results: Result[];
  adjudications: unknown[];
}

// ---------------------------------------------------------------------------
// Local State
// ---------------------------------------------------------------------------

export interface LocalTaskInfo {
  task_id: string;
  role: "initiator" | "executor";
  status: TaskStatus;
  domains: string[];
  description_summary: string;
  created_at: string;
}

export interface EacnState {
  server_card: ServerCard | null;
  network_endpoint: string;
  agents: Record<string, AgentCard>;
  local_tasks: Record<string, LocalTaskInfo>;
  reputation_cache: Record<string, number>;
  pending_events: PushEvent[];
}

export function createDefaultState(networkEndpoint?: string): EacnState {
  return {
    server_card: null,
    network_endpoint: networkEndpoint ?? "https://network.eacn.dev",
    agents: {},
    local_tasks: {},
    reputation_cache: {},
    pending_events: [],
  };
}
