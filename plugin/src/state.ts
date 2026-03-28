/**
 * Local state persistence — per-agent file isolation.
 *
 * Storage layout:
 *   ~/.eacn3/server.json              ← shared server identity (rarely written)
 *   ~/.eacn3/agents/{agent_id}.json   ← per-agent state (only one process writes)
 *   ~/.eacn3/events-{agent_id}.json   ← per-agent events (already isolated)
 *
 * Each CC session owns exactly one agent and only writes to its own files.
 * No cross-process file locking needed.
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, readdirSync, renameSync, unlinkSync } from "node:fs";
import { randomBytes } from "node:crypto";
import { join } from "node:path";
import { homedir } from "node:os";
import { type EacnState, type AgentCard, type LocalTaskInfo, type PushEvent, type DirectMessage, type SessionKey, type TeamInfo, MAX_MESSAGES_PER_SESSION, createDefaultState } from "./models.js";

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

const EACN3_DIR = process.env.EACN3_STATE_DIR ?? join(homedir(), ".eacn3");
const SERVER_FILE = join(EACN3_DIR, "server.json");
const AGENTS_DIR = join(EACN3_DIR, "agents");

/** Legacy state file — migrated on first load. */
const LEGACY_STATE_FILE = join(EACN3_DIR, "state.json");

// ---------------------------------------------------------------------------
// Per-agent data stored on disk
// ---------------------------------------------------------------------------

interface AgentData {
  agent: AgentCard;
  local_tasks: Record<string, LocalTaskInfo>;
  reputation_cache: Record<string, number>;
  active_sessions: Record<SessionKey, DirectMessage[]>;
  teams: Record<string, TeamInfo>;
}

interface ServerData {
  server_card: import("./models.js").ServerCard | null;
  network_endpoint: string;
}

// ---------------------------------------------------------------------------
// Atomic file write helper
// ---------------------------------------------------------------------------

function atomicWrite(filePath: string, data: string): void {
  const dir = join(filePath, "..");
  mkdirSync(dir, { recursive: true });
  const tmpFile = filePath + "." + randomBytes(4).toString("hex") + ".tmp";
  writeFileSync(tmpFile, data);
  renameSync(tmpFile, filePath);
}

