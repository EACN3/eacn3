/**
 * Local state persistence — reads/writes ~/.eacn3/state.json.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, copyFileSync, renameSync, unlinkSync, openSync, closeSync, constants as fsConstants } from "node:fs";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import { homedir } from "node:os";
import { type EacnState, type AgentCard, type LocalTaskInfo, type PushEvent, type DirectMessage, type SessionKey, type TeamInfo, MAX_MESSAGES_PER_SESSION, createDefaultState } from "./models.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const EACN3_DIR = process.env.EACN3_STATE_DIR ?? join(homedir(), ".eacn3");
const STATE_FILE = join(EACN3_DIR, "state.json");
const STATE_BACKUP = join(EACN3_DIR, "state.json.bak");
const STATE_LOCK = join(EACN3_DIR, "state.lock");

// ---------------------------------------------------------------------------
// Cross-process file lock — prevents multi-session races on state.json
// ---------------------------------------------------------------------------

const LOCK_RETRY_MS = 50;
const LOCK_MAX_RETRIES = 20; // 50ms × 20 = 1s max wait
const LOCK_STALE_MS = 10_000; // force-break locks older than 10s

/**
 * Acquire an exclusive lock file. Returns true on success.
 * Uses O_CREAT | O_EXCL for atomic creation. Retries with backoff.
 * Stale locks (>10s old) are force-removed to prevent deadlock from crashed processes.
 */
function acquireLock(): boolean {
  mkdirSync(EACN3_DIR, { recursive: true });
  for (let i = 0; i < LOCK_MAX_RETRIES; i++) {
    try {
      const fd = openSync(STATE_LOCK, fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_WRONLY);
      // Write PID + timestamp for stale detection
      writeFileSync(fd, `${process.pid}:${Date.now()}`);
      closeSync(fd);
      return true;
    } catch {
      // Lock exists — check if stale
      try {
        const raw = readFileSync(STATE_LOCK, "utf-8");
        const ts = parseInt(raw.split(":")[1], 10);
        if (Date.now() - ts > LOCK_STALE_MS) {
          // Stale lock from crashed process — force remove
          try { unlinkSync(STATE_LOCK); } catch { /* race with another cleaner */ }
          continue;
        }
      } catch { /* lock file unreadable — retry */ }

      // Busy-wait with small delay
      const waitMs = LOCK_RETRY_MS + Math.random() * LOCK_RETRY_MS;
      const start = Date.now();
      while (Date.now() - start < waitMs) { /* spin */ }
    }
  }
  console.error("[State] failed to acquire lock after retries — proceeding unlocked");
  return false;
}

function releaseLock(): void {
  try { unlinkSync(STATE_LOCK); } catch { /* already removed */ }
}

// ---------------------------------------------------------------------------
// Singleton state
// ---------------------------------------------------------------------------

let state: EacnState | null = null;

/**
 * Read state from disk (no side effects on the singleton).
 * Tries primary file, then backup, then returns default.
 */
function readStateFromDisk(): EacnState {
  if (existsSync(STATE_FILE)) {
    try {
      const raw = readFileSync(STATE_FILE, "utf-8");
      return JSON.parse(raw) as EacnState;
    } catch { /* primary corrupted */ }
  }
  if (existsSync(STATE_BACKUP)) {
    try {
      const bak = readFileSync(STATE_BACKUP, "utf-8");
      return JSON.parse(bak) as EacnState;
    } catch { /* backup also corrupted */ }
  }
  return createDefaultState();
}

/**
 * Load state from disk. Creates default if not exists.
 */
export function load(): EacnState {
  acquireLock();
  try {
    state = readStateFromDisk();
  } finally {
    releaseLock();
  }

  // Migrate pending_events from main state to per-agent files
  if (state.pending_events) {
    for (const [agentId, events] of Object.entries(state.pending_events)) {
      if (events.length > 0) {
        agentEvents.set(agentId, events);
        saveAgentEvents(agentId);
      }
    }
    state.pending_events = {};
  }

  snapshotKnownIds();
  return state;
}

/**
 * Serialize save operations to prevent concurrent write races (#107).
 */
let saveQueued = false;
let saving = false;

/**
 * Merge in-memory state onto a freshly-read disk state.
 * This preserves changes made by OTHER processes (agents, tasks, sessions)
 * while applying our in-memory mutations on top.
 *
 * Strategy: our in-memory state wins for keys we actively manage,
 * but we pick up new agents/tasks/sessions that appeared on disk.
 */
