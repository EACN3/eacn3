/**
 * WebSocket manager — one connection per registered Agent.
 *
 * Events are buffered in memory. Host retrieves via eacn_get_events (drainEvents).
 * Auto-reconnect on disconnect. Ping keepalive.
 */

import WebSocket from "ws";
import { type PushEvent } from "./models.js";
import { getState, pushEvents } from "./state.js";

// ---------------------------------------------------------------------------
// Event callback — server.ts registers a handler for auto-actions
// ---------------------------------------------------------------------------

type EventCallback = (agentId: string, event: PushEvent) => void;
let onEventCallback: EventCallback | null = null;

export function setEventCallback(cb: EventCallback): void {
  onEventCallback = cb;
}

// ---------------------------------------------------------------------------
// Connection state
// ---------------------------------------------------------------------------

interface AgentConnection {
  ws: WebSocket;
  agentId: string;
  pingInterval: ReturnType<typeof setInterval>;
  reconnectTimeout: ReturnType<typeof setTimeout> | null;
}

const connections = new Map<string, AgentConnection>();

const PING_INTERVAL_MS = 30_000;
const RECONNECT_DELAY_MS = 5_000;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function wsUrl(agentId: string): string {
  const httpUrl = getState().network_endpoint;
  const wsBase = httpUrl.replace(/^http/, "ws");
  return `${wsBase}/ws/${agentId}`;
}

function handleMessage(agentId: string, data: WebSocket.Data): void {
  try {
    const raw = typeof data === "string" ? data : data.toString("utf-8");
    if (raw === "pong") return; // keepalive response
    const event = JSON.parse(raw) as Omit<PushEvent, "received_at">;
    const pushEvent: PushEvent = { ...event, received_at: Date.now() } as PushEvent;
    pushEvents([pushEvent]);

    // Trigger registered callback for auto-actions
    if (onEventCallback) {
      try { onEventCallback(agentId, pushEvent); } catch { /* callback errors non-fatal */ }
    }
  } catch {
    // Ignore malformed messages
  }
}

function scheduleReconnect(agentId: string): void {
  const existing = connections.get(agentId);
  if (existing?.reconnectTimeout) return; // already scheduled

  const timeout = setTimeout(() => {
    if (connections.has(agentId)) {
      const conn = connections.get(agentId)!;
      conn.reconnectTimeout = null;
      // Only reconnect if we haven't explicitly disconnected
      connect(agentId);
    }
  }, RECONNECT_DELAY_MS);

  if (existing) {
    existing.reconnectTimeout = timeout;
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Establish WebSocket connection for an Agent.
 */
export function connect(agentId: string): void {
  // Close existing connection if any
  const existing = connections.get(agentId);
  if (existing) {
    if (existing.pingInterval) clearInterval(existing.pingInterval);
    if (existing.reconnectTimeout) clearTimeout(existing.reconnectTimeout);
    try { existing.ws.close(); } catch { /* ignore */ }
    connections.delete(agentId);
  }

  const url = wsUrl(agentId);
  let ws: WebSocket;
  try {
    ws = new WebSocket(url);
  } catch {
    // Connection failed — schedule retry without leaking a timer
    connections.set(agentId, {
      ws: null as unknown as WebSocket,
      agentId,
      pingInterval: null as unknown as ReturnType<typeof setInterval>,
      reconnectTimeout: null,
    });
    scheduleReconnect(agentId);
    return;
  }

  const pingInterval = setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send("ping");
    }
  }, PING_INTERVAL_MS);

  const conn: AgentConnection = { ws, agentId, pingInterval, reconnectTimeout: null };
  connections.set(agentId, conn);

  ws.on("message", (data) => handleMessage(agentId, data));

  ws.on("close", () => {
    // Auto-reconnect if still in connections map (not explicitly disconnected)
    if (connections.has(agentId)) {
      scheduleReconnect(agentId);
    }
  });

  ws.on("error", () => {
    // Error will be followed by close event — reconnect handled there
  });
}

/**
 * Disconnect a specific Agent's WebSocket.
 */
export function disconnect(agentId: string): void {
  const conn = connections.get(agentId);
  if (!conn) return;

  if (conn.pingInterval) clearInterval(conn.pingInterval);
  if (conn.reconnectTimeout) clearTimeout(conn.reconnectTimeout);
  try { conn.ws.close(); } catch { /* ignore */ }
  connections.delete(agentId);
}

/**
 * Disconnect all WebSocket connections.
 */
export function disconnectAll(): void {
  for (const agentId of connections.keys()) {
    disconnect(agentId);
  }
}

/**
 * Check if an Agent has an active WebSocket connection.
 */
export function isConnected(agentId: string): boolean {
  const conn = connections.get(agentId);
  return conn !== undefined && conn.ws?.readyState === WebSocket.OPEN;
}

/**
 * Get all connected agent IDs.
 */
export function connectedAgents(): string[] {
  return Array.from(connections.keys());
}
