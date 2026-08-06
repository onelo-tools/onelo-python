"""Tests for monitor.init / close / flush — the public entry points."""
from __future__ import annotations

import os
import sys
import threading

import httpx
import pytest

from onelo import monitor
from onelo.monitor import _capture, _excepthook
from onelo.monitor._init import _get_active_transport
from onelo.monitor._scope import get_global_scope, reset_for_tests


@pytest.fixture(autouse=True)
def _clean() -> None:
    monitor.close()
    reset_for_tests()
    yield
    monitor.close()
    reset_for_tests()


class _Mock:
    """Minimal httpx mock transport that records calls."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.calls.append(request)
        return httpx.Response(204, request=request)


def _httpx_transport(mock: _Mock) -> httpx.AsyncBaseTransport:
    return httpx.MockTransport(mock.handle_async_request)


# ── happy path ──────────────────────────────────────────────────────────────


def test_init_starts_transport_and_registers_sink() -> None:
    mock = _Mock()
    monitor.init(
        publishable_key="onelo_pk_test_key",
        api_url="https://api.example.com",
        install_excepthook=False,
        http_transport=_httpx_transport(mock),
    )
    assert monitor.is_initialised()
    assert _capture.get_transport() is not None
    # Capture a message and flush — the mock transport should see exactly one POST.
    monitor.capture_message("hi", level="error")
    monitor.flush(timeout=2.0)
    assert len(mock.calls) == 1
    assert mock.calls[0].url.path == "/api/sdk/monitor/events/batch"


def test_init_pulls_config_from_existing_onelo_client() -> None:
    """When passed an Onelo client, init must use its publishable_key + api_url
    so auth / features / monitor never disagree on which backend to talk to.
    """
    from onelo._client import Onelo

    onelo = Onelo(
        publishable_key="onelo_pk_test_from_client",
        api_url="https://different.example.com",
    )
    try:
        mock = _Mock()
        monitor.init(
            onelo=onelo,
            install_excepthook=False,
            http_transport=_httpx_transport(mock),
        )
        monitor.capture_message("hello", level="error")
        monitor.flush(timeout=2.0)

        assert len(mock.calls) == 1
        # URL came from the Onelo client, not from the kw default.
        assert "different.example.com" in str(mock.calls[0].url)
        body = mock.calls[0].read()
        assert b"onelo_pk_test_from_client" in body
    finally:
        onelo.close()


def test_init_requires_publishable_key_when_no_client() -> None:
    with pytest.raises(ValueError, match="publishable_key"):
        monitor.init(install_excepthook=False)


def test_double_init_replaces_previous_transport() -> None:
    """Calling init twice should warn and close the first transport so we
    don't leak threads."""
    mock1 = _Mock()
    monitor.init(
        publishable_key="k1",
        install_excepthook=False,
        http_transport=_httpx_transport(mock1),
    )
    first = _get_active_transport()
    assert first is not None

    mock2 = _Mock()
    monitor.init(
        publishable_key="k2",
        install_excepthook=False,
        http_transport=_httpx_transport(mock2),
    )
    second = _get_active_transport()
    assert second is not None
    assert first is not second


# ── global-scope tags from init ────────────────────────────────────────────


def test_init_populates_global_scope_with_release_and_environment() -> None:
    monitor.init(
        publishable_key="k",
        release="abc123",
        environment="staging",
        server_name="pod-7",
        install_excepthook=False,
        http_transport=_httpx_transport(_Mock()),
    )
    g = get_global_scope()
    assert g.tags["release"] == "abc123"
    assert g.tags["environment"] == "staging"
    assert g.tags["server_name"] == "pod-7"


def test_init_falls_back_to_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONELO_RELEASE", "from-env")
    monkeypatch.setenv("ONELO_ENVIRONMENT", "production")
    monitor.init(
        publishable_key="k",
        install_excepthook=False,
        http_transport=_httpx_transport(_Mock()),
    )
    g = get_global_scope()
    assert g.tags["release"] == "from-env"
    assert g.tags["environment"] == "production"


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_close_unsets_sink_and_uninstalls_excepthook() -> None:
    monitor.init(
        publishable_key="k",
        install_excepthook=True,
        http_transport=_httpx_transport(_Mock()),
    )
    assert _capture.get_transport() is not None

    monitor.close()
    assert _capture.get_transport() is None
    assert not monitor.is_initialised()


def test_close_is_idempotent() -> None:
    monitor.close()
    monitor.close()  # must not raise
    assert not monitor.is_initialised()


def test_flush_no_op_before_init() -> None:
    # Should not raise even though no transport exists.
    monitor.flush(timeout=0.1)


# ── excepthook ──────────────────────────────────────────────────────────────


def test_install_excepthook_replaces_sys_excepthook() -> None:
    original = sys.excepthook
    monitor.init(
        publishable_key="k",
        install_excepthook=True,
        http_transport=_httpx_transport(_Mock()),
    )
    try:
        assert sys.excepthook is not original
    finally:
        monitor.close()
    # Restored after close.
    assert sys.excepthook is original


