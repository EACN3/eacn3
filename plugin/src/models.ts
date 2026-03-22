/**
 * EACN3 data models — TypeScript interfaces matching network-api.md structures.
 */

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

/** A server node on the EACN3 network. One per plugin session, created by eacn3_connect. */
export interface ServerCard {
  /** Unique identifier assigned by the network on connect. */
  server_id: string;
  /** Semantic version of the EACN3 server software. */
  version: string;
  /** Base URL other nodes use to reach this server. */
  endpoint: string;
  /** Identifier of the user/entity that owns this server instance. */
  owner: string;
  /** Current connectivity status; "offline" servers are unreachable but may still be registered. */
  status: "online" | "offline";
}

// ---------------------------------------------------------------------------
// Agent
// ---------------------------------------------------------------------------

/** A discrete capability an agent advertises (e.g. "code-review", "translate-ja"). */
export interface AgentSkill {
  /** Optional server-assigned identifier; absent until the skill is persisted. */
  id?: string;
  /** Human-readable skill name. */
  name: string;
  /** Brief explanation of what this skill does. */
  description: string;
  /** Free-form tags for discovery filtering (e.g. ["python", "async"]). */
  tags?: string[];
  /** Schema or hints describing the inputs the skill accepts; structure is skill-specific. */
  parameters?: Record<string, unknown>;
}

/**
 * Agent capability tier — determines which task levels the agent can bid on.
 *
 * - `"general"` — General-purpose agent, can bid on any task level.
 * - `"expert"` — Domain expert, can bid on expert, expert_general, and tool tasks.
 * - `"expert_general"` — Generalist within an expert domain, can bid on expert_general and tool tasks.
 * - `"tool"` — Single-purpose tool wrapper, can ONLY bid on tool-level tasks.
 *
 * Tier hierarchy (higher can accept lower):
 *   general > expert > expert_general > tool
 */
export type AgentTier = "general" | "expert" | "expert_general" | "tool";

/**
 * Ordered tier hierarchy for comparison. Lower index = higher tier.
 */
export const AGENT_TIER_HIERARCHY: AgentTier[] = ["general", "expert", "expert_general", "tool"];

/** Concurrency limits for an agent's task execution. */
export interface AgentCapabilities {
  /** Maximum number of tasks this agent can execute simultaneously. */
  max_concurrent_tasks: number;
  /** Whether the agent supports concurrent execution at all. If false, max_concurrent_tasks is effectively 1. */
  concurrent: boolean;
}

/** Full identity card for an agent on the network. Created by eacn3_register_agent. */
export interface AgentCard {
  /** Unique agent identifier, assigned by the network on registration. */
  agent_id: string;
  /** Human-readable display name for the agent. */
  name: string;
  /** Capability tier: general > expert > expert_general > tool. Determines which task levels the agent can bid on. Defaults to "general". */
  tier: AgentTier;
  /** Capability tags used for task routing (e.g. "translation", "python-coding"). Only matching broadcasts are received. */
  domains: string[];
  /** List of discrete skills this agent advertises. */
  skills: AgentSkill[];
  /** Optional concurrency configuration; absent means server defaults apply. */
  capabilities?: AgentCapabilities;
  /** Reachable endpoint URL for direct agent-to-agent communication. */
  url: string;
  /** The server hosting this agent. */
  server_id: string;
  /** The network this agent belongs to. */
  network_id: string;
  /** Free-text description of the agent's purpose and abilities. */
  description: string;
}

// ---------------------------------------------------------------------------
// Task
// ---------------------------------------------------------------------------

/** Describes what the initiator expects the executor to produce. */
export interface ExpectedOutput {
  /** MIME type or format hint (e.g. "application/json", "text/plain"). */
  type: string;
  /** Human-readable description of the desired output shape/content. */
  description: string;
}

/** The payload of a task: what needs to be done, supporting materials, and threaded discussions. */
export interface TaskContent {
  /** Full description of the work to be performed. */
  description: string;
  /** Optional specification of the desired result format; null when the initiator has no preference. */
  expected_output?: ExpectedOutput | null;
  /** Supporting files/data. Each entry has a MIME `type` and inline `content` (typically base64 for binary). */
  attachments?: Array<{ type: string; content: string }>;
  /** Threaded discussion channels. Populated when initiator/executors exchange clarifications via eacn3_update_discussions. */
  discussions?: Array<{
    initiator_id: string;
    messages: Array<{ role: string; message: string }>;
  }>;
}

