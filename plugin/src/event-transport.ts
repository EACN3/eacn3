/**
 * Event Transport — unified delivery layer with automatic fallback.
 *
 * Tries WebSocket first. If WS fails repeatedly, degrades to HTTP polling.
 * Exposes the same EventCallback interface so callers don't care which
 * transport is active.
 *
 * Transport hierarchy:
 *   1. WebSocket (low latency, bidirectional, but fragile)
 *   2. HTTP long-polling (reliable, works through any proxy, higher latency)
 *
 * Switching happens automatically:
 *   - WS fails N consecutive times → switch to HTTP polling
 *   - HTTP polling works → optionally probe WS again later
 */

import WebSocket from "ws";
import { type PushEvent } from "./models.js";
import { getState, pushEvents } from "./state.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EventCallback = (agentId: string, event: PushEvent) => void;

type TransportMode = "websocket" | "http_poll" | "disconnected";

interface TransportState {
  mode: TransportMode;
  agentId: string;
  /** WebSocket instance (null if using HTTP polling). */
  ws: WebSocket | null;
  /** Polling interval handle. */
  pollInterval: ReturnType<typeof setInterval> | null;
  /** Ping/keepalive interval for WS. */
  pingInterval: ReturnType<typeof setInterval> | null;
  /** Reconnect/retry timer. */
  retryTimeout: ReturnType<typeof setTimeout> | null;
  /** Consecutive WS failures. */
  wsFailures: number;
  /** AbortController for in-flight HTTP requests. */
  abortController: AbortController | null;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** Max consecutive WS failures before switching to HTTP polling. */
const WS_MAX_FAILURES = 3;

/** WS ping interval. */
const WS_PING_MS = 30_000;

/** Base reconnect delay (doubles each retry, capped). */
const RECONNECT_BASE_MS = 2_000;
const RECONNECT_MAX_MS = 30_000;

/** HTTP poll interval when in polling mode. */
const HTTP_POLL_INTERVAL_MS = 3_000;

/** HTTP long-poll timeout parameter sent to server. */
const HTTP_POLL_TIMEOUT_SEC = 25;

/** How often to probe WS while in HTTP-poll mode. */
const WS_PROBE_INTERVAL_MS = 120_000;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const transports = new Map<string, TransportState>();
let onEventCallback: EventCallback | null = null;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function setEventCallback(cb: EventCallback): void {
  onEventCallback = cb;
}

/**
 * Start receiving events for an agent. Tries WebSocket first.
 */
export function connect(agentId: string): void {
  // Clean up existing
  disconnect(agentId);

  const ts: TransportState = {
    mode: "disconnected",
    agentId,
    ws: null,
    pollInterval: null,
    pingInterval: null,
    retryTimeout: null,
    wsFailures: 0,
    abortController: null,
  };
  transports.set(agentId, ts);

  // Try WebSocket first
  connectWebSocket(ts);
}

/**
 * Stop receiving events for an agent.
 */
export function disconnect(agentId: string): void {
  const ts = transports.get(agentId);
  if (!ts) return;
  cleanupTransport(ts);
  transports.delete(agentId);
}

/**
 * Disconnect all agents.
 */
export function disconnectAll(): void {
  for (const agentId of transports.keys()) {
    disconnect(agentId);
  }
}

/**
 * Check if an agent has an active connection (any transport).
 */
export function isConnected(agentId: string): boolean {
  const ts = transports.get(agentId);
  if (!ts) return false;
  if (ts.mode === "websocket") return ts.ws?.readyState === WebSocket.OPEN;
  if (ts.mode === "http_poll") return true;
  return false;
}

/**
 * Get all connected agent IDs.
 */
export function connectedAgents(): string[] {
  return Array.from(transports.keys());
}

/**
 * Get transport status for debugging.
 */
export function getTransportStatus(agentId: string): {
  mode: TransportMode;
  wsFailures: number;
} | null {
  const ts = transports.get(agentId);
  if (!ts) return null;
  return { mode: ts.mode, wsFailures: ts.wsFailures };
}

// ---------------------------------------------------------------------------
// WebSocket Transport
// ---------------------------------------------------------------------------

function wsUrl(agentId: string): string {
  const httpUrl = getState().network_endpoint;
  const wsBase = httpUrl.replace(/^http/, "ws");
  return `${wsBase}/ws/${agentId}`;
}

function connectWebSocket(ts: TransportState): void {
  const url = wsUrl(ts.agentId);
  let ws: WebSocket;

  try {
    ws = new WebSocket(url);
  } catch {
    handleWsFailure(ts);
    return;
  }

  ts.ws = ws;

  ws.on("open", () => {
    ts.mode = "websocket";
    ts.wsFailures = 0; // reset on successful connection
    console.error(`[Transport] ${ts.agentId} connected via WebSocket`);

    // Start keepalive
    ts.pingInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
      }
    }, WS_PING_MS);
  });

  ws.on("message", (data) => handleWsMessage(ts.agentId, data));

  ws.on("close", () => {
    cleanupWs(ts);
    if (transports.has(ts.agentId)) {
      handleWsFailure(ts);
    }
  });

  ws.on("error", () => {
    // close event will follow
  });
}

