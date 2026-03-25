"""Shared fixtures for plugin × network integration tests.

Architecture:
  pytest  ─→  uvicorn (random port)   ← network HTTP server (in-process)
          ─→  node dist/server.js      ← plugin MCP server (subprocess)
          ─→  MCP JSON-RPC over stdio  ← drives plugin tool calls

The plugin's network-client.ts makes real HTTP calls to the live network
server, exercising the full stack end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI

from eacn3.network.app import Network
from eacn3.network.config import NetworkConfig
from eacn3.network.db import Database
from eacn3.network.api.routes import router as net_router, set_network, set_offline_store
from eacn3.network.api.discovery_routes import discovery_router, set_discovery_network
from eacn3.network.offline_store import OfflineStore


# ── Helpers ──────────────────────────────────────────────────────────

def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Network live server ─────────────────────────────────────────────

@pytest.fixture
async def network():
    """Bare Network with in-memory DB."""
    db = Database()
    await db.connect()
    net = Network(db=db, config=NetworkConfig())
    yield net
    await db.close()


@pytest.fixture
async def funded_network(network):
    """Network with pre-funded accounts + DHT entries.

    Accounts: user1=10000, user2=5000
    DHT: coding→[a1,a2,a3], design→[a4], research→[a5]
    Reputation: a1=0.8, a2=0.75, a3=0.7, a4=0.65, a5=0.6
    """
    net = network
    net.escrow.get_or_create_account("user1", 10_000.0)
    net.escrow.get_or_create_account("user2", 5_000.0)
    for aid in ("a1", "a2", "a3"):
        await net.dht.announce("coding", aid)
    await net.dht.announce("design", "a4")
    await net.dht.announce("research", "a5")
    net.reputation._scores.update({
        "a1": 0.8, "a2": 0.75, "a3": 0.7, "a4": 0.65, "a5": 0.6,
    })
    return net


@pytest.fixture
async def live_server(funded_network):
    """Start a real uvicorn server on a random port. Yields base URL."""
    from eacn3.network.db import Database

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(net_router)
    app.include_router(discovery_router)
    set_network(funded_network)
    set_discovery_network(funded_network)

    # Create an in-memory offline store for the test server
    offline_store = OfflineStore(db=funded_network.db)
    set_offline_store(offline_store)

    # Wire push handler → queue delivery (same as production app.py)
    async def queue_push_handler(event):
        for agent_id in event.recipients:
            await offline_store.store(
                msg_id=event.msg_id,
                agent_id=agent_id,
                event_type=event.type.value,
                task_id=event.task_id,
                payload=event.payload,
            )

    funded_network.push.set_handler(queue_push_handler)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    task = asyncio.create_task(server.serve())
    # Wait for server to start
    for _ in range(50):
        await asyncio.sleep(0.1)
        if server.started:
            break
    assert server.started, "uvicorn failed to start"

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    await task


# ── MCP Client (drives plugin subprocess via stdio JSON-RPC) ────────

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugin"
PLUGIN_SERVER = PLUGIN_DIR / "dist" / "server.js"


class McpClient:
    """Minimal MCP client that talks JSON-RPC over stdio to the plugin."""

    def __init__(self, proc: subprocess.Popen, state_dir: str):
        self.proc = proc
        self.state_dir = state_dir
        self._id = 0
        self._buf = b""

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, msg: dict) -> None:
        """Send a JSON-RPC message (newline-delimited)."""
        data = json.dumps(msg) + "\n"
        self.proc.stdin.write(data.encode())
        self.proc.stdin.flush()

    def _recv(self, timeout: float = 15.0) -> dict:
        """Read one JSON-RPC response. Skips notifications."""
        import select
        import time

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("MCP response timeout")

            # Try to extract a complete line from buffer
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                # Skip notifications (no "id")
                if "id" in msg:
                    return msg
                continue

            # Read more data
            ready, _, _ = select.select([self.proc.stdout], [], [], min(remaining, 1.0))
            if ready:
                chunk = self.proc.stdout.read1(4096)
                if not chunk:
                    raise ConnectionError("Plugin process closed stdout")
                self._buf += chunk

    async def _send_recv(self, msg: dict, timeout: float = 15.0) -> dict:
        """Send and receive in a thread to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()

        def _sync():
            self._send(msg)
            return self._recv(timeout)

        return await loop.run_in_executor(None, _sync)

    async def initialize(self) -> dict:
        """MCP initialization handshake."""
        resp = await self._send_recv({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest-integration", "version": "1.0"},
            },
        })
        # Send initialized notification (no response expected)
        self._send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        return resp

    async def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call an MCP tool. Returns the result or raises on error."""
        resp = await self._send_recv({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
            },
        })
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return resp["result"]

    async def call_tool_parsed(self, name: str, arguments: dict | None = None) -> Any:
        """Call tool and parse the JSON text content.

        If the tool returns a JSON-parseable string, parse it.
        If the MCP call itself errored, return {"error": error_message}.
        """
        resp = await self._send_recv({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
            },
        })
        if "error" in resp:
            return {"error": resp["error"].get("message", str(resp["error"]))}
        result = resp["result"]
        text = result["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def close(self):
        """Terminate the plugin process."""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


@pytest.fixture
async def mcp(live_server):
    """Start plugin MCP server subprocess, yield McpClient.

    The plugin uses a temporary state directory so tests don't pollute
    each other or the user's real ~/.eacn3 state.
    """
    state_dir = tempfile.mkdtemp(prefix="eacn3-test-")
    env = {
        **os.environ,
        "EACN3_STATE_DIR": state_dir,
        "EACN3_NETWORK_URL": live_server,
    }

    proc = subprocess.Popen(
        ["node", str(PLUGIN_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(PLUGIN_DIR),
    )

    client = McpClient(proc, state_dir)

    try:
        await client.initialize()
        # Connect plugin to the live network server
        connect_result = await client.call_tool_parsed("eacn3_connect", {
            "network_endpoint": live_server,
        })
        assert connect_result.get("connected") is True, (
            f"eacn3_connect failed: {connect_result}"
        )
        yield client
    finally:
        client.close()
        shutil.rmtree(state_dir, ignore_errors=True)


# ── Direct HTTP client (for verifying network state from test side) ──

@pytest.fixture
async def http(live_server):
    """httpx AsyncClient pointed at the live server (for verification)."""
    import httpx
    async with httpx.AsyncClient(base_url=live_server) as c:
        yield c


# ── Test helper ──────────────────────────────────────────────────────

def seed_reputation(funded_network, agent_id: str, score: float = 0.8) -> None:
    """Pre-seed reputation so bids pass ability gate (confidence × reputation ≥ 0.5)."""
    funded_network.reputation._scores[agent_id] = score


def is_error(result: dict) -> bool:
    """Check if a call_tool_parsed result is an error.

    Plugin errors come in two forms:
    - {"error": "..."} — JSON-RPC level error or plugin-caught error
    - {"raw": "POST ... → 4xx: ..."} — HTTP error returned as non-JSON text
    """
    if "error" in result:
        return True
    raw = result.get("raw", "")
    if raw and ("→ 4" in raw or "→ 5" in raw):
        return True
    return False