/**
 * Permission toggle for executor to contact a human.
 * Agents cannot contact humans by default. The task initiator sets this
 * field at creation, authorizing the assigned executor to contact a
 * designated human when needed.
 * After timeout_s the executor should decide on its own.
 */
export interface HumanContact {
  /** Whether the executor is permitted to contact a human for guidance. */
  allowed: boolean;
  /** Identifier of the human to contact; only meaningful when allowed is true. */
  contact_id?: string;
  /** Seconds to wait for a human response before the executor should decide on its own. */
  timeout_s?: number;
}

/**
 * Task lifecycle states.
 * - `"unclaimed"` — Published but no bids received yet.
 * - `"bidding"` — At least one bid has arrived; still accepting more.
 * - `"awaiting_retrieval"` — An executor submitted results; waiting for the initiator to retrieve them.
 * - `"completed"` — Initiator retrieved results (first eacn3_get_task_results call transitions here).
 * - `"no_one"` — Deadline expired with no bids or results; terminal state.
 */
export type TaskStatus =
  | "unclaimed"
  | "bidding"
  | "awaiting_retrieval"
  | "completed"
  | "no_one";

/**
 * Task category.
 * - `"normal"` — Standard work task.
 * - `"adjudication"` — A meta-task to judge/evaluate results of another task.
 */
export type TaskType = "normal" | "adjudication";

/**
 * Task complexity level — matches against AgentTier for bid admission.
 *
 * - `"general"` — Open to all agent tiers.
 * - `"expert"` — Requires expert-tier or higher.
 * - `"expert_general"` — Requires expert_general-tier or higher.
 * - `"tool"` — Simple tool-level task, open to all tiers including tool-only agents.
 *
 * Tier filtering rule:
 *   An agent can bid if its tier index <= task level index in AGENT_TIER_HIERARCHY,
 *   EXCEPT tool-tier agents can ONLY bid on tool-level tasks.
 */
export type TaskLevel = "general" | "expert" | "expert_general" | "tool";

/** A unit of work on the EACN3 network. Created by eacn3_create_task or eacn3_create_subtask. */
export interface Task {
  /** Unique task identifier (e.g. "t-abc123"). */
  id: string;
  /** Current lifecycle state. See TaskStatus for the state machine. */
  status: TaskStatus;
  /** Whether this is a regular task or an adjudication (judging) task. */
  type: TaskType;
  /** Complexity level — determines which agent tiers can bid. Defaults to "general". */
  level: TaskLevel;
  /** Agent ID of the task creator. Budget is frozen from this agent's balance. */
  initiator_id: string;
  /** Server that hosts the initiator; may be absent for cross-network tasks. */
  server_id?: string;
  /** Capability tags for routing; only agents with matching domains receive the broadcast. */
  domains: string[];
  /** Total EACN credits allocated for this task, frozen from the initiator's balance on creation. */
  budget: number;
  /** Credits not yet consumed by subtask escrow; always <= budget. */
  remaining_budget: number;
  /** ISO 8601 deadline. Task moves to "no_one" if no result is submitted by this time. */
  deadline: string;
  /** Subtask nesting level: 0 for root tasks, increments per delegation. Max depth is 3. */
  depth: number;
  /** ID of the parent task if this is a subtask; null for root tasks. */
  parent_id: string | null;
  /** IDs of subtasks created by the executor of this task. */
  child_ids: string[];
  /** Task description, expected output, attachments, and discussion threads. */
  content: TaskContent;
  /** All bids submitted for this task. */
  bids: Bid[];
  /** All submitted results. Populated once executors call eacn3_submit_result. */
  results: Result[];
  /** How many agents can bid simultaneously. Excess bids enter "waiting_execution" queue. */
  max_concurrent_bidders: number;
  /** True while the budget is held in escrow; prevents the initiator from spending it elsewhere. */
  budget_locked: boolean;
  /** Permission for executors to contact a human; absent means human contact is not allowed. */
  human_contact?: HumanContact;
  /**
   * Agent IDs that bypass bid admission filtering (confidence×reputation threshold).
   * These agents are directly approved when they bid — the publisher's explicit choice.
   * Domain matching still applies for broadcast routing, but invited agents can bid
   * even if they don't match domains (they just won't receive the broadcast automatically).
   * Optional; defaults to [] for tasks created without invitations or legacy tasks from the network.
   */
  invited_agent_ids?: string[];
  /** ISO 8601 timestamp of task creation; set by the server. */
  created_at?: string;
}

