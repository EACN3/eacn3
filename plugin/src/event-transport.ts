/**
 * Event Transport — reliable event delivery over plain HTTP.
 *
 * HTTP is universally supported. WebSocket is not — many proxies, CDNs,
 * serverless platforms, and corporate firewalls block the upgrade handshake.
 *
 * Strategy:
 *   1. HTTP long-polling (default, works everywhere)
 *   2. WebSocket (optional upgrade, only if explicitly enabled or auto-detected)
 *
 * The server persists undelivered messages in OfflineStore (SQLite).
 * HTTP polling drains them via GET /api/events/{agent_id}. This means:
 *   - No messages are lost during disconnection
 *   - No special protocol support needed (just HTTP GET)
 *   - Works behind any proxy, CDN, or load balancer
 *
 * Same public API as the old ws-manager.ts — drop-in replacement.
 */

import WebSocket from "ws";
import { type PushEvent } from "./models.js";
import { getState, pushEvents } from "./state.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EventCallback = (agentId: string, event: PushEvent) => void;

export type TransportMode = "http_poll" | "websocket" | "disconnected";

interface AgentTransport {
  agentId: string;
  mode: TransportMode;

  // -- HTTP polling --
  pollTimer: ReturnType<typeof setTimeout> | null;
  polling: boolean;              // guard against overlapping polls
  lastAckMsgId: string | null;  // piggybacked ACK on next poll
  consecutivePollErrors: number;

  // -- WebSocket (optional upgrade) --
  ws: WebSocket | null;
  pingInterval: ReturnType<typeof setInterval> | null;
  pongReceived: boolean;         // dead-connection detection
  wsFailures: number;

  // -- Shared --
  retryTimeout: ReturnType<typeof setTimeout> | null;
  /** msg_ids seen recently — dedup on reconnect / transport switch. */
  seenMsgIds: Set<string>;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** HTTP long-poll: server-side timeout (seconds). Keep < typical HTTP timeout (60s). */
const POLL_SERVER_TIMEOUT_SEC = 25;

/** How soon to poll again after getting results. */
const POLL_INTERVAL_FAST_MS = 1_000;

/** How soon to poll again after an empty response. */
const POLL_INTERVAL_IDLE_MS = 5_000;

/** Backoff cap on poll errors. */
const POLL_BACKOFF_MAX_MS = 30_000;

/** WS ping interval and pong timeout. */
const WS_PING_MS = 25_000;
const WS_PONG_TIMEOUT_MS = 10_000;

/** Max msg_ids to remember for dedup (sliding window). */
const DEDUP_WINDOW = 500;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const transports = new Map<string, AgentTransport>();
let eventCallback: EventCallback | null = null;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function setEventCallback(cb: EventCallback): void {
  eventCallback = cb;
}

/**
 * Start receiving events for an agent.
 * Default: HTTP polling. Pass `{ preferWebSocket: true }` to try WS first.
 */
export function connect(agentId: string, opts?: { preferWebSocket?: boolean }): void {
  disconnect(agentId);

  const t: AgentTransport = {
    agentId,
    mode: "disconnected",
    pollTimer: null,
    polling: false,
    lastAckMsgId: null,
    consecutivePollErrors: 0,
    ws: null,
    pingInterval: null,
    pongReceived: false,
    wsFailures: 0,
    retryTimeout: null,
    seenMsgIds: new Set(),
  };
  transports.set(agentId, t);

  if (opts?.preferWebSocket) {
    startWebSocket(t);
  } else {
    startHttpPoll(t);
  }
}

export function disconnect(agentId: string): void {
  const t = transports.get(agentId);
  if (!t) return;
  cleanup(t);
  transports.delete(agentId);
}

export function disconnectAll(): void {
  for (const id of [...transports.keys()]) disconnect(id);
}

export function isConnected(agentId: string): boolean {
  const t = transports.get(agentId);
  if (!t) return false;
  if (t.mode === "http_poll") return true; // stateless — always "connected"
  if (t.mode === "websocket") return t.ws?.readyState === WebSocket.OPEN;
  return false;
}

export function connectedAgents(): string[] {
  return [...transports.keys()];
}

export function getTransportStatus(agentId: string): {
  mode: TransportMode;
  wsFailures: number;
  pollErrors: number;
} | null {
  const t = transports.get(agentId);
  if (!t) return null;
  return {
    mode: t.mode,
    wsFailures: t.wsFailures,
    pollErrors: t.consecutivePollErrors,
  };
}

// ---------------------------------------------------------------------------
// HTTP Long-Polling
// ---------------------------------------------------------------------------

function pollUrl(agentId: string, timeout: number, ack?: string | null): string {
  const base = getState().network_endpoint;
  let url = `${base}/api/events/${agentId}?timeout=${timeout}`;
  if (ack) url += `&ack=${encodeURIComponent(ack)}`;
  return url;
}

function startHttpPoll(t: AgentTransport): void {
  t.mode = "http_poll";
  console.error(`[Transport] ${t.agentId} HTTP polling started`);
  schedulePoll(t, 0); // immediate first poll
}

function schedulePoll(t: AgentTransport, delayMs: number): void {
  if (t.pollTimer) clearTimeout(t.pollTimer);
  t.pollTimer = setTimeout(() => {
    t.pollTimer = null;
    doPoll(t);
  }, delayMs);
}

async function doPoll(t: AgentTransport): Promise<void> {
  if (t.mode !== "http_poll" || t.polling) return;
  t.polling = true;

  const url = pollUrl(t.agentId, POLL_SERVER_TIMEOUT_SEC, t.lastAckMsgId);
  t.lastAckMsgId = null;

  try {
    const resp = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(
        (POLL_SERVER_TIMEOUT_SEC + 5) * 1000, // slightly longer than server timeout
      ),
    });

