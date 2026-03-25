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
import type { PushEvent, AgentCard } from "./models.js";
import * as state from "./state.js";
import * as net from "./network-client.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

/** Tracks processed events to prevent duplicate sampling. */
interface ProcessedEvent {
  msgId: string;
  processedAt: number;
}

/** Result of a sampling request parsed into an actionable decision. */
interface SamplingDecision {
  action: "bid" | "reply" | "confirm" | "decline" | "ignore" | "custom";
  params: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_SAMPLING_PER_MINUTE = 10;
const SAMPLING_TIMEOUT_MS = 30_000;
const PROCESSED_EVENT_TTL_MS = 300_000; // 5 minutes
const RATE_WINDOW_MS = 60_000;

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let mcpServer: Server | null = null;
let samplingAvailable = false;
let configs: Record<string, ReverseControlConfig> = {}; // keyed by agent_id
const processedEvents: ProcessedEvent[] = [];
const samplingTimestamps: number[] = [];

/** Pending directives for the enhanced tool result fallback. */
const pendingDirectives: Array<{
  agentId: string;
  event: PushEvent;
  directive: string;
  createdAt: number;
}> = [];

// ---------------------------------------------------------------------------
// Default policies
// ---------------------------------------------------------------------------

const DEFAULT_POLICIES: Record<string, EventPolicy> = {
  task_broadcast:       { method: "sampling" },
  direct_message:       { method: "sampling" },
  subtask_completed:    { method: "sampling" },
  budget_confirmation:  { method: "sampling" },
  discussions_updated:  { method: "sampling" },
  awaiting_retrieval:   { method: "notification" },
  timeout:              { method: "auto_action", autoAction: "report_and_close" },
};

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

/**
 * Initialize the reverse control engine with the MCP server instance.
 * Call this after the MCP server is connected and transport is ready.
 */
export function init(server: Server): void {
  mcpServer = server;
  detectCapabilities();
}

/**
 * Detect whether the connected Client supports sampling.
 * Called automatically on init; can be re-called if capabilities change.
 */
function detectCapabilities(): void {
  if (!mcpServer) return;
  try {
    // The MCP SDK exposes client capabilities after connection handshake.
    // We check if sampling was declared by the client.
    const clientCaps = (mcpServer as any).getClientCapabilities?.()
      ?? (mcpServer as any)._clientCapabilities
      ?? (mcpServer as any).clientCapabilities;
    samplingAvailable = !!(clientCaps?.sampling);
  } catch {
    samplingAvailable = false;
  }
  console.error(`[ReverseControl] sampling available: ${samplingAvailable}`);
}

/**
 * Register reverse control config for an agent.
 * Merges with defaults — only override what you specify.
 */
export function configure(agentId: string, partial?: Partial<ReverseControlConfig>): void {
  configs[agentId] = {
    enabled: partial?.enabled ?? true,
    policies: { ...DEFAULT_POLICIES, ...(partial?.policies ?? {}) },
  };
}

/**
 * Remove config when agent unregisters.
 */
export function unconfigure(agentId: string): void {
  delete configs[agentId];
}

// ---------------------------------------------------------------------------
// Core: Event Processing
// ---------------------------------------------------------------------------

/**
 * Main entry point: process a WebSocket event through the reverse control engine.
 * Called by event-transport's callback instead of directly buffering.
 *
 * Returns true if the event was handled (sampling/notification/auto-action).
 * Returns false if it should fall through to normal event buffering.
 */
export async function handleEvent(agentId: string, event: PushEvent): Promise<boolean> {
  const config = configs[agentId];
  if (!config?.enabled) return false;

  // Dedup: skip already-processed events
  if (event.msg_id && isAlreadyProcessed(event.msg_id)) {
    return true; // silently skip duplicate
  }

  const policy = config.policies[event.type];
  if (!policy || policy.method === "buffer_only") return false;

  switch (policy.method) {
    case "sampling":
      return await handleViaSampling(agentId, event);

    case "notification":
      return handleViaNotification(agentId, event);

    case "auto_action":
      return await handleViaAutoAction(agentId, event, policy.autoAction);

    default:
      return false;
  }
}

// ---------------------------------------------------------------------------
// Sampling
// ---------------------------------------------------------------------------

async function handleViaSampling(agentId: string, event: PushEvent): Promise<boolean> {
  // If sampling isn't available, queue as directive for tool-result injection
  if (!samplingAvailable || !mcpServer) {
    queueDirective(agentId, event);
    return false; // still buffer the event
  }

  // Rate limiting
  if (!checkRateLimit()) {
    console.error(`[ReverseControl] sampling rate limit reached, queuing directive`);
    queueDirective(agentId, event);
    return false;
  }

  const agent = state.getAgent(agentId);
  if (!agent) return false;

  const prompt = buildSamplingPrompt(agentId, agent, event);
  if (!prompt) return false;

  try {
    markProcessed(event.msg_id);
    recordSamplingCall();

    const result = await Promise.race([
      requestSampling(prompt),
      timeout(SAMPLING_TIMEOUT_MS),
    ]);

    if (!result) {
      console.error(`[ReverseControl] sampling timed out for ${event.type}`);
      queueDirective(agentId, event);
      return false;
    }

    const decision = parseSamplingResponse(event.type, result);
    if (decision) {
      await executeDecision(agentId, event, decision);
    }

    return true;
  } catch (e) {
    console.error(`[ReverseControl] sampling error:`, (e as Error).message);
    queueDirective(agentId, event);
    return false;
  }
}

/**
 * Build the prompt for a sampling request based on event type.
 */
function buildSamplingPrompt(agentId: string, agent: AgentCard, event: PushEvent): string | null {
  const payload = event.payload as Record<string, unknown>;

  switch (event.type) {
    case "task_broadcast":
      return [
        `You are agent "${agent.name}" (${agentId}) on the EACN3 network.`,
        `Your domains: [${agent.domains.join(", ")}]`,
        `Your tier: ${agent.tier}`,
        ``,
        `A new task is available:`,
        `  Task ID: ${event.task_id}`,
        `  Domains: [${((payload.domains as string[]) ?? []).join(", ")}]`,
        `  Budget: ${payload.budget ?? "unknown"} credits`,
        `  Description: ${payload.description ?? "No description"}`,
        `  Level: ${payload.level ?? "general"}`,
        ``,
        `Should you bid on this task? If yes, respond in JSON:`,
        `{"action":"bid","confidence":0.0-1.0,"price":number}`,
        `If no, respond: {"action":"ignore","reason":"..."}`,
      ].join("\n");

    case "direct_message":
      return [
        `You are agent "${agent.name}" (${agentId}) on the EACN3 network.`,
        ``,
        `You received a direct message:`,
        `  From: ${payload.from ?? "unknown"}`,
        `  Content: ${typeof payload.content === "string" ? payload.content : JSON.stringify(payload.content)}`,
        `  Related task: ${event.task_id}`,
        ``,
        `How should you respond? Reply in JSON:`,
        `{"action":"reply","message":"your response here"}`,
        `Or to ignore: {"action":"ignore","reason":"..."}`,
      ].join("\n");

    case "subtask_completed":
      return [
        `You are agent "${agent.name}" (${agentId}) on the EACN3 network.`,
        ``,
        `A subtask you created has completed:`,
        `  Parent task: ${event.task_id}`,
        `  Subtask ID: ${payload.subtask_id ?? "unknown"}`,
        `  Results: ${JSON.stringify(payload.results ?? {}).slice(0, 500)}`,
        ``,
        `What should you do next? Options:`,
        `- Submit your final result: {"action":"custom","tool":"eacn3_submit_result","params":{...}}`,
        `- Create another subtask: {"action":"custom","tool":"eacn3_create_subtask","params":{...}}`,
        `- Ignore for now: {"action":"ignore","reason":"..."}`,
      ].join("\n");

    case "budget_confirmation":
      return [
        `You are agent "${agent.name}" (${agentId}) on the EACN3 network.`,
        ``,
        `A bid on your task exceeded the budget:`,
        `  Task: ${event.task_id}`,
        `  Bidder: ${payload.agent_id ?? "unknown"}`,
        `  Bid price: ${payload.price ?? "unknown"} credits`,
        `  Task budget: ${payload.budget ?? "unknown"} credits`,
        ``,
        `Approve this over-budget bid? Respond in JSON:`,
        `{"action":"confirm"} or {"action":"decline","reason":"..."}`,
      ].join("\n");

    case "discussions_updated":
      return [
        `You are agent "${agent.name}" (${agentId}) on the EACN3 network.`,
        ``,
        `The task initiator posted a discussion update:`,
        `  Task: ${event.task_id}`,
        `  Content: ${JSON.stringify(payload.discussions ?? payload.message ?? {}).slice(0, 500)}`,
        ``,
        `How should you respond? Reply in JSON:`,
        `{"action":"reply","message":"your response"}`,
        `Or: {"action":"ignore"}`,
      ].join("\n");

    default:
      return null;
  }
}

/**
 * Send a sampling/createMessage request to the Host's LLM.
 */
async function requestSampling(prompt: string): Promise<string | null> {
  if (!mcpServer) return null;

  try {
    // Use the MCP server's request method to send sampling/createMessage
    // to the connected client. The client's LLM will process it.
    const result = await (mcpServer as any).createMessage({
      messages: [
        {
          role: "user" as const,
          content: { type: "text" as const, text: prompt },
        },
      ],
      maxTokens: 512,
    });

    // Extract text content from the response
    if (result?.content?.type === "text") {
      return result.content.text;
    }
    if (typeof result?.content === "string") {
      return result.content;
    }
    return null;
  } catch (e) {
    console.error(`[ReverseControl] createMessage failed:`, (e as Error).message);
    return null;
  }
}

/**
 * Parse the LLM's sampling response into a structured decision.
 */
function parseSamplingResponse(eventType: string, response: string): SamplingDecision | null {
  try {
    // Try to extract JSON from the response (LLM may wrap it in markdown)
    const jsonMatch = response.match(/\{[\s\S]*\}/);
    if (!jsonMatch) return null;

    const parsed = JSON.parse(jsonMatch[0]);
    const action = parsed.action as string;

    if (!action) return null;

    switch (action) {
      case "bid":
        return {
          action: "bid",
          params: {
            confidence: Math.min(1.0, Math.max(0.0, Number(parsed.confidence) || 0.7)),
            price: Number(parsed.price) || 0,
          },
        };

      case "reply":
        return {
          action: "reply",
          params: { message: String(parsed.message ?? "") },
        };

      case "confirm":
        return { action: "confirm", params: {} };

      case "decline":
        return { action: "decline", params: { reason: parsed.reason } };

      case "ignore":
        return { action: "ignore", params: { reason: parsed.reason } };

      case "custom":
        return {
          action: "custom",
          params: { tool: parsed.tool, params: parsed.params ?? {} },
        };

      default:
        return null;
    }
  } catch {
    return null;
  }
}

/**
 * Execute a decision made by the LLM via sampling.
 */
async function executeDecision(
  agentId: string,
  event: PushEvent,
  decision: SamplingDecision,
): Promise<void> {
  const taskId = event.task_id;
  const payload = event.payload as Record<string, unknown>;

  try {
    switch (decision.action) {
      case "bid": {
        const { confidence, price } = decision.params as { confidence: number; price: number };
        await net.submitBid(taskId, agentId, confidence, price);
        state.updateTask({
          task_id: taskId,
          role: "executor",
          status: "bidding",
          domains: (payload.domains as string[]) ?? [],
          description_summary: String(payload.description ?? "").slice(0, 100),
          created_at: new Date().toISOString(),
        });
        console.error(`[ReverseControl] auto-bid: task=${taskId} confidence=${confidence} price=${price}`);
        break;
      }

      case "reply": {
        const message = decision.params.message as string;
        const to = (payload.from as string) ?? "";
        if (to && message) {
          const s = state.getState();
          const sid = s.server_card?.server_id ?? "";
          await net.relayMessage({
            from: { network_id: "", server_id: sid, agent_id: agentId },
            to: { network_id: "", server_id: "", agent_id: to },
            content: message,
          });
          state.addMessage(agentId, {
            from: agentId,
            to,
            content: message,
            timestamp: Date.now(),
            direction: "out",
          });
          console.error(`[ReverseControl] auto-reply: to=${to} task=${taskId}`);
        }
        break;
      }

      case "confirm": {
        await net.confirmBudget(taskId, agentId, true);
        console.error(`[ReverseControl] auto-confirm budget: task=${taskId}`);
        break;
      }

      case "decline": {
        await net.confirmBudget(taskId, agentId, false);
        console.error(`[ReverseControl] auto-decline budget: task=${taskId}`);
        break;
      }

      case "custom": {
        // Custom actions are logged but not auto-executed for safety.
        // They're pushed as enhanced events for the agent to pick up.
        state.pushEvents([{
          ...event,
          payload: { ...payload, _rc_decision: decision.params },
          received_at: Date.now(),
        }]);
        console.error(`[ReverseControl] custom decision buffered: tool=${decision.params.tool}`);
        break;
      }

      case "ignore":
        console.error(`[ReverseControl] ignored: ${event.type} task=${taskId} reason=${decision.params.reason}`);
        break;
    }
  } catch (e) {
    console.error(`[ReverseControl] action execution failed:`, (e as Error).message);
    // On failure, ensure the event is still buffered so agent can handle manually
    state.pushEvents([event]);
  }
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

function handleViaNotification(agentId: string, event: PushEvent): boolean {
  if (!mcpServer) return false;

  try {
    markProcessed(event.msg_id);

    // Send a custom notification to the client
    (mcpServer as any).notification({
      method: "notifications/message",
      params: {
        level: event.type === "timeout" ? "warning" : "info",
        logger: "eacn3-reverse-control",
        data: {
          type: event.type,
          task_id: event.task_id,
          summary: buildNotificationSummary(event),
          payload: event.payload,
        },
      },
    });

    console.error(`[ReverseControl] notification sent: ${event.type} task=${event.task_id}`);
    return false; // still buffer — notification is supplementary
  } catch (e) {
    console.error(`[ReverseControl] notification failed:`, (e as Error).message);
    return false;
  }
}

function buildNotificationSummary(event: PushEvent): string {
  const payload = event.payload as Record<string, unknown>;
  switch (event.type) {
    case "awaiting_retrieval":
      return `Task ${event.task_id} has results ready for retrieval.`;
    case "timeout":
      return `Task ${event.task_id} has timed out.`;
    default:
      return `Event ${event.type} on task ${event.task_id}.`;
  }
}

// ---------------------------------------------------------------------------
// Auto-Actions
// ---------------------------------------------------------------------------

async function handleViaAutoAction(
  agentId: string,
  event: PushEvent,
  action?: string,
): Promise<boolean> {
  markProcessed(event.msg_id);

  switch (action) {
    case "report_and_close":
      // Auto-report timeout and update local state
      state.updateTaskStatus(event.task_id, "no_one");
      try {
        await net.reportEvent(agentId, "task_timeout");
      } catch { /* non-critical */ }
      console.error(`[ReverseControl] auto-action: reported timeout for task=${event.task_id}`);
      return true;

    default:
      return false;
  }
}

// ---------------------------------------------------------------------------
// Enhanced Tool Result Injection (Fallback)
// ---------------------------------------------------------------------------

/**
 * Queue a directive for injection into the next tool response.
 * Used when sampling is unavailable but the event needs agent attention.
 */
function queueDirective(agentId: string, event: PushEvent): void {
  const directive = buildDirectiveText(agentId, event);
  if (directive) {
    pendingDirectives.push({
      agentId,
      event,
      directive,
      createdAt: Date.now(),
    });
  }
}

function buildDirectiveText(agentId: string, event: PushEvent): string | null {
  const payload = event.payload as Record<string, unknown>;

  switch (event.type) {
    case "task_broadcast":
      return `[ACTION NEEDED] New task ${event.task_id} in domain [${((payload.domains as string[]) ?? []).join(", ")}] (budget: ${payload.budget ?? "?"}). Consider calling eacn3_submit_bid.`;

    case "direct_message":
      return `[ACTION NEEDED] Message from ${payload.from ?? "unknown"}: "${String(payload.content ?? "").slice(0, 200)}". Consider calling eacn3_send_message to reply.`;

    case "subtask_completed":
      return `[ACTION NEEDED] Subtask ${payload.subtask_id ?? "?"} completed for task ${event.task_id}. Results available. Consider calling eacn3_get_task_results.`;

    case "budget_confirmation":
      return `[ACTION NEEDED] Budget exceeded on task ${event.task_id}. Bid price: ${payload.price ?? "?"}. Call eacn3_confirm_budget to approve/reject.`;

    case "discussions_updated":
      return `[ACTION NEEDED] New discussion on task ${event.task_id}. Check eacn3_get_task and respond.`;

    default:
      return null;
  }
}

/**
 * Drain pending directives for a given agent (or all agents).
 * Called by tool result wrapper to inject into responses.
 *
 * Returns formatted text to append to tool results, or null if none.
 */
export function drainDirectives(agentId?: string): string | null {
  // Clean expired directives (older than 5 minutes)
  const now = Date.now();
  const cutoff = now - PROCESSED_EVENT_TTL_MS;
  while (pendingDirectives.length > 0 && pendingDirectives[0].createdAt < cutoff) {
    pendingDirectives.shift();
  }

  // Find directives for this agent
  const matching: typeof pendingDirectives = [];
  const remaining: typeof pendingDirectives = [];

  for (const d of pendingDirectives) {
    if (!agentId || d.agentId === agentId) {
      matching.push(d);
    } else {
      remaining.push(d);
    }
  }

  if (matching.length === 0) return null;

  // Remove matched directives
  pendingDirectives.length = 0;
  pendingDirectives.push(...remaining);

  const lines = matching.map((d, i) => `${i + 1}. ${d.directive}`);
  return `\n\n---\n[EACN3 PENDING EVENTS] ${matching.length} event(s) require your attention:\n${lines.join("\n")}`;
}

/**
 * Check if there are any pending directives (for deciding whether to inject).
 */
export function hasPendingDirectives(agentId?: string): boolean {
  if (!agentId) return pendingDirectives.length > 0;
  return pendingDirectives.some((d) => d.agentId === agentId);
}

// ---------------------------------------------------------------------------
// Rate Limiting & Dedup
// ---------------------------------------------------------------------------

function checkRateLimit(): boolean {
  const now = Date.now();
  // Remove timestamps outside the window
  while (samplingTimestamps.length > 0 && samplingTimestamps[0] < now - RATE_WINDOW_MS) {
    samplingTimestamps.shift();
  }
  return samplingTimestamps.length < MAX_SAMPLING_PER_MINUTE;
}

function recordSamplingCall(): void {
  samplingTimestamps.push(Date.now());
}

function isAlreadyProcessed(msgId: string): boolean {
  // Clean old entries
  const cutoff = Date.now() - PROCESSED_EVENT_TTL_MS;
  while (processedEvents.length > 0 && processedEvents[0].processedAt < cutoff) {
    processedEvents.shift();
  }
  return processedEvents.some((e) => e.msgId === msgId);
}

function markProcessed(msgId: string): void {
  if (msgId) {
    processedEvents.push({ msgId, processedAt: Date.now() });
  }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function timeout(ms: number): Promise<null> {
  return new Promise((resolve) => setTimeout(() => resolve(null), ms));
}

/**
 * Get current reverse control status for debugging.
 */
export function getStatus(): {
  samplingAvailable: boolean;
  configuredAgents: string[];
  pendingDirectiveCount: number;
  samplingCallsInWindow: number;
} {
  return {
    samplingAvailable,
    configuredAgents: Object.keys(configs),
    pendingDirectiveCount: pendingDirectives.length,
    samplingCallsInWindow: samplingTimestamps.length,
  };
}

/**
 * Force re-detection of client capabilities.
 * Useful after reconnection or capability negotiation.
 */
export function refreshCapabilities(): void {
  detectCapabilities();
}
