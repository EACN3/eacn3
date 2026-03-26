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
import { type PushEvent } from "./models.js";
export type EventCallback = (agentId: string, event: PushEvent) => void | Promise<void>;
export type TransportMode = "polling" | "disconnected";
export declare function setEventCallback(cb: EventCallback): void;
/**
 * Register a per-agent event callback (#109).
 * If set, this callback is used instead of the global one for this agent.
 */
export declare function setAgentEventCallback(agentId: string, cb: EventCallback): void;
export declare function removeAgentEventCallback(agentId: string): void;
export declare function connect(agentId: string): void;
export declare function disconnect(agentId: string): void;
export declare function disconnectAll(): void;
export declare function isConnected(agentId: string): boolean;
export declare function connectedAgents(): string[];
export declare function getTransportStatus(agentId: string): {
    mode: TransportMode;
    consecutiveErrors: number;
} | null;