// ---------------------------------------------------------------------------
// Bid
// ---------------------------------------------------------------------------

/**
 * Bid lifecycle states.
 * - `"waiting_execution"` — Bid accepted but all concurrent slots are full; queued.
 * - `"executing"` — Slot acquired; the agent is actively working on the task.
 * - `"waiting_subtasks"` — Agent created subtask(s) and is waiting for their completion.
 * - `"submitted"` — Agent called eacn3_submit_result; terminal success state.
 * - `"rejected"` — Bid failed admission (confidence * reputation < threshold); terminal.
 * - `"timeout"` — Task deadline passed before the agent submitted a result; terminal. Hurts reputation.
 * - `"declined"` — Agent voluntarily gave up via eacn3_reject_task; terminal. Hurts reputation.
 */
export type BidStatus =
  | "waiting_execution"
  | "executing"
  | "waiting_subtasks"
  | "submitted"
  | "rejected"
  | "timeout"
  | "declined";

/** An agent's offer to execute a task. Created by eacn3_submit_bid. */
export interface Bid {
  /** Unique bid identifier. */
  id: string;
  /** The task this bid is for. */
  task_id: string;
  /** The agent placing the bid. */
  agent_id: string;
  /** Server hosting the bidding agent. */
  server_id: string;
  /** Self-assessed ability to complete the task; 0.0-1.0. Used with reputation for admission: confidence * reputation >= threshold. */
  confidence: number;
  /** EACN credits the agent requests as payment. If price > task budget, triggers "pending_confirmation" from the initiator. */
  price: number;
  /** Current bid lifecycle state. See BidStatus. */
  status: BidStatus;
  /** ISO 8601 timestamp when the bid was placed. */
  started_at: string;
}

// ---------------------------------------------------------------------------
// Result
// ---------------------------------------------------------------------------

/** Work output submitted by an executor via eacn3_submit_result. */
export interface Result {
  /** Unique result identifier. */
  id: string;
  /** The task this result belongs to. */
  task_id: string;
  /** Agent ID of the executor who submitted this result. */
  submitter_id: string;
  /** Free-form JSON payload; should match TaskContent.expected_output if specified. */
  content: Record<string, unknown>;
  /** True if the initiator chose this result via eacn3_select_result; triggers credit transfer. */
  selected: boolean;
  /** Adjudication records from judging tasks, if any were created for this result. */
  adjudications: unknown[];
  /** ISO 8601 timestamp when the result was submitted. */
  submitted_at: string;
}

// ---------------------------------------------------------------------------
// WebSocket Push Events
// ---------------------------------------------------------------------------

/**
 * WebSocket push event types. Events buffer in memory; drain with eacn3_get_events().
 * - `"task_broadcast"` — New task matching your domains is available. Evaluate and bid.
 * - `"discussions_updated"` — Initiator added a clarification message to a task.
 * - `"subtask_completed"` — A subtask you created has finished; payload contains results.
 * - `"awaiting_retrieval"` — A task you published has results ready for retrieval.
 * - `"budget_confirmation"` — A bid on your task exceeded its budget; approve or reject via eacn3_confirm_budget.
 * - `"timeout"` — A task expired with no result. Reputation hit is automatic.
 * - `"direct_message"` — Another agent sent you a message; check payload.from and payload.content.
 */
export type PushEventType =
  | "task_broadcast"
  | "discussions_updated"
  | "subtask_completed"
  | "awaiting_retrieval"
  | "budget_confirmation"
  | "timeout"
  | "direct_message";

/** A single event received over the WebSocket connection. */
export interface PushEvent {
  /** Discriminator for the event; determines how to interpret payload. */
  type: PushEventType;
  /** The task this event relates to. */
  task_id: string;
  /** Event-specific data; structure varies by type (e.g. results for subtask_completed, from/content for direct_message). */
  payload: Record<string, unknown>;
  /** Unix timestamp in milliseconds when the event was received; added client-side by ws-manager. */
  received_at: number; // timestamp ms, added by ws-manager
}

// ---------------------------------------------------------------------------
// Reputation
// ---------------------------------------------------------------------------

/**
 * Types of reputation-affecting events. Usually auto-reported by submit_result/reject_task.
 * - `"task_completed"` — Agent finished work successfully; score increases.
 * - `"task_rejected"` — Agent gave up on a task via eacn3_reject_task; score decreases.
 * - `"task_timeout"` — Agent failed to submit before the deadline; score decreases.
 * - `"bid_declined"` — Agent's bid was declined; minor score decrease.
 */
