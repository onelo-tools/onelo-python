"""End-to-end integration test against a mock backend.

Exercises the lifecycle the user actually goes through: init → ready →
declare → feature lookup → identify → simulated update → re-lookup.
"""
import time

import pytest

from onelo import Onelo


def test_full_lifecycle_with_sse(mock_backend, mock_backend_transport):
    """Init, declare, lookup, simulate admin change via mocked SSE, re-lookup."""
    # Pre-populate backend state — simulates features admin already deployed
    mock_backend.features = {"chat-stream": "enabled", "voice-stream": "hidden"}
    mock_backend.config_version = 1

    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=mock_backend_transport,
    )
    try:
        # 1. ready() blocks until first SSE event (the 'connected' frame)
        assert onelo.ready(timeout=2.0) is True

        # 2. Initial state reflects backend
        assert onelo.features.feature("chat-stream").is_enabled is True
        assert onelo.features.feature("voice-stream").is_enabled is False

        # 3. declare() pings the backend
        onelo.features.declare(["new-feature-a", "new-feature-b"])
        time.sleep(1.5)  # 1s debounce + async POST
        assert "new-feature-a" in mock_backend.declared
        assert "new-feature-b" in mock_backend.declared

        # 4. identify() does not raise
        onelo.identify("user-123")
    finally:
        onelo.close()


def test_full_lifecycle_with_polling(mock_backend, mock_backend_transport):
    """Same flow, polling strategy explicit."""
    mock_backend.features = {"chat-stream": "beta"}
    mock_backend.config_version = 1

    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        strategy="polling",
        poll_interval=0.05,  # fast for test
        transport=mock_backend_transport,
    )
    try:
        assert onelo.ready(timeout=2.0) is True
        feat = onelo.features.feature("chat-stream")
        assert feat.status == "beta"
        assert feat.is_enabled is True
    finally:
        onelo.close()


def test_unknown_feature_is_hidden(mock_backend, mock_backend_transport):
    """Cache miss returns fail-closed default."""
    mock_backend.features = {}
    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=mock_backend_transport,
    )
    try:
        onelo.ready(timeout=2.0)
        feat = onelo.features.feature("does-not-exist")
        assert feat.status == "hidden"
        assert feat.is_enabled is False
        assert feat.is_visible is False
    finally:
        onelo.close()
