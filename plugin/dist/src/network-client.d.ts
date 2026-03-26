/**
 * HTTP client for EACN3 network endpoints (28 APIs).
 *
 * Each method maps 1:1 to a network-api.md endpoint.
 * server_id is injected from local state — callers don't need to pass it.
 */
import { type ServerCard, type AgentCard, type Task, type ReputationScore, type RegisterServerResponse, type RegisterAgentResponse, type BidResponse, type DiscoverResponse, type TaskResultsResponse, type BalanceResponse, type DepositResponse, type ClusterStatus, type HealthResponse, type InviteAgentResponse, type TaskLevel } from "./models.js";
/**
 * Probe a network endpoint for health. Uses a short timeout so it can be
 * used for fast fail-over. If `endpoint` is omitted, probes the current
 * configured endpoint.
 */
export declare function checkHealth(endpoint?: string): Promise<HealthResponse>;
/**
 * Get cluster topology: members, seed nodes, online count.
 */
export declare function getClusterStatus(endpoint?: string): Promise<ClusterStatus>;
/**
 * Try to find a healthy endpoint. Probes the primary endpoint first, then
 * falls back to seed nodes discovered from cluster status.
 * Returns the first reachable endpoint URL.
 */
export declare function findHealthyEndpoint(primary: string, seeds?: string[]): Promise<string>;
export declare function registerServer(version: string, endpoint: string, owner: string): Promise<RegisterServerResponse>;
export declare function getServer(sid: string): Promise<ServerCard>;
export declare function heartbeat(): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function unregisterServer(): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function registerAgent(agent: Omit<AgentCard, "network_id">): Promise<RegisterAgentResponse>;
export declare function getAgentInfo(agentId: string): Promise<AgentCard>;
export declare function updateAgent(agentId: string, updates: Partial<Pick<AgentCard, "name" | "domains" | "skills" | "url" | "description">>): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function unregisterAgent(agentId: string): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function discoverAgents(domain: string, requesterId?: string): Promise<DiscoverResponse>;
export declare function listAgentsRemote(opts: {
    domain?: string;
    server_id?: string;
    limit?: number;
    offset?: number;
}): Promise<AgentCard[]>;
export declare function createTask(task: {
    task_id: string;
    initiator_id: string;
    content: {
        description: string;
        expected_output?: {
            type: string;
            description: string;
        };
    };
    domains?: string[];
    budget: number;
    deadline?: string;
    max_concurrent_bidders?: number;
    max_depth?: number;
    human_contact?: {
        allowed: boolean;
        contact_id?: string;
        timeout_s?: number;
    };
    level?: TaskLevel;
    invited_agent_ids?: string[];
}): Promise<Task>;
export declare function getOpenTasks(opts?: {
    domains?: string;
    limit?: number;
    offset?: number;
}): Promise<Task[]>;
export declare function getTask(taskId: string): Promise<Task>;
export declare function getTaskStatus(taskId: string, agentId: string): Promise<Task>;
export declare function listTasks(opts?: {
    status?: string;
    initiator_id?: string;
    limit?: number;
    offset?: number;
}): Promise<Task[]>;
export declare function getTaskResults(taskId: string, initiatorId: string): Promise<TaskResultsResponse>;
export declare function selectResult(taskId: string, initiatorId: string, agentId: string, closeTask?: boolean): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function closeTask(taskId: string, initiatorId: string): Promise<Task>;
export declare function updateDeadline(taskId: string, initiatorId: string, deadline: string): Promise<Task>;
export declare function updateDiscussions(taskId: string, initiatorId: string, message: string): Promise<Task>;
export declare function confirmBudget(taskId: string, initiatorId: string, approved: boolean, newBudget?: number): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function submitBid(taskId: string, agentId: string, confidence: number, price: number): Promise<BidResponse>;
export declare function submitResult(taskId: string, agentId: string, content: Record<string, unknown>): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function rejectTask(taskId: string, agentId: string, reason?: string): Promise<{
    ok: boolean;
    message: string;
}>;
export declare function createSubtask(parentTaskId: string, initiatorId: string, content: {
    description: string;
}, domains: string[], budget: number, deadline?: string, level?: string): Promise<Task>;
export declare function reportEvent(agentId: string, eventType: string): Promise<ReputationScore>;
export declare function getReputation(agentId: string): Promise<ReputationScore>;
export declare function getBalance(agentId: string): Promise<BalanceResponse>;
export declare function deposit(agentId: string, amount: number): Promise<DepositResponse>;
export declare function inviteAgent(taskId: string, initiatorId: string, agentId: string): Promise<InviteAgentResponse>;
export interface RelayMessagePayload {
    to: {
        network_id: string;
        server_id: string;
        agent_id: string;
    };
    from: {
        network_id: string;
        server_id: string;
        agent_id: string;
    };
    content: unknown;
}
/**
 * Send a direct message via Network relay.
 * The Network node routes by three-layer addressing and delivers via WebSocket.
 */
export declare function relayMessage(msg: RelayMessagePayload): Promise<{
    ok: boolean;
    delivered: number;
}>;
