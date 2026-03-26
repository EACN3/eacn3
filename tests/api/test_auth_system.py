"""Authentication system tests.

Tests the auth module and its integration with API routes:
- Admin key enforcement
- Agent token lifecycle (register → use → revoke)
- Peer HMAC signature
- Auth bypass in dev mode (no keys configured)
"""

import pytest
from eacn.network.auth import (
    generate_token,
    register_agent_token, revoke_agent_token, validate_agent_token,
    register_server_token, revoke_server_token, validate_server_token,
    set_admin_key, get_admin_key, validate_admin_key,
    set_peer_secret, compute_peer_signature, validate_peer_signature,
    _agent_tokens, _server_tokens,
)


@pytest.fixture(autouse=True)
def clean_auth_state():
    """Reset auth state between tests."""
    _agent_tokens.clear()
    _server_tokens.clear()
    set_admin_key(None)
    set_peer_secret(None)
    yield
    _agent_tokens.clear()
    _server_tokens.clear()
    set_admin_key(None)
    set_peer_secret(None)


class TestTokenGeneration:
    def test_token_not_empty(self):
        token = generate_token()
        assert len(token) > 20

    def test_tokens_unique(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100  # All unique


class TestAgentTokens:
    def test_register_and_validate(self):
        token = register_agent_token("agent-1")
        assert validate_agent_token("agent-1", token)

    def test_wrong_token_fails(self):
        register_agent_token("agent-2")
        assert not validate_agent_token("agent-2", "wrong-token")

    def test_unknown_agent_fails(self):
        assert not validate_agent_token("ghost", "any-token")

    def test_revoke_invalidates(self):
        token = register_agent_token("agent-3")
        assert validate_agent_token("agent-3", token)
        revoke_agent_token("agent-3")
        assert not validate_agent_token("agent-3", token)

    def test_revoke_nonexistent_no_crash(self):
        revoke_agent_token("ghost")  # Should not raise


class TestServerTokens:
    def test_register_and_validate(self):
        token = register_server_token("srv-1")
        assert validate_server_token("srv-1", token)

    def test_wrong_token_fails(self):
        register_server_token("srv-2")
        assert not validate_server_token("srv-2", "wrong")

    def test_revoke(self):
        token = register_server_token("srv-3")
        revoke_server_token("srv-3")
        assert not validate_server_token("srv-3", token)


class TestAdminKey:
    def test_no_key_allows_all(self):
        assert validate_admin_key("anything")
        assert validate_admin_key("")

    def test_set_key_enforces(self):
        set_admin_key("my-secret-key")
        assert validate_admin_key("my-secret-key")
        assert not validate_admin_key("wrong-key")
        assert not validate_admin_key("")

    def test_get_admin_key(self):
        set_admin_key("test-key")
        assert get_admin_key() == "test-key"


class TestPeerHMAC:
    def test_no_secret_allows_all(self):
        assert validate_peer_signature(b"body", "123", "any")

    def test_valid_signature(self):
        import time
        set_peer_secret("shared-secret")
        ts = str(int(time.time()))
        body = b'{"task_id": "t1"}'
        sig = compute_peer_signature(body, ts)
        assert validate_peer_signature(body, ts, sig)

    def test_invalid_signature_rejected(self):
        import time
        set_peer_secret("shared-secret")
        ts = str(int(time.time()))
        assert not validate_peer_signature(b"body", ts, "wrong-sig")

    def test_expired_timestamp_rejected(self):
        set_peer_secret("shared-secret")
        old_ts = "1000000000"  # Year 2001
        body = b"test"
        sig = compute_peer_signature(body, old_ts)
        assert not validate_peer_signature(body, old_ts, sig)

    def test_tampered_body_rejected(self):
        import time
        set_peer_secret("shared-secret")
        ts = str(int(time.time()))
        body = b'original'
        sig = compute_peer_signature(body, ts)
        assert not validate_peer_signature(b'tampered', ts, sig)
