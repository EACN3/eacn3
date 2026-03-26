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
 * Public API: connect/disconnect/isConnected per agent.
 */
import { getState, pushEvents } from "./state.js";
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
const pollers = new Map();
let eventCallback = null;
// Per-agent callbacks for multi-agent isolation (#109)
const agentCallbacks = new Map();
// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
export function setEventCallback(cb) {
    eventCallback = cb;
}
/**
 * Register a per-agent event callback (#109).
 * If set, this callback is used instead of the global one for this agent.
 */
export function setAgentEventCallback(agentId, cb) {
    agentCallbacks.set(agentId, cb);
}
export function removeAgentEventCallback(agentId) {
    agentCallbacks.delete(agentId);
}
export function connect(agentId) {
    disconnect(agentId);
    const p = {
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
export function disconnect(agentId) {
    const p = pollers.get(agentId);
    if (!p)
        return;
    if (p.timer)
        clearTimeout(p.timer);
    pollers.delete(agentId);
}
export function disconnectAll() {
    for (const id of [...pollers.keys()])
        disconnect(id);
}
export function isConnected(agentId) {
    return pollers.has(agentId);
}
export function connectedAgents() {
    return [...pollers.keys()];
}
export function getTransportStatus(agentId) {
    const p = pollers.get(agentId);
    if (!p)
        return null;
    return { mode: "polling", consecutiveErrors: p.consecutiveErrors };
}
// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
function pollUrl(agentId, timeout, ack) {
    const base = getState().network_endpoint;
    let url = `${base}/api/events/${agentId}?timeout=${timeout}`;
    if (ack)
        url += `&ack=${encodeURIComponent(ack)}`;
    return url;
}
function schedulePoll(p, delayMs) {
    if (p.timer)
        clearTimeout(p.timer);
    p.timer = setTimeout(() => {
        p.timer = null;
        doPoll(p);
    }, delayMs);
}
async function doPoll(p) {
    if (!pollers.has(p.agentId) || p.polling)
        return;
    p.polling = true;
    const url = pollUrl(p.agentId, POLL_TIMEOUT_SEC, p.lastAckMsgId);
    p.lastAckMsgId = null;
    try {
        const resp = await fetch(url, {
            headers: { Accept: "application/json" },
            signal: AbortSignal.timeout((POLL_TIMEOUT_SEC + 5) * 1000),
        });
        if (!resp.ok)
            throw new Error(`HTTP ${resp.status}`);
        const body = (await resp.json());
        p.consecutiveErrors = 0;
        if (body.count > 0) {
            for (const raw of body.events) {
                deliverEvent(p, raw);
            }
            schedulePoll(p, POLL_FAST_MS);
        }
        else {
            schedulePoll(p, POLL_IDLE_MS);
        }
    }
    catch (e) {
        p.consecutiveErrors++;
        const backoff = Math.min(POLL_IDLE_MS * 2 ** (p.consecutiveErrors - 1), POLL_BACKOFF_MAX_MS);
        console.error(`[Transport] ${p.agentId} poll error #${p.consecutiveErrors}: ${e.message}, retry in ${backoff}ms`);
        schedulePoll(p, backoff);
    }
    finally {
        p.polling = false;
    }
}
// ---------------------------------------------------------------------------
// Event delivery + dedup
// ---------------------------------------------------------------------------
function deliverEvent(p, raw) {
    const msgId = raw.msg_id ?? "";
    // Dedup
    if (msgId && p.seenMsgIds.has(msgId))
        return;
    if (msgId) {
        p.seenMsgIds.add(msgId);
        if (p.seenMsgIds.size > DEDUP_WINDOW) {
            const first = p.seenMsgIds.values().next().value;
            if (first !== undefined)
                p.seenMsgIds.delete(first);
        }
    }
    // Piggybacked ACK on next poll
    if (msgId)
        p.lastAckMsgId = msgId;
    const pushEvent = {
        msg_id: msgId,
        type: raw.type,
        task_id: raw.task_id ?? "",
        payload: typeof raw.payload === "string" ? JSON.parse(raw.payload) : (raw.payload ?? {}),
        received_at: Date.now(),
    };
    pushEvents(p.agentId, [pushEvent]);
    // Use per-agent callback if registered, otherwise global (#109)
    const cb = agentCallbacks.get(p.agentId) ?? eventCallback;
    if (cb) {
        try {
            Promise.resolve(cb(p.agentId, pushEvent)).catch(() => { });
        }
        catch { }
    }
}
//# sourceMappingURL=event-transport.js.map