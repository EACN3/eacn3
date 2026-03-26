/**
 * Local state persistence — reads/writes ~/.eacn3/state.json.
 */
import { type EacnState, type AgentCard, type LocalTaskInfo, type PushEvent, type DirectMessage } from "./models.js";
/**
 * Load state from disk. Creates default if not exists.
 */
export declare function load(): EacnState;
/**
 * Persist current state to disk using atomic write (#107).
 * Writes to a temp file first, then renames to avoid partial writes.
 */
export declare function save(): void;
/**
 * Get current state (loads from disk if not yet loaded).
 */
export declare function getState(): EacnState;
/**
 * Replace entire state.
 */
export declare function setState(newState: EacnState): void;
export declare function addAgent(agent: AgentCard): void;
export declare function removeAgent(agentId: string): void;
export declare function getAgent(agentId: string): AgentCard | undefined;
export declare function listAgents(): AgentCard[];
export declare function updateTask(info: LocalTaskInfo): void;
export declare function removeTask(taskId: string): void;
export declare function updateTaskStatus(taskId: string, status: string): void;
export declare function getTask(taskId: string): import("./models.js").LocalTaskInfo | undefined;
export declare function pushEvents(agentId: string, events: PushEvent[]): void;
export declare function drainEvents(agentId: string): PushEvent[];
/** Drain events for ALL agents at once (used by legacy callers). */
export declare function drainAllEvents(): PushEvent[];
export declare function updateReputationCache(agentId: string, score: number): void;
export declare function isConnected(): boolean;
export declare function getServerId(): string | null;
/**
 * Add a message to a session. Creates the session if it doesn't exist.
 * Trims to MAX_MESSAGES_PER_SESSION, dropping oldest messages.
 */
export declare function addMessage(localAgentId: string, msg: DirectMessage): void;
/**
 * Get all messages in a session between a local agent and a peer.
 */
export declare function getMessages(localAgentId: string, peerAgentId: string): DirectMessage[];
/**
 * List all active session keys for a local agent.
 * Returns peer agent IDs.
 */
export declare function listSessions(localAgentId: string): string[];