function handleWsMessage(agentId: string, data: WebSocket.Data): void {
  try {
    const raw = typeof data === "string" ? data : data.toString("utf-8");
    if (raw === "pong") return;

    const event = JSON.parse(raw) as Omit<PushEvent, "received_at">;
    const pushEvent: PushEvent = { ...event, received_at: Date.now() } as PushEvent;
    pushEvents([pushEvent]);

    // ACK
    const ts = transports.get(agentId);
    if (event.msg_id && ts?.ws?.readyState === WebSocket.OPEN) {
      try { ts.ws.send(JSON.stringify({ ack: event.msg_id })); } catch { }
    }

    // Callback
    if (onEventCallback) {
      try { onEventCallback(agentId, pushEvent); } catch { }
    }
  } catch { }
}

function handleWsFailure(ts: TransportState): void {
  ts.wsFailures++;
  console.error(
    `[Transport] ${ts.agentId} WS failure #${ts.wsFailures}/${WS_MAX_FAILURES}`,
  );

  if (ts.wsFailures >= WS_MAX_FAILURES) {
    // Degrade to HTTP polling
    console.error(
      `[Transport] ${ts.agentId} switching to HTTP polling after ${ts.wsFailures} WS failures`,
    );
    startHttpPolling(ts);
    return;
  }

  // Retry WS with exponential backoff
  const delay = Math.min(RECONNECT_BASE_MS * 2 ** (ts.wsFailures - 1), RECONNECT_MAX_MS);
  ts.retryTimeout = setTimeout(() => {
    ts.retryTimeout = null;
    if (transports.has(ts.agentId)) {
      connectWebSocket(ts);
    }
  }, delay);
}

function cleanupWs(ts: TransportState): void {
  if (ts.pingInterval) { clearInterval(ts.pingInterval); ts.pingInterval = null; }
  if (ts.ws) {
    try { ts.ws.close(); } catch { }
    ts.ws = null;
  }
}

// ---------------------------------------------------------------------------
// HTTP Polling Transport
// ---------------------------------------------------------------------------

function pollUrl(agentId: string, timeout: number, ack?: string): string {
  const base = getState().network_endpoint;
  let url = `${base}/api/events/${agentId}?timeout=${timeout}`;
  if (ack) url += `&ack=${encodeURIComponent(ack)}`;
  return url;
}

function startHttpPolling(ts: TransportState): void {
  cleanupWs(ts);
  ts.mode = "http_poll";
  console.error(`[Transport] ${ts.agentId} HTTP polling started`);

  // Immediate first poll
  doHttpPoll(ts);

  // Regular polling interval
  ts.pollInterval = setInterval(() => {
    doHttpPoll(ts);
  }, HTTP_POLL_INTERVAL_MS);

  // Periodically probe WebSocket to see if it's come back
  scheduleWsProbe(ts);
}

let lastAck: string | undefined;

async function doHttpPoll(ts: TransportState): Promise<void> {
  // Abort any in-flight request
  if (ts.abortController) {
    ts.abortController.abort();
  }
  ts.abortController = new AbortController();

  const url = pollUrl(ts.agentId, HTTP_POLL_TIMEOUT_SEC, lastAck);
  lastAck = undefined;

  try {
    const resp = await fetch(url, {
      signal: ts.abortController.signal,
      headers: { Accept: "application/json" },
    });

    if (!resp.ok) {
      console.error(`[Transport] ${ts.agentId} HTTP poll error: ${resp.status}`);
      return;
    }

    const body = (await resp.json()) as { events: any[]; count: number };

    if (body.count > 0) {
      for (const raw of body.events) {
        const pushEvent: PushEvent = {
          msg_id: raw.msg_id ?? "",
          type: raw.type,
          task_id: raw.task_id ?? "",
          payload: typeof raw.payload === "string" ? JSON.parse(raw.payload) : raw.payload,
          received_at: Date.now(),
          _offline: raw._offline,
        } as PushEvent;

        pushEvents([pushEvent]);

        // Remember last msg_id for piggybacked ACK on next poll
        if (raw.msg_id) lastAck = raw.msg_id;

        if (onEventCallback) {
          try { onEventCallback(ts.agentId, pushEvent); } catch { }
        }
      }
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      console.error(`[Transport] ${ts.agentId} HTTP poll failed:`, (e as Error).message);
    }
  }
}

function scheduleWsProbe(ts: TransportState): void {
  // After some time in HTTP-poll mode, try WS once to see if it works
  setTimeout(() => {
    if (!transports.has(ts.agentId) || ts.mode !== "http_poll") return;

    console.error(`[Transport] ${ts.agentId} probing WebSocket...`);
    const probeWs = new WebSocket(wsUrl(ts.agentId));
    const probeTimeout = setTimeout(() => {
      try { probeWs.close(); } catch { }
    }, 5000);

    probeWs.on("open", () => {
      clearTimeout(probeTimeout);
      probeWs.close();
      // WS is back! Switch back from HTTP polling
      console.error(`[Transport] ${ts.agentId} WebSocket recovered, switching back`);
      stopHttpPolling(ts);
      ts.wsFailures = 0;
      connectWebSocket(ts);
    });

    probeWs.on("error", () => {
      clearTimeout(probeTimeout);
      // Still broken, schedule another probe
      scheduleWsProbe(ts);
    });
  }, WS_PROBE_INTERVAL_MS);
}

function stopHttpPolling(ts: TransportState): void {
  if (ts.pollInterval) { clearInterval(ts.pollInterval); ts.pollInterval = null; }
  if (ts.abortController) { ts.abortController.abort(); ts.abortController = null; }
}

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

function cleanupTransport(ts: TransportState): void {
  cleanupWs(ts);
  stopHttpPolling(ts);
  if (ts.retryTimeout) { clearTimeout(ts.retryTimeout); ts.retryTimeout = null; }
  ts.mode = "disconnected";
}
