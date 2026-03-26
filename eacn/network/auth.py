"""Authentication middleware and helpers.

Provides three layers:
1. Admin API key — protects /admin/* endpoints (#24)
2. Peer HMAC — protects /peer/* inter-node routes (#23)
3. Agent token — associates API calls with an agent identity (#25, #78)

Configuration via environment variables or NetworkConfig:
- EACN3_ADMIN_KEY: Admin API key (required for admin endpoints)
- EACN3_PEER_SECRET: Shared secret for peer HMAC (required for cluster mode)
- Agent tokens are issued on registration and validated per-request.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# ── Token store ──────────────────────────────────────────────────────

# agent_id → token
_agent_tokens: dict[str, str] = {}
# server_id → token
_server_tokens: dict[str, str] = {}


def generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def register_agent_token(agent_id: str) -> str:
    """Issue a token for an agent. Returns the token."""
    token = generate_token()
    _agent_tokens[agent_id] = token
    return token


def revoke_agent_token(agent_id: str) -> None:
    _agent_tokens.pop(agent_id, None)


def register_server_token(server_id: str) -> str:
    token = generate_token()
    _server_tokens[server_id] = token
    return token


def revoke_server_token(server_id: str) -> None:
    _server_tokens.pop(server_id, None)


def validate_agent_token(agent_id: str, token: str) -> bool:
    expected = _agent_tokens.get(agent_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


def validate_server_token(server_id: str, token: str) -> bool:
    expected = _server_tokens.get(server_id)
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


# ── Admin key ────────────────────────────────────────────────────────

_admin_key: str | None = None


def set_admin_key(key: str | None) -> None:
    global _admin_key
    _admin_key = key


def get_admin_key() -> str | None:
    return _admin_key or os.environ.get("EACN3_ADMIN_KEY")


def validate_admin_key(provided: str) -> bool:
    expected = get_admin_key()
    if not expected:
        return True  # No key configured = no protection (dev mode)
    return hmac.compare_digest(expected, provided)


# ── Peer HMAC ────────────────────────────────────────────────────────

_peer_secret: str | None = None


def set_peer_secret(secret: str | None) -> None:
    global _peer_secret
    _peer_secret = secret


def get_peer_secret() -> str | None:
    return _peer_secret or os.environ.get("EACN3_PEER_SECRET")


def compute_peer_signature(body: bytes, timestamp: str) -> str:
    """Compute HMAC-SHA256 signature for peer requests."""
    secret = get_peer_secret()
    if not secret:
        return ""
    msg = timestamp.encode() + b":" + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def validate_peer_signature(body: bytes, timestamp: str, signature: str) -> bool:
    secret = get_peer_secret()
    if not secret:
        return True  # No secret configured = no protection (standalone mode)
    expected = compute_peer_signature(body, timestamp)
    if not expected:
        return True
    # Check timestamp freshness (within 5 minutes)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, signature)


# ── FastAPI dependency helpers ───────────────────────────────────────

async def require_admin(request: Request) -> None:
    """FastAPI dependency: require admin API key (#24, #43)."""
    key = get_admin_key()
    if not key:
        return  # No key configured = dev mode, allow all
    provided = request.headers.get("X-Admin-Key", "")
    if not validate_admin_key(provided):
        raise HTTPException(403, "Invalid or missing admin API key")


async def require_peer_auth(request: Request) -> None:
    """FastAPI dependency: require peer HMAC signature (#23)."""
    secret = get_peer_secret()
    if not secret:
        return  # No secret configured = standalone mode
    signature = request.headers.get("X-Peer-Signature", "")
    timestamp = request.headers.get("X-Peer-Timestamp", "")
    body = await request.body()
    if not validate_peer_signature(body, timestamp, signature):
        raise HTTPException(403, "Invalid peer signature")


def extract_agent_token(request: Request) -> str | None:
    """Extract agent token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


async def require_agent_auth(request: Request, agent_id: str) -> None:
    """Validate that the request is authorized for the given agent_id (#25, #78).

    When no tokens are configured (dev mode), this is a no-op.
    """
    if not _agent_tokens:
        return  # No tokens issued yet = dev mode
    token = extract_agent_token(request)
    if not token:
        raise HTTPException(401, "Missing Authorization header")
    if not validate_agent_token(agent_id, token):
        raise HTTPException(403, f"Not authorized for agent {agent_id}")
