/**
 * Event Transport — on-demand HTTP fetch against the per-agent message queue.
 *
 * The network server maintains a persistent message queue (SQLite) for
 * every agent. This module provides a single `fetchEvents()` function that
 * fetches events from the network when an agent explicitly asks for them
 * (via eacn3_next, eacn3_get_events, or eacn3_await_events).
 *
 * No background polling. No timers. No persistent connections.
 * Events are only fetched when the agent requests them.
 *
 * Public API: fetchEvents / isRegistered / registeredAgents.
 */

import { type PushEvent } from "./models.js";
import { getState } from "./state.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type EventCallback = (agentId: string, event: PushEvent) => void | Promise<void>;

export type TransportMode = "on-demand" | "inactive";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** Default server-side long-poll timeout for on-demand fetch. */
const DEFAULT_FETCH_TIMEOUT_SEC = 5;

/** Sliding window size for msg_id dedup. */
const DEDUP_WINDOW = 500;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/** Tracks which agents are registered (for isRegistered checks). */
const registeredAgentIds = new Set<string>();

/** Per-agent last ACK msg_id for server-side cursor. */
const lastAckMsgIds = new Map<string, string>();

/** Per-agent dedup window. */
const seenMsgIds = new Map<string, Set<string>>();

let eventCallback: EventCallback | null = null;
const agentCallbacks = new Map<string, EventCallback>();

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function setEventCallback(cb: EventCallback): void {
  eventCallback = cb;
}

/**
 * Register a per-agent event callback (#109).
 * If set, this callback is used instead of the global one for this agent.
 */
export function setAgentEventCallback(agentId: string, cb: EventCallback): void {
  agentCallbacks.set(agentId, cb);
}

export function removeAgentEventCallback(agentId: string): void {
  agentCallbacks.delete(agentId);
}

/** Mark an agent as registered (replaces old connect). */
export function connect(agentId: string): void {
  registeredAgentIds.add(agentId);
  if (!seenMsgIds.has(agentId)) seenMsgIds.set(agentId, new Set());
  console.error(`[Transport] ${agentId} registered for on-demand fetch`);
}

/** Mark an agent as unregistered (replaces old disconnect). */
export function disconnect(agentId: string): void {
  registeredAgentIds.delete(agentId);
  lastAckMsgIds.delete(agentId);
  seenMsgIds.delete(agentId);
  agentCallbacks.delete(agentId);
}

/** Unregister all agents. */
export function disconnectAll(): void {
  registeredAgentIds.clear();
  lastAckMsgIds.clear();
  seenMsgIds.clear();
  agentCallbacks.clear();
}

/** Check if an agent is registered for event fetching. */
export function isConnected(agentId: string): boolean {
  return registeredAgentIds.has(agentId);
}

/** List all registered agent IDs. */
export function connectedAgents(): string[] {
  return [...registeredAgentIds];
}

export function getTransportStatus(agentId: string): {
  mode: TransportMode;
  consecutiveErrors: number;
} | null {
  if (!registeredAgentIds.has(agentId)) return null;
  return { mode: "on-demand", consecutiveErrors: 0 };
}

// ---------------------------------------------------------------------------
// On-demand fetch
// ---------------------------------------------------------------------------

/**
 * Fetch events from the network for a specific agent. Called on-demand
 * when the agent invokes eacn3_next / eacn3_get_events / eacn3_await_events.
 *
 * Returns parsed PushEvent[]. Also fires the event callback for each event
 * (reverse control, state updates, auto-bid, etc.).
 *
 * @param agentId  Agent to fetch events for
 * @param timeoutSec  Server-side long-poll timeout (0 = non-blocking). Default 5s.
 */
export async function fetchEvents(agentId: string, timeoutSec: number = DEFAULT_FETCH_TIMEOUT_SEC): Promise<PushEvent[]> {
  const base = getState().network_endpoint;
  if (!base) return [];

  const ack = lastAckMsgIds.get(agentId);
  let url = `${base}/api/events/${agentId}?timeout=${timeoutSec}`;
  if (ack) url += `&ack=${encodeURIComponent(ack)}`;

  try {
    const resp = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout((timeoutSec + 5) * 1000),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const body = (await resp.json()) as { events: any[]; count: number };
    const events: PushEvent[] = [];
    const dedupSet = seenMsgIds.get(agentId) ?? new Set();

    for (const raw of body.events) {
      const msgId: string = raw.msg_id ?? "";

      // Dedup
      if (msgId && dedupSet.has(msgId)) continue;
      if (msgId) {
        dedupSet.add(msgId);
        if (dedupSet.size > DEDUP_WINDOW) {
          const first = dedupSet.values().next().value;
          if (first !== undefined) dedupSet.delete(first);
        }
      }

      // Track ACK cursor
      if (msgId) lastAckMsgIds.set(agentId, msgId);

      const pushEvent: PushEvent = {
        msg_id: msgId,
        type: raw.type,
        task_id: raw.task_id ?? "",
        payload: typeof raw.payload === "string" ? JSON.parse(raw.payload) : (raw.payload ?? {}),
        received_at: Date.now(),
      } as PushEvent;

      events.push(pushEvent);

      // Fire event callback (reverse control, state updates, etc.)
      const cb = agentCallbacks.get(agentId) ?? eventCallback;
      if (cb) {
        try { await Promise.resolve(cb(agentId, pushEvent)); } catch { }
      }
    }

    return events;
  } catch (e) {
    console.error(`[Transport] ${agentId} fetch error: ${(e as Error).message}`);
    return [];
  }
}