def test_excepthook_skips_keyboard_interrupt() -> None:
    """KeyboardInterrupt must NOT trigger an event — that's deliberate user
    action, not an error to report.

    ``init()`` itself now auto-emits one unconditional ``session_opened``
    baseline event (see ``monitor.init`` / ``_capture._emit_session_opened``),
    so the mock DOES see one POST containing that event — the assertion below
    checks no *additional* (KeyboardInterrupt-triggered) event rode along.
    """
    import json

    mock = _Mock()
    monitor.init(
        publishable_key="k",
        install_excepthook=True,
        http_transport=_httpx_transport(mock),
    )
    try:
        # Simulate the interpreter calling our excepthook.
        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt as e:
            sys.excepthook(type(e), e, e.__traceback__)
        monitor.flush(timeout=0.5)
    finally:
        monitor.close()
    assert len(mock.calls) == 1
    body = json.loads(mock.calls[0].content)
    events = body["events"] if isinstance(body, dict) else body
    assert len(events) == 1
    assert events[0]["featureName"] == "session_opened"


def test_excepthook_captures_regular_exception() -> None:
    mock = _Mock()
    monitor.init(
        publishable_key="k",
        install_excepthook=True,
        http_transport=_httpx_transport(mock),
    )
    try:
        try:
            raise ValueError("uncaught")
        except ValueError as e:
            sys.excepthook(type(e), e, e.__traceback__)
        monitor.flush(timeout=2.0)
    finally:
        monitor.close()
    assert len(mock.calls) == 1
    body = mock.calls[0].read()
    assert b'"featureName":"uncaught"' in body
    assert b"ValueError" in body


# ── thread excepthook ──────────────────────────────────────────────────────


def test_thread_excepthook_captures_thread_exceptions() -> None:
    mock = _Mock()
    monitor.init(
        publishable_key="k",
        install_excepthook=True,
        http_transport=_httpx_transport(mock),
    )
    try:
        def boom() -> None:
            raise RuntimeError("thread boom")

        thread = threading.Thread(target=boom, name="test-boom")
        thread.start()
        thread.join()

        monitor.flush(timeout=2.0)
    finally:
        monitor.close()

    # threading.excepthook fires on the joined thread's exception.
    assert any(b"thread boom" in c.read() for c in mock.calls)


def test_deploy_context_stamped_on_backend_read_meta_paths() -> None:
    """release/environment must land on meta.app.version + meta.environment —
    the exact paths the backend aggregates on (M2). The scope-tag copies under
    meta.tags.* are ignored by the dashboard's Release/Environment filters."""
    import json

    mock = _Mock()
    monitor.init(
        publishable_key="onelo_pk_test_key",
        api_url="https://api.example.com",
        environment="production",
        release="abc123",
        install_excepthook=False,
        http_transport=_httpx_transport(mock),
    )
    monitor.capture_message("boom", level="error")
    monitor.flush(timeout=2.0)

    assert len(mock.calls) == 1
    body = json.loads(mock.calls[0].content)
    meta = body["events"][0]["meta"]
    assert meta["environment"] == "production"          # backend _dim(meta,"environment")
    assert meta["app"]["version"] == "abc123"           # backend _release_dim -> meta.app.version


def test_deploy_context_reset_on_close() -> None:
    """close() clears deploy context so a later capture without init doesn't
    inherit a previous process config."""
    from onelo.monitor._types import MonitorEvent

    monitor.init(
        publishable_key="onelo_pk_test_key",
        api_url="https://api.example.com",
        environment="staging",
        release="v9",
        install_excepthook=False,
        http_transport=_httpx_transport(_Mock()),
    )
    monitor.close()

    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    monitor.capture_message("after-close", level="error")
    _capture.set_transport(None)

    assert sink[0].meta.get("environment") is None
    assert "app" not in sink[0].meta


# ── M4: client-side sampling ────────────────────────────────────────────────

def test_sampling_drops_errors_at_rate_zero_keeps_successes() -> None:
    from onelo.monitor._types import MonitorEvent

    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    _capture.set_sample_rates(0.0, 1.0)  # drop all errors, keep all successes
    try:
        for _ in range(25):
            _capture.capture_event(MonitorEvent(feature_name="err", ok=False))
        assert sink == []  # every error sampled out
        _capture.capture_event(MonitorEvent(feature_name="ok", ok=True))
        assert len(sink) == 1  # success unaffected by error sample_rate
    finally:
        _capture.set_transport(None)
        _capture.set_sample_rates(1.0, 1.0)


def test_default_sample_rate_keeps_everything() -> None:
    from onelo.monitor._types import MonitorEvent

    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    try:
        for _ in range(10):
            _capture.capture_event(MonitorEvent(feature_name="e", ok=False))
        assert len(sink) == 10  # default 1.0 → no sampling
    finally:
        _capture.set_transport(None)


@pytest.mark.parametrize("kwargs", [{"sample_rate": 1.5}, {"success_sample_rate": -0.1}])
def test_invalid_sample_rate_raises(kwargs) -> None:
    with pytest.raises(ValueError, match="must be between 0.0 and 1.0"):
        monitor.init(
            publishable_key="onelo_pk_test_key",
            api_url="https://api.example.com",
            install_excepthook=False,
            **kwargs,
        )
    # Fail-fast: nothing was configured.
    assert not monitor.is_initialised()