function mergeWithDisk(mem: EacnState): EacnState {
  let disk: EacnState;
  try {
    disk = readStateFromDisk();
  } catch {
    return mem; // can't read disk — just use memory
  }

  // Merge agents: keep all from disk, overwrite with ours
  disk.agents = { ...disk.agents, ...mem.agents };
  // Remove agents we explicitly deleted (not in mem but were in previous load)
  for (const id of Object.keys(disk.agents)) {
    if (!(id in mem.agents) && id in (lastKnownAgentIds)) {
      delete disk.agents[id];
    }
  }

  // Merge local_tasks: same strategy
  disk.local_tasks = { ...disk.local_tasks, ...mem.local_tasks };
  for (const id of Object.keys(disk.local_tasks)) {
    if (!(id in mem.local_tasks) && id in lastKnownTaskIds) {
      delete disk.local_tasks[id];
    }
  }

  // Merge reputation_cache
  disk.reputation_cache = { ...disk.reputation_cache, ...mem.reputation_cache };

  // Merge active_sessions
  disk.active_sessions = { ...(disk.active_sessions ?? {}), ...(mem.active_sessions ?? {}) };

  // Merge teams
  disk.teams = { ...(disk.teams ?? {}), ...(mem.teams ?? {}) };

  // Use our server_card and network_endpoint (we own these)
  disk.server_card = mem.server_card;
  disk.network_endpoint = mem.network_endpoint;

  return disk;
}

/** Track IDs from last load so we can detect intentional deletions. */
let lastKnownAgentIds: Record<string, true> = {};
let lastKnownTaskIds: Record<string, true> = {};

function snapshotKnownIds(): void {
  if (!state) return;
  lastKnownAgentIds = {};
  for (const id of Object.keys(state.agents)) lastKnownAgentIds[id] = true;
  lastKnownTaskIds = {};
  for (const id of Object.keys(state.local_tasks)) lastKnownTaskIds[id] = true;
}

/**
 * Persist current state to disk using cross-process lock + atomic write.
 * Re-reads disk state and merges to prevent lost updates from other processes.
 */