export type ReputationEventType =
  | "task_completed"
  | "task_rejected"
  | "task_timeout"
  | "bid_declined";

/** An agent's current reputation score on the network. Returned by eacn3_get_reputation. */
export interface ReputationScore {
  /** The agent whose reputation this represents. */
  agent_id: string;
  /** Reputation score; 0.0-1.0, starts at 0.5 for new agents. Used in bid admission: confidence * score >= threshold. */
  score: number;
}

// ---------------------------------------------------------------------------
// Network API response types
// ---------------------------------------------------------------------------

/** Response from eacn3_connect when registering this server with the network. */
export interface RegisterServerResponse {
  /** Unique server ID assigned by the network. */
  server_id: string;
  /** Registration outcome; typically "ok" on success. */
  status: string;
}

/** Response from eacn3_register_agent. Agent is now discoverable and receives WebSocket events. */
export interface RegisterAgentResponse {
  /** Unique agent ID assigned by the network on registration. */
  agent_id: string;
  /** Seed node URLs for network discovery; can be passed to future eacn3_connect calls. */
  seeds: string[];
}

/** Response from eacn3_submit_bid. Check status to determine next action. */
export interface BidResponse {
  /** Bid outcome: "accepted" = executing, "rejected" = failed admission, "waiting" = queued, "pending_confirmation" = price exceeded budget and awaiting initiator approval. */
  status: "accepted" | "rejected" | "waiting" | "pending_confirmation";
  /** The task the bid was placed on. */
  task_id: string;
  /** The agent that placed the bid. */
  agent_id: string;
}

/** Response from eacn3_discover_agents. Lists agents matching a capability domain. */
export interface DiscoverResponse {
  /** The domain that was searched. */
  domain: string;
  /** IDs of agents advertising this domain; found via Gossip, DHT, or Bootstrap. */
  agent_ids: string[];
}

/** Response from eacn3_get_task_results. First call transitions task to "completed". */
export interface TaskResultsResponse {
  /** All results submitted by executors for this task. */
  results: Result[];
  /** Adjudication judgments, if any adjudication tasks were created for these results. */
  adjudications: unknown[];
}

/** Response from eacn3_get_balance. All values are in EACN credits. */
export interface BalanceResponse {
  /** The agent whose balance this represents. */
  agent_id: string;
  /** Spendable credits; can be used to create tasks or deposited further. */
  available: number;
  /** Credits locked in escrow for active tasks; released on completion or timeout. */
  frozen: number;
}

/** Response from eacn3_deposit. Confirms credits were added to the agent's balance. */
export interface DepositResponse {
  /** The agent that received the deposit. */
  agent_id: string;
  /** Amount of credits deposited in this transaction. */
  deposited: number;
  /** Updated spendable balance after the deposit. */
  available: number;
  /** Credits currently locked in escrow; unchanged by deposit. */
  frozen: number;
}

// ---------------------------------------------------------------------------
// Cluster / Health
// ---------------------------------------------------------------------------

/** A node in the EACN3 cluster. Returned as part of ClusterStatus. */
export interface ClusterMember {
  /** Unique identifier for this cluster node. */
  node_id: string;
  /** URL other nodes use to reach this member. */
  endpoint: string;
  /** Capability domains this node handles for routing. */
  domains: string[];
  /** Current connectivity; "offline" nodes may be temporarily unreachable. */
  status: "online" | "offline";
  /** ISO 8601 timestamp of the last successful heartbeat from this node. */
  last_seen: string;
}

/** Full cluster topology. Returned by eacn3_cluster_status. */
export interface ClusterStatus {
  /** "standalone" if this is the only node; "cluster" if part of a multi-node deployment. */
  mode: "standalone" | "cluster";
  /** Details about the local node (the one you are connected to). */
  local: {
    /** This node's unique identifier. */
    node_id: string;
    /** This node's reachable URL. */
    endpoint: string;
    /** Domains this node routes. */
    domains: string[];
    /** This node's current connectivity status. */
    status: "online" | "offline";
    /** Semantic version of the EACN3 software running on this node. */
    version: string;
    /** ISO 8601 timestamp when this node joined the cluster. */
    joined_at: string;
  };
  /** All nodes in the cluster, including the local node. */
  members: ClusterMember[];
  /** Total number of registered cluster members. */
  member_count: number;
  /** Number of members currently reachable. */
  online_count: number;
  /** Bootstrap URLs used for initial cluster discovery. */
  seed_nodes: string[];
}

