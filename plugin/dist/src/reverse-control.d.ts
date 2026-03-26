/**
 * MCP Reverse Control Engine
 *
 * Enables the MCP Server to proactively drive the Host LLM via:
 * 1. Sampling (sampling/createMessage) — ask LLM to reason and decide
 * 2. Notifications — push state updates to Host
 * 3. Enhanced tool results — inject pending events into any tool response (fallback)
 *
 * When push events arrive from the EACN3 network, instead of just buffering
 * them for polling, this engine evaluates each event and may invoke the Host's LLM
 * to make a decision (bid on a task, reply to a message, etc.).
 */
import type { Server } from "@modelcontextprotocol/sdk/server/index.js";
import type { PushEvent } from "./models.js";
/** Which reverse control mechanism to use for a given event type. */
export type ReverseMethod = "sampling" | "notification" | "auto_action" | "buffer_only";
/** Per-event-type configuration. */
export interface EventPolicy {
    method: ReverseMethod;
    /** For auto_action: what to do automatically without LLM involvement. */
    autoAction?: string;
}
/** Reverse control configuration for an agent. */
export interface ReverseControlConfig {
    enabled: boolean;
    policies: Record<string, EventPolicy>;
}
/**
 * Initialize the reverse control engine with the MCP server instance.
 * Call this after the MCP server is connected and transport is ready.
 */
export declare function init(server: Server): void;
/**
 * Register reverse control config for an agent.
 * Merges with defaults — only override what you specify.
 */
export declare function configure(agentId: string, partial?: Partial<ReverseControlConfig>): void;
/**
 * Remove config when agent unregisters.
 */
export declare function unconfigure(agentId: string): void;
/**
 * Main entry point: process a WebSocket event through the reverse control engine.
 * Called by event-transport's callback instead of directly buffering.
 *
 * Returns true if the event was handled (sampling/notification/auto-action).
 * Returns false if it should fall through to normal event buffering.
 */
export declare function handleEvent(agentId: string, event: PushEvent): Promise<boolean>;
/**
 * Drain pending directives for a given agent (or all agents).
 * Called by tool result wrapper to inject into responses.
 *
 * Returns formatted text to append to tool results, or null if none.
 */
export declare function drainDirectives(agentId?: string): string | null;
/**
 * Check if there are any pending directives (for deciding whether to inject).
 */
export declare function hasPendingDirectives(agentId?: string): boolean;
/**
 * Get current reverse control status for debugging.
 */
export declare function getStatus(): {
    samplingAvailable: boolean;
    configuredAgents: string[];
    pendingDirectiveCount: number;
    samplingCallsInWindow: number;
};
/**
 * Force re-detection of client capabilities.
 * Useful after reconnection or capability negotiation.
 */
export declare function refreshCapabilities(): void;
