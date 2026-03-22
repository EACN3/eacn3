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

import { createServer, type Server, type IncomingMessage, type ServerResponse } from "node:http";
import { type PushEvent } from "./models.js";
import { getAgent, pushEvents } from "./state.js";

// ---------------------------------------------------------------------------
// Server state
// ---------------------------------------------------------------------------

let server: Server | null = null;
let serverPort: number = 0;

// ---------------------------------------------------------------------------
// Request handling
// ---------------------------------------------------------------------------

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

function respond(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

/**
 * Route: POST /agents/{agent_id}/events
 * Accepts a direct message and pushes it into the event queue.
 */
async function handleEventsPost(
  agentId: string,
  req: IncomingMessage,
  res: ServerResponse,
): Promise<void> {
  // Validate agent exists locally
  if (!getAgent(agentId)) {
    respond(res, 404, { error: `Agent ${agentId} not found on this server` });
    return;
  }

  // Parse body
  let body: Record<string, unknown>;
  try {
    const raw = await readBody(req);
    body = JSON.parse(raw);
  } catch {
    respond(res, 400, { error: "Invalid JSON body" });
    return;
  }

  // Validate required fields
  const from = body.from as string | undefined;
  const content = body.content;
  if (!from || content === undefined) {
    respond(res, 400, { error: "Missing required fields: from, content" });
    return;
  }

  // Push into event queue (same path as WebSocket messages)
  const event: PushEvent = {
    type: "direct_message",
    task_id: "",
    payload: { from, content, agent_id: agentId },
    received_at: Date.now(),
  };
  pushEvents([event]);

  respond(res, 200, { ok: true, agent_id: agentId });
}

/**
 * Main request handler — route dispatch.
 */
async function handleRequest(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = req.url ?? "";
  const method = req.method ?? "";

  // Route: POST /agents/{agent_id}/events
  const match = url.match(/^\/agents\/([^/]+)\/events\/?$/);
  if (match && method === "POST") {
    await handleEventsPost(match[1], req, res);
    return;
  }

  // Health check
  if (url === "/health" && method === "GET") {
    respond(res, 200, { status: "ok" });
    return;
  }

  respond(res, 404, { error: "Not found" });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Start the A2A HTTP server on the given port.
 * Returns the actual port (useful if 0 was passed for auto-assign).
 */
export function startServer(port: number): Promise<number> {
  return new Promise((resolve, reject) => {
    if (server) {
      resolve(serverPort);
      return;
    }

    server = createServer((req, res) => {
      handleRequest(req, res).catch(() => {
        respond(res, 500, { error: "Internal server error" });
      });
    });

    server.listen(port, () => {
      const addr = server!.address();
      serverPort = typeof addr === "object" && addr ? addr.port : port;
      resolve(serverPort);
    });

    server.on("error", (err) => {
      server = null;
      reject(err);
    });
  });
}

/**
 * Stop the A2A HTTP server.
 */
export function stopServer(): Promise<void> {
  return new Promise((resolve) => {
    if (!server) {
      resolve();
      return;
    }
    server.close(() => {
      server = null;
      serverPort = 0;
      resolve();
    });
  });
}

/**
 * Get the port the server is listening on (0 if not started).
 */
export function getServerPort(): number {
  return serverPort;
}

/**
 * Check if the A2A server is running.
 */
export function isRunning(): boolean {
  return server !== null;
}