/** Response from eacn3_health. Use to verify a node is reachable before connecting. */
export interface HealthResponse {
  /** "ok" = fully operational, "degraded" = partial functionality, "down" = unreachable (you won't actually get this in a response). */
  status: "ok" | "degraded" | "down";
  /** Additional diagnostic fields; structure varies by server implementation. */
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Local State
// ---------------------------------------------------------------------------

/** Lightweight local cache entry for a task this agent is involved with. */
export interface LocalTaskInfo {
  /** The task's unique identifier. */
  task_id: string;
  /** Whether this agent created the task ("initiator") or is working on it ("executor"). */
  role: "initiator" | "executor";
  /** Last-known lifecycle state; may be stale if not recently refreshed. */
  status: TaskStatus;
  /** Capability domains the task was broadcast to. */
  domains: string[];
  /** Truncated description for display; not the full TaskContent.description. */
  description_summary: string;
  /** ISO 8601 timestamp when the task was created. */
  created_at: string;
}

/** A single direct message between two agents. */
export interface DirectMessage {
  /** Sender agent ID. */
  from: string;
  /** Receiver agent ID. */
  to: string;
  /** Message content (text). */
  content: string;
  /** Unix timestamp in milliseconds. */
  timestamp: number;
  /** Direction relative to the local agent: "in" = received, "out" = sent. */
  direction: "in" | "out";
}

/**
 * Session key: `${local_agent_id}:${peer_agent_id}`.
 * Each session stores the conversation between a local agent and a remote peer.
 */
export type SessionKey = string;

/** Maximum messages per session to prevent unbounded growth. */
export const MAX_MESSAGES_PER_SESSION = 100;

/** Plugin-local state. Holds all in-memory data for the current session; reset on disconnect. */
export interface EacnState {
  /** Server identity; null before eacn3_connect is called. */
  server_card: ServerCard | null;
  /** Base URL of the network API this session is connected to. */
  network_endpoint: string;
  /** Registered agents keyed by agent_id. Populated by eacn3_register_agent. */
  agents: Record<string, AgentCard>;
  /** Tasks this session is tracking, keyed by task_id. Updated on create/bid/result operations. */
  local_tasks: Record<string, LocalTaskInfo>;
  /** Cached reputation scores keyed by agent_id; may be stale. Values are 0.0-1.0. */
  reputation_cache: Record<string, number>;
  /** Buffered WebSocket events not yet consumed. Drained by eacn3_get_events(). */
  pending_events: PushEvent[];
  /** Active message sessions keyed by "local_agent_id:peer_agent_id". */
  active_sessions: Record<SessionKey, DirectMessage[]>;
}

/**
 * Default network endpoint. Override with EACN3_NETWORK_URL env var.
 */
export const EACN3_DEFAULT_NETWORK_ENDPOINT =
  process.env.EACN3_NETWORK_URL ?? "https://network.eacn3.dev";

export function createDefaultState(networkEndpoint?: string): EacnState {
  return {
    server_card: null,
    network_endpoint: networkEndpoint ?? EACN3_DEFAULT_NETWORK_ENDPOINT,
    agents: {},
    local_tasks: {},
    reputation_cache: {},
    pending_events: [],
    active_sessions: {},
  };
}

// ---------------------------------------------------------------------------
// Tier / Level Helpers
// ---------------------------------------------------------------------------

/**
 * Check whether an agent tier is eligible to bid on a task level.
 *
 * Rule: tool-tier agents can ONLY bid on tool-level tasks.
 * All other tiers (general, expert, expert_general) can bid on ANY task level.
 * The tier is a self-declaration of specialization breadth, not a hard gate —
 * an expert should still be able to take general tasks.
 */
export function isTierEligible(agentTier: AgentTier, taskLevel: TaskLevel): boolean {
  if (agentTier === "tool") return taskLevel === "tool";
  return true;
}

/** Response from eacn3_invite_agent. Confirms the agent was added to the invite list. */
export interface InviteAgentResponse {
  /** Whether the invitation was recorded. */
  ok: boolean;
  /** The task the agent was invited to. */
  task_id: string;
  /** The agent that was invited. */
  agent_id: string;
  /** Status message. */
  message: string;
}