function safeReadJSON<T>(filePath: string): T | null {
  try {
    if (!existsSync(filePath)) return null;
    return JSON.parse(readFileSync(filePath, "utf-8")) as T;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Singleton state
// ---------------------------------------------------------------------------

let state: EacnState | null = null;

// ---------------------------------------------------------------------------
// Server data (shared, rarely written)
// ---------------------------------------------------------------------------

function loadServerData(): ServerData {
  return safeReadJSON<ServerData>(SERVER_FILE) ?? { server_card: null, network_endpoint: "" };
}

/**
 * Persist server identity to disk. Only call this from eacn3_connect —
 * server_card and network_endpoint don't change outside of connect.
 */
export function saveServerData(): void {
  if (!state) return;
  const data: ServerData = {
    server_card: state.server_card,
    network_endpoint: state.network_endpoint,
  };
  atomicWrite(SERVER_FILE, JSON.stringify(data, null, 2));
}

// ---------------------------------------------------------------------------
// Per-agent data
// ---------------------------------------------------------------------------

function agentFilePath(agentId: string): string {
  return join(AGENTS_DIR, `${agentId}.json`);
}

function loadAgentData(agentId: string): AgentData | null {
  return safeReadJSON<AgentData>(agentFilePath(agentId));
}

function saveAgentData(agentId: string): void {
  if (!state) return;
  const agent = state.agents[agentId];
  if (!agent) return;

  const s = state;
  const prefix = `${agentId}:`;

  // Collect this agent's tasks
  const tasks: Record<string, LocalTaskInfo> = {};
  for (const [tid, task] of Object.entries(s.local_tasks)) {
    if (task.agent_id === agentId) tasks[tid] = task;
  }

  // Collect this agent's sessions
  const sessions: Record<SessionKey, DirectMessage[]> = {};
  if (s.active_sessions) {
    for (const [key, msgs] of Object.entries(s.active_sessions)) {
      if (key.startsWith(prefix)) sessions[key] = msgs;
    }
  }

  // Collect this agent's teams
  const teams: Record<string, TeamInfo> = {};
  if (s.teams) {
    for (const [key, team] of Object.entries(s.teams)) {
      if (team.my_agent_id === agentId) teams[key] = team;
    }
  }

  const data: AgentData = {
    agent,
    local_tasks: tasks,
    reputation_cache: { [agentId]: s.reputation_cache[agentId] ?? 0 },
    active_sessions: sessions,
    teams,
  };
  atomicWrite(agentFilePath(agentId), JSON.stringify(data, null, 2));
}

function loadAllAgents(): Record<string, AgentData> {
  const result: Record<string, AgentData> = {};
  if (!existsSync(AGENTS_DIR)) return result;
  try {
    for (const file of readdirSync(AGENTS_DIR)) {
      if (!file.endsWith(".json")) continue;
      const agentId = file.slice(0, -5); // strip .json
      const data = loadAgentData(agentId);
      if (data) result[agentId] = data;
    }
  } catch { /* dir unreadable */ }
  return result;
}

function removeAgentFile(agentId: string): void {
  const filePath = agentFilePath(agentId);
  try { if (existsSync(filePath)) unlinkSync(filePath); } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Migration from legacy state.json
// ---------------------------------------------------------------------------

function migrateLegacyState(): void {
  if (!existsSync(LEGACY_STATE_FILE)) return;

  let legacy: EacnState;
  try {
    legacy = JSON.parse(readFileSync(LEGACY_STATE_FILE, "utf-8")) as EacnState;
  } catch { return; }

  // Write server data
  const serverData: ServerData = {
    server_card: legacy.server_card,
    network_endpoint: legacy.network_endpoint,
  };
  atomicWrite(SERVER_FILE, JSON.stringify(serverData, null, 2));

  // Write per-agent data
  for (const [agentId, agent] of Object.entries(legacy.agents ?? {})) {
    const prefix = `${agentId}:`;
    const tasks: Record<string, LocalTaskInfo> = {};
    for (const [tid, task] of Object.entries(legacy.local_tasks ?? {})) {
      if (task.agent_id === agentId) tasks[tid] = task;
    }
    const sessions: Record<SessionKey, DirectMessage[]> = {};
    for (const [key, msgs] of Object.entries(legacy.active_sessions ?? {})) {
      if (key.startsWith(prefix)) sessions[key] = msgs;
    }
    const teams: Record<string, TeamInfo> = {};
    for (const [key, team] of Object.entries(legacy.teams ?? {})) {
      if (team.my_agent_id === agentId) teams[key] = team;
    }

    const data: AgentData = {
      agent,
      local_tasks: tasks,
      reputation_cache: { [agentId]: legacy.reputation_cache?.[agentId] ?? 0 },
      active_sessions: sessions,
      teams,
    };
    atomicWrite(agentFilePath(agentId), JSON.stringify(data, null, 2));
  }

  // Migrate per-agent pending_events to event files
  if (legacy.pending_events) {
    for (const [agentId, events] of Object.entries(legacy.pending_events)) {
      if (events.length > 0) {
        agentEvents.set(agentId, events);
        saveAgentEventsFile(agentId);
      }
    }
  }

  // Rename legacy file so we don't re-migrate
  try { renameSync(LEGACY_STATE_FILE, LEGACY_STATE_FILE + ".migrated"); } catch { /* best-effort */ }
  console.error("[State] migrated legacy state.json to per-agent files");
}

// ---------------------------------------------------------------------------
// Assemble / Disassemble EacnState
// ---------------------------------------------------------------------------

function assembleState(): EacnState {
  const server = loadServerData();
  const allAgents = loadAllAgents();

  const s = createDefaultState(server.network_endpoint || undefined);
  s.server_card = server.server_card;

  for (const [agentId, data] of Object.entries(allAgents)) {
    s.agents[agentId] = data.agent;
    Object.assign(s.local_tasks, data.local_tasks);
    Object.assign(s.reputation_cache, data.reputation_cache);
    if (!s.active_sessions) s.active_sessions = {};
    Object.assign(s.active_sessions, data.active_sessions);
    if (!s.teams) s.teams = {};
    Object.assign(s.teams, data.teams);
  }

  return s;
}

// ---------------------------------------------------------------------------
// Public API — same interface as before
// ---------------------------------------------------------------------------

/**
 * Load state from disk. Creates default if not exists.
 */
export function load(): EacnState {
  mkdirSync(EACN3_DIR, { recursive: true });
  migrateLegacyState();
  state = assembleState();
  return state;
}

/**
 * Serialize save operations to prevent concurrent write races (#107).
 */
let saveQueued = false;
let saving = false;

/**
 * Persist current state to disk.
 * Writes server.json + per-agent files for agents known to this session.
 */
export function save(): void {
  if (!state) return;
  if (saving) { saveQueued = true; return; }
  saving = true;
  try {
    for (const agentId of Object.keys(state.agents)) {
      saveAgentData(agentId);
    }
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

  // Remove per-agent files
  removeAgentFile(agentId);
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
// Per-agent event files (unchanged — already isolated)
// ---------------------------------------------------------------------------

/** In-memory cache of per-agent events. */
const agentEvents = new Map<string, PushEvent[]>();

function eventsFilePath(agentId: string): string {
  return join(EACN3_DIR, `events-${agentId}.json`);
}

function loadAgentEventsFromFile(agentId: string): PushEvent[] {
  if (agentEvents.has(agentId)) return agentEvents.get(agentId)!;
  const filePath = eventsFilePath(agentId);
  try {
    if (existsSync(filePath)) {
      const raw = readFileSync(filePath, "utf-8");
      const events = JSON.parse(raw) as PushEvent[];
      agentEvents.set(agentId, events);
      return events;
    }
  } catch { /* corrupted — start fresh */ }
  agentEvents.set(agentId, []);
  return [];
}

function saveAgentEventsFile(agentId: string): void {
  const events = agentEvents.get(agentId) ?? [];
  try {
    mkdirSync(EACN3_DIR, { recursive: true });
    const filePath = eventsFilePath(agentId);
    const tmpFile = filePath + "." + randomBytes(4).toString("hex") + ".tmp";
    writeFileSync(tmpFile, JSON.stringify(events));
    renameSync(tmpFile, filePath);
  } catch { /* best-effort */ }
}

export function pushEvents(agentId: string, events: PushEvent[]): void {
  const existing = loadAgentEventsFromFile(agentId);
  existing.push(...events);
  saveAgentEventsFile(agentId);
}

export function drainEvents(agentId: string): PushEvent[] {
  const events = loadAgentEventsFromFile(agentId);
  agentEvents.set(agentId, []);
  saveAgentEventsFile(agentId);
  return events;
}

/** Drain events for ALL agents at once (used by legacy callers). */
export function drainAllEvents(): PushEvent[] {
  const all: PushEvent[] = [];
  for (const [agentId, events] of agentEvents) {
    all.push(...events);
    agentEvents.set(agentId, []);
    saveAgentEventsFile(agentId);
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
