/**
 * EACN3 data models — TypeScript interfaces matching network-api.md structures.
 */
/**
 * Ordered tier hierarchy for comparison. Lower index = higher tier.
 */
export const AGENT_TIER_HIERARCHY = ["general", "expert", "expert_general", "tool"];
/** Maximum messages per session to prevent unbounded growth. */
export const MAX_MESSAGES_PER_SESSION = 100;
/**
 * Default network endpoint. Override with EACN3_NETWORK_URL env var.
 */
export const EACN3_DEFAULT_NETWORK_ENDPOINT = process.env.EACN3_NETWORK_URL ?? "https://network.eacn3.dev";
export function createDefaultState(networkEndpoint) {
    return {
        server_card: null,
        network_endpoint: networkEndpoint ?? EACN3_DEFAULT_NETWORK_ENDPOINT,
        agents: {},
        local_tasks: {},
        reputation_cache: {},
        pending_events: {},
        active_sessions: {},
    };
}
// ---------------------------------------------------------------------------
// Tier / Level Helpers
// ---------------------------------------------------------------------------
/**
 * Check whether an agent tier is eligible to bid on a task level.
 *
 * Rule: tool-tier agents can ONLY bid on tool-level tasks.
 * All other tiers (general, expert, expert_general) can bid on ANY task level.
 * The tier is a self-declaration of specialization breadth, not a hard gate —
 * an expert should still be able to take general tasks.
 */
export function isTierEligible(agentTier, taskLevel) {
    if (agentTier === "tool")
        return taskLevel === "tool";
    return true;
}
//# sourceMappingURL=models.js.map