/**
 * Event Transport — HTTP polling against the per-agent message queue.
 *
 * The network server maintains a persistent message queue (SQLite) for
 * every agent. All push events are written there unconditionally.
 * This module simply polls GET /api/events/{agent_id} to drain it.
 *
 * No WebSocket. No connection state. No reconnection logic.
 * Just HTTP GET on a timer.
 *
 * Same public API as the old ws-manager.ts — drop-in replacement.
 */

import { type PushEvent } from "./models.js";
import { getState, pushEvents } from "./state.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EventCallback = (agentId: string, event: PushEvent) => void;

export type TransportMode = "polling" | "disconnected";

interface AgentPoller {
  agentId: string;
  timer: ReturnType<typeof setTimeout> | null;
  polling: boolean;
  lastAckMsgId: string | null;
  consecutiveErrors: number;
  seenMsgIds: Set<string>;
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** Server-side long-poll timeout. Keep below typical HTTP gateway timeout (60s). */
const POLL_TIMEOUT_SEC = 25;

/** Delay before next poll after receiving events (fast follow-up). */
const POLL_FAST_MS = 500;

/** Delay before next poll after empty response. */
const POLL_IDLE_MS = 3_000;

/** Backoff cap on consecutive errors. */
const POLL_BACKOFF_MAX_MS = 30_000;

/** Sliding window size for msg_id dedup. */
const DEDUP_WINDOW = 500;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const pollers = new Map<string, AgentPoller>();
let eventCallback: EventCallback | null = null;

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function setEventCallback(cb: EventCallback): void {
  eventCallback = cb;
}

export function connect(agentId: string): void {
  disconnect(agentId);

  const p: AgentPoller = {
    agentId,
    timer: null,
    polling: false,
    lastAckMsgId: null,
    consecutiveErrors: 0,
    seenMsgIds: new Set(),
  };
  pollers.set(agentId, p);

  // Start polling immediately
  schedulePoll(p, 0);
  console.error(`[Transport] ${agentId} polling started`);
}

export function disconnect(agentId: string): void {
  const p = pollers.get(agentId);
  if (!p) return;
  if (p.timer) clearTimeout(p.timer);
  pollers.delete(agentId);
}

export function disconnectAll(): void {
  for (const id of [...pollers.keys()]) disconnect(id);
}

export function isConnected(agentId: string): boolean {
  return pollers.has(agentId);
}

export function connectedAgents(): string[] {
  return [...pollers.keys()];
}

export function getTransportStatus(agentId: string): {
  mode: TransportMode;
  consecutiveErrors: number;
} | null {
  const p = pollers.get(agentId);
  if (!p) return null;
  return { mode: "polling", consecutiveErrors: p.consecutiveErrors };
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

function pollUrl(agentId: string, timeout: number, ack?: string | null): string {
  const base = getState().network_endpoint;
  let url = `${base}/api/events/${agentId}?timeout=${timeout}`;
  if (ack) url += `&ack=${encodeURIComponent(ack)}`;
  return url;
}

function schedulePoll(p: AgentPoller, delayMs: number): void {
  if (p.timer) clearTimeout(p.timer);
  p.timer = setTimeout(() => {
    p.timer = null;
    doPoll(p);
  }, delayMs);
}

async function doPoll(p: AgentPoller): Promise<void> {
  if (!pollers.has(p.agentId) || p.polling) return;
  p.polling = true;

  const url = pollUrl(p.agentId, POLL_TIMEOUT_SEC, p.lastAckMsgId);
  p.lastAckMsgId = null;

  try {
    const resp = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout((POLL_TIMEOUT_SEC + 5) * 1000),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const body = (await resp.json()) as { events: any[]; count: number };
    p.consecutiveErrors = 0;

    if (body.count > 0) {
      for (const raw of body.events) {
        deliverEvent(p, raw);
      }
      schedulePoll(p, POLL_FAST_MS);
    } else {
      schedulePoll(p, POLL_IDLE_MS);
    }
  } catch (e) {
    p.consecutiveErrors++;
    const backoff = Math.min(
      POLL_IDLE_MS * 2 ** (p.consecutiveErrors - 1),
      POLL_BACKOFF_MAX_MS,
    );
    console.error(
      `[Transport] ${p.agentId} poll error #${p.consecutiveErrors}: ${(e as Error).message}, retry in ${backoff}ms`,
    );
    schedulePoll(p, backoff);
  } finally {
    p.polling = false;
  }
}

// ---------------------------------------------------------------------------
// Event delivery + dedup
// ---------------------------------------------------------------------------

function deliverEvent(p: AgentPoller, raw: any): void {
  const msgId: string = raw.msg_id ?? "";

  // Dedup
  if (msgId && p.seenMsgIds.has(msgId)) return;
  if (msgId) {
    p.seenMsgIds.add(msgId);
    if (p.seenMsgIds.size > DEDUP_WINDOW) {
      const first = p.seenMsgIds.values().next().value;
      if (first !== undefined) p.seenMsgIds.delete(first);
    }
  }

  // Piggybacked ACK on next poll
  if (msgId) p.lastAckMsgId = msgId;

  const pushEvent: PushEvent = {
    msg_id: msgId,
    type: raw.type,
    task_id: raw.task_id ?? "",
    payload: typeof raw.payload === "string" ? JSON.parse(raw.payload) : (raw.payload ?? {}),
    received_at: Date.now(),
  } as PushEvent;

  pushEvents([pushEvent]);

  if (eventCallback) {
    try { eventCallback(p.agentId, pushEvent); } catch { }
  }
}
