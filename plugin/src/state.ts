/**
 * Local state persistence — reads/writes ~/.eacn3/state.json.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, copyFileSync, renameSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import { homedir } from "node:os";
import { type EacnState, type AgentCard, type LocalTaskInfo, type PushEvent, type DirectMessage, type SessionKey, MAX_MESSAGES_PER_SESSION, createDefaultState } from "./models.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const EACN3_DIR = process.env.EACN3_STATE_DIR ?? join(homedir(), ".eacn3");
const STATE_FILE = join(EACN3_DIR, "state.json");
const STATE_BACKUP = join(EACN3_DIR, "state.json.bak");

// ---------------------------------------------------------------------------
// Singleton state
// ---------------------------------------------------------------------------

let state: EacnState | null = null;

/**
 * Load state from disk. Creates default if not exists.
 */
export function load(): EacnState {
  if (existsSync(STATE_FILE)) {
    try {
      const raw = readFileSync(STATE_FILE, "utf-8");
      state = JSON.parse(raw) as EacnState;
    } catch {
      // Primary corrupted — try backup
      if (existsSync(STATE_BACKUP)) {
        try {
          const bak = readFileSync(STATE_BACKUP, "utf-8");
          state = JSON.parse(bak) as EacnState;
        } catch {
          state = createDefaultState();
        }
      } else {
        state = createDefaultState();
      }
    }
  } else {
    state = createDefaultState();
  }
  return state;
}

/**
 * Serialize save operations to prevent concurrent write races (#107).
 */
let saveQueued = false;
let saving = false;

/**
 * Persist current state to disk using atomic write (#107).
 * Writes to a temp file first, then renames to avoid partial writes.
 */
export function save(): void {
  if (!state) return;
  if (saving) { saveQueued = true; return; }
  saving = true;
  try {
    mkdirSync(EACN3_DIR, { recursive: true });
    // Backup current file before overwriting
    if (existsSync(STATE_FILE)) {
      try { copyFileSync(STATE_FILE, STATE_BACKUP); } catch { /* best-effort */ }
    }
    // Atomic write: write to temp, then rename (#107)
    const tmpFile = STATE_FILE + "." + randomBytes(4).toString("hex") + ".tmp";
    writeFileSync(tmpFile, JSON.stringify(state, null, 2));
    renameSync(tmpFile, STATE_FILE);
  } finally {
    saving = false;
    if (saveQueued) {
      saveQueued = false;
      save();
    }
  }
}

/**
 * Get current state (loads from disk if not yet loaded).
 */
export function getState(): EacnState {
  if (!state) load();
  return state!;
}

/**
 * Replace entire state.
 */
export function setState(newState: EacnState): void {
  state = newState;
}

// ---------------------------------------------------------------------------
// Convenience methods
// ---------------------------------------------------------------------------

export function addAgent(agent: AgentCard): void {
  getState().agents[agent.agent_id] = agent;
  save();
}

export function removeAgent(agentId: string): void {
  delete getState().agents[agentId];
  save();
}

export function getAgent(agentId: string): AgentCard | undefined {
  return getState().agents[agentId];
}

export function listAgents(): AgentCard[] {
  return Object.values(getState().agents);
}

export function updateTask(info: LocalTaskInfo): void {
  getState().local_tasks[info.task_id] = info;
  save();
}

export function removeTask(taskId: string): void {
  delete getState().local_tasks[taskId];
  save();
}

export function updateTaskStatus(taskId: string, status: string): void {
  const task = getState().local_tasks[taskId];
  if (task) {
    task.status = status as import("./models.js").TaskStatus;
    save();
  }
}

export function getTask(taskId: string): import("./models.js").LocalTaskInfo | undefined {
  return getState().local_tasks[taskId];
}

export function pushEvents(agentId: string, events: PushEvent[]): void {
  const s = getState();
  if (!s.pending_events[agentId]) s.pending_events[agentId] = [];
  s.pending_events[agentId].push(...events);
  save();
}

export function drainEvents(agentId: string): PushEvent[] {
  const s = getState();
  const events = s.pending_events[agentId] ?? [];
  s.pending_events[agentId] = [];
  save();
  return events;
}

/** Drain events for ALL agents at once (used by legacy callers). */
export function drainAllEvents(): PushEvent[] {
  const s = getState();
  const all: PushEvent[] = [];
  for (const events of Object.values(s.pending_events)) {
    all.push(...events);
  }
  s.pending_events = {};
  save();
  return all;
}

export function updateReputationCache(agentId: string, score: number): void {
  getState().reputation_cache[agentId] = score;
  save();
}

export function isConnected(): boolean {
  return getState().server_card !== null;
}

export function getServerId(): string | null {
  return getState().server_card?.server_id ?? null;
}

// ---------------------------------------------------------------------------
// Message sessions
// ---------------------------------------------------------------------------

function sessionKey(localAgentId: string, peerAgentId: string): SessionKey {
  return `${localAgentId}:${peerAgentId}`;
}

/**
 * Add a message to a session. Creates the session if it doesn't exist.
 * Trims to MAX_MESSAGES_PER_SESSION, dropping oldest messages.
 */
export function addMessage(localAgentId: string, msg: DirectMessage): void {
  const s = getState();
  // Ensure active_sessions exists (backward compat with old state files)
  if (!s.active_sessions) s.active_sessions = {};

  const peerId = msg.direction === "in" ? msg.from : msg.to;
  const key = sessionKey(localAgentId, peerId);

  if (!s.active_sessions[key]) {
    s.active_sessions[key] = [];
  }
  s.active_sessions[key].push(msg);

  // Trim oldest if over limit
  if (s.active_sessions[key].length > MAX_MESSAGES_PER_SESSION) {
    s.active_sessions[key] = s.active_sessions[key].slice(-MAX_MESSAGES_PER_SESSION);
  }

  save();
}

/**
 * Get all messages in a session between a local agent and a peer.
 */
export function getMessages(localAgentId: string, peerAgentId: string): DirectMessage[] {
  const s = getState();
  if (!s.active_sessions) return [];
  return s.active_sessions[sessionKey(localAgentId, peerAgentId)] ?? [];
}

/**
 * List all active session keys for a local agent.
 * Returns peer agent IDs.
 */
export function listSessions(localAgentId: string): string[] {
  const s = getState();
  if (!s.active_sessions) return [];
  const prefix = `${localAgentId}:`;
  return Object.keys(s.active_sessions)
    .filter((k) => k.startsWith(prefix))
    .map((k) => k.slice(prefix.length));
}
