/**
 * A2A HTTP server — receives direct messages from remote agents.
 *
 * Listens on a configurable port and accepts POST /agents/{agent_id}/events.
 * Incoming messages are validated and pushed into the shared event buffer,
 * identical to messages arriving via WebSocket.
 *
 * Lifecycle: started per-agent when a real url/port is provided at registration,
 * stopped when the agent is unregistered.
 */
/**
 * Start the A2A HTTP server on the given port.
 * Returns the actual port (useful if 0 was passed for auto-assign).
 */
export declare function startServer(port: number): Promise<number>;
/**
 * Stop the A2A HTTP server.
 */
export declare function stopServer(): Promise<void>;
/**
 * Get the port the server is listening on (0 if not started).
 */
export declare function getServerPort(): number;
/**
 * Check if the A2A server is running.
 */
export declare function isRunning(): boolean;