    if (!resp.ok) {
      throw new Error(`HTTP ${resp.status}`);
    }

    const body = (await resp.json()) as { events: any[]; count: number };
    t.consecutivePollErrors = 0;

    if (body.count > 0) {
      for (const raw of body.events) {
        deliverEvent(t, raw);
      }
      // Got events — poll again quickly for more
      schedulePoll(t, POLL_INTERVAL_FAST_MS);
    } else {
      // Empty — back to normal interval
      schedulePoll(t, POLL_INTERVAL_IDLE_MS);
    }
  } catch (e) {
    t.consecutivePollErrors++;
    const backoff = Math.min(
      POLL_INTERVAL_IDLE_MS * 2 ** (t.consecutivePollErrors - 1),
      POLL_BACKOFF_MAX_MS,
    );
    console.error(
      `[Transport] ${t.agentId} poll error #${t.consecutivePollErrors}: ${(e as Error).message}, retry in ${backoff}ms`,
    );
    schedulePoll(t, backoff);
  } finally {
    t.polling = false;
  }
}

// ---------------------------------------------------------------------------
// WebSocket (Optional Upgrade)
// ---------------------------------------------------------------------------

function wsUrl(agentId: string): string {
  const httpUrl = getState().network_endpoint;
  return httpUrl.replace(/^http/, "ws") + `/ws/${agentId}`;
}

function startWebSocket(t: AgentTransport): void {
  let ws: WebSocket;
  try {
    ws = new WebSocket(wsUrl(t.agentId));
  } catch {
    // WS not available at all — fall back to HTTP immediately
    console.error(`[Transport] ${t.agentId} WS constructor failed, using HTTP`);
    startHttpPoll(t);
    return;
  }

  t.ws = ws;

  ws.on("open", () => {
    t.mode = "websocket";
    t.wsFailures = 0;
    t.pongReceived = true;
    console.error(`[Transport] ${t.agentId} WebSocket connected`);

    // Ping/pong with dead-connection detection
    t.pingInterval = setInterval(() => {
      if (!t.pongReceived) {
        // Server didn't respond to last ping — connection is dead
        console.error(`[Transport] ${t.agentId} pong timeout, reconnecting`);
        teardownWs(t);
        handleWsDown(t);
        return;
      }
      t.pongReceived = false;
      if (ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
      }
    }, WS_PING_MS);
  });

  ws.on("message", (data) => {
    const raw = typeof data === "string" ? data : data.toString("utf-8");
    if (raw === "pong") { t.pongReceived = true; return; }

    try {
      const event = JSON.parse(raw);
      deliverEvent(t, event);
      // ACK
      if (event.msg_id && ws.readyState === WebSocket.OPEN) {
        try { ws.send(JSON.stringify({ ack: event.msg_id })); } catch { }
      }
    } catch { }
  });

  ws.on("close", () => {
    teardownWs(t);
    if (transports.has(t.agentId)) handleWsDown(t);
  });

  ws.on("error", () => { /* close follows */ });
}

function handleWsDown(t: AgentTransport): void {
  t.wsFailures++;

  // After 3 failures, give up on WS and use HTTP
  if (t.wsFailures >= 3) {
    console.error(
      `[Transport] ${t.agentId} WS failed ${t.wsFailures}x, falling back to HTTP`,
    );
    startHttpPoll(t);
    return;
  }

  // Exponential backoff retry
  const delay = Math.min(2000 * 2 ** (t.wsFailures - 1), 30_000);
  console.error(`[Transport] ${t.agentId} WS retry in ${delay}ms`);
  t.retryTimeout = setTimeout(() => {
    t.retryTimeout = null;
    if (transports.has(t.agentId)) startWebSocket(t);
  }, delay);
}

function teardownWs(t: AgentTransport): void {
  if (t.pingInterval) { clearInterval(t.pingInterval); t.pingInterval = null; }
  if (t.ws) {
    try { t.ws.close(); } catch { }
    t.ws = null;
  }
}

// ---------------------------------------------------------------------------
// Shared: event delivery + dedup
// ---------------------------------------------------------------------------

function deliverEvent(t: AgentTransport, raw: any): void {
  const msgId: string = raw.msg_id ?? "";

  // Dedup: skip if we've seen this msg_id recently
  if (msgId && t.seenMsgIds.has(msgId)) return;
  if (msgId) {
    t.seenMsgIds.add(msgId);
    // Evict oldest entries when window is full
    if (t.seenMsgIds.size > DEDUP_WINDOW) {
      const first = t.seenMsgIds.values().next().value;
      if (first !== undefined) t.seenMsgIds.delete(first);
    }
  }

  // Remember for piggybacked ACK (HTTP mode)
  if (msgId && t.mode === "http_poll") {
    t.lastAckMsgId = msgId;
  }

  const pushEvent: PushEvent = {
    msg_id: msgId,
    type: raw.type,
    task_id: raw.task_id ?? "",
    payload: typeof raw.payload === "string" ? JSON.parse(raw.payload) : (raw.payload ?? {}),
    received_at: Date.now(),
    _offline: raw._offline,
  } as PushEvent;

  pushEvents([pushEvent]);

  if (eventCallback) {
    try { eventCallback(t.agentId, pushEvent); } catch { }
  }
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

function cleanup(t: AgentTransport): void {
  // HTTP
  if (t.pollTimer) { clearTimeout(t.pollTimer); t.pollTimer = null; }
  // WS
  teardownWs(t);
  // Shared
  if (t.retryTimeout) { clearTimeout(t.retryTimeout); t.retryTimeout = null; }
  t.mode = "disconnected";
}