export function save(): void {
  if (!state) return;
  if (saving) { saveQueued = true; return; }
  saving = true;
  acquireLock();
  try {
    mkdirSync(EACN3_DIR, { recursive: true });

    // Merge with disk to avoid overwriting other processes' changes
    state = mergeWithDisk(state);
    snapshotKnownIds();

    // Backup current file before overwriting
    if (existsSync(STATE_FILE)) {
      try { copyFileSync(STATE_FILE, STATE_BACKUP); } catch { /* best-effort */ }
    }
    // Atomic write: write to temp, then rename (#107)
    const tmpFile = STATE_FILE + "." + randomBytes(4).toString("hex") + ".tmp";
    writeFileSync(tmpFile, JSON.stringify(state, null, 2));
    renameSync(tmpFile, STATE_FILE);
  } finally {
    releaseLock();
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
  const s = getState();

  // Remove agent record
  delete s.agents[agentId];

  // Remove agent's local tasks
  for (const [taskId, task] of Object.entries(s.local_tasks)) {
    if (task.agent_id === agentId) {
      delete s.local_tasks[taskId];
    }
  }

  // Remove agent's reputation cache
  delete s.reputation_cache[agentId];

  // Remove agent's message sessions
  if (s.active_sessions) {
    for (const key of Object.keys(s.active_sessions)) {
      if (key.startsWith(`${agentId}:`)) {
        delete s.active_sessions[key];
      }
    }
  }

  // Remove agent's team records
  if (s.teams) {
    for (const [key, team] of Object.entries(s.teams)) {
      if (team.my_agent_id === agentId) {
        delete s.teams[key];
      }
    }
  }

  save();

  // Remove per-agent event file
  agentEvents.delete(agentId);
  const evtFile = eventsFilePath(agentId);
  try { if (existsSync(evtFile)) unlinkSync(evtFile); } catch { /* best-effort */ }
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

// ---------------------------------------------------------------------------
// Per-agent event files — isolated from main state to avoid async contention
// ---------------------------------------------------------------------------

/** In-memory cache of per-agent events. */
const agentEvents = new Map<string, PushEvent[]>();

function eventsFilePath(agentId: string): string {
  return join(EACN3_DIR, `events-${agentId}.json`);
}

function loadAgentEvents(agentId: string): PushEvent[] {
  if (agentEvents.has(agentId)) return agentEvents.get(agentId)!;
  const filePath = eventsFilePath(agentId);
  acquireEventsLock(agentId);
  try {
    if (existsSync(filePath)) {
      const raw = readFileSync(filePath, "utf-8");
      const events = JSON.parse(raw) as PushEvent[];
      agentEvents.set(agentId, events);
      return events;
    }
  } catch { /* corrupted — start fresh */ }
  finally { releaseEventsLock(agentId); }
  agentEvents.set(agentId, []);
  return [];
}

/** Per-agent event lock file path. */
function eventsLockPath(agentId: string): string {
  return join(EACN3_DIR, `events-${agentId}.lock`);
}

function acquireEventsLock(agentId: string): boolean {
  const lockPath = eventsLockPath(agentId);
  mkdirSync(EACN3_DIR, { recursive: true });
  for (let i = 0; i < LOCK_MAX_RETRIES; i++) {
    try {
      const fd = openSync(lockPath, fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_WRONLY);
      writeFileSync(fd, `${process.pid}:${Date.now()}`);
      closeSync(fd);
      return true;
    } catch {
      try {
        const raw = readFileSync(lockPath, "utf-8");
        const ts = parseInt(raw.split(":")[1], 10);
        if (Date.now() - ts > LOCK_STALE_MS) {
          try { unlinkSync(lockPath); } catch { /* race */ }
          continue;
        }
      } catch { /* retry */ }
      const waitMs = LOCK_RETRY_MS + Math.random() * LOCK_RETRY_MS;
      const start = Date.now();
      while (Date.now() - start < waitMs) { /* spin */ }
    }
  }
  return false;
}

function releaseEventsLock(agentId: string): void {
  try { unlinkSync(eventsLockPath(agentId)); } catch { /* already removed */ }
}

function saveAgentEvents(agentId: string): void {
  const events = agentEvents.get(agentId) ?? [];
  acquireEventsLock(agentId);
  try {
    mkdirSync(EACN3_DIR, { recursive: true });
    const filePath = eventsFilePath(agentId);
    const tmpFile = filePath + "." + randomBytes(4).toString("hex") + ".tmp";
    writeFileSync(tmpFile, JSON.stringify(events));
    renameSync(tmpFile, filePath);
  } catch { /* best-effort */ }
  finally { releaseEventsLock(agentId); }
}

export function pushEvents(agentId: string, events: PushEvent[]): void {
  const existing = loadAgentEvents(agentId);
  existing.push(...events);
  saveAgentEvents(agentId);
}

export function drainEvents(agentId: string): PushEvent[] {
  const events = loadAgentEvents(agentId);
  agentEvents.set(agentId, []);
  saveAgentEvents(agentId);
  return events;
}

/** Drain events for ALL agents at once (used by legacy callers). */
export function drainAllEvents(): PushEvent[] {
  const all: PushEvent[] = [];
  for (const [agentId, events] of agentEvents) {
    all.push(...events);
    agentEvents.set(agentId, []);
    saveAgentEvents(agentId);
  }
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

// ---------------------------------------------------------------------------
// Team coordination
// ---------------------------------------------------------------------------

function ensureTeams(): Record<string, TeamInfo> {
  const s = getState();
  if (!s.teams) s.teams = {};
  return s.teams;
}

export function addTeam(team: TeamInfo): void {
  ensureTeams()[`${team.team_id}:${team.my_agent_id}`] = team;
  save();
}

export function getTeam(teamId: string): TeamInfo | undefined {
  // Try exact key first, then fallback to team_id prefix match
  const teams = ensureTeams();
  if (teams[teamId]) return teams[teamId];
  return Object.values(teams).find((t) => t.team_id === teamId);
}

export function getTeamsForAgent(agentId: string): TeamInfo[] {
  return Object.values(ensureTeams()).filter(
    (t) => t.my_agent_id === agentId,
  );
}

export function updateTeamPeerBranch(
  teamId: string,
  peerId: string,
  branch: string,
): void {
  const teams = ensureTeams();
  const entries = Object.values(teams).filter((t) => t.team_id === teamId);
  for (const team of entries) {
    team.peer_branches[peerId] = branch;
    // Check if all peers have branches → team ready
    const peers = team.agent_ids.filter((id) => id !== team.my_agent_id);
    if (peers.every((id) => id in team.peer_branches)) {
      team.status = "ready";
    }
    save();
  }
}

export function setTeamBranch(teamId: string, branch: string): void {
  const teams = ensureTeams();
  let saved = false;
  for (const team of Object.values(teams)) {
    if (team.team_id === teamId) {
      team.my_branch = branch;
      saved = true;
    }
  }
  if (saved) save();
}

/** Find team by handshake task ID (in either ack_out or ack_in). */
export function findTeamByHandshakeTask(taskId: string): { team: TeamInfo; direction: "out" | "in"; peerId: string } | undefined {
  for (const team of Object.values(ensureTeams())) {
    for (const [peerId, tid] of Object.entries(team.ack_out)) {
      if (tid === taskId) return { team, direction: "out", peerId };
    }
    for (const [peerId, tid] of Object.entries(team.ack_in)) {
      if (tid === taskId) return { team, direction: "in", peerId };
    }
  }
  return undefined;
}

/** Record an incoming handshake task for a team. */
export function recordAckIn(teamId: string, agentId: string, peerId: string, taskId: string): void {
  const team = Object.values(ensureTeams()).find(
    (t) => t.team_id === teamId && t.my_agent_id === agentId,
  );
  if (team) {
    team.ack_in[peerId] = taskId;
    save();
  }
}
