"""Tests for the killer feature: feature-flag correlation on every event."""
from __future__ import annotations

import httpx
import pytest

from onelo import Onelo, monitor
from onelo.monitor import _capture
from onelo.monitor._scope import reset_for_tests
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    _capture.set_flag_provider(None)
    reset_for_tests()
    yield sink
    _capture.set_transport(None)
    _capture.set_flag_provider(None)
    reset_for_tests()


def test_no_flags_attached_when_no_provider(captured: list[MonitorEvent]) -> None:
    monitor.capture_message("plain", level="error")
    assert "flags" not in captured[0].meta


def test_flag_provider_attaches_snapshot_to_event(captured: list[MonitorEvent]) -> None:
    _capture.set_flag_provider(lambda: {"checkout-v2": "enabled", "old-flow": "hidden"})
    monitor.capture_message("with flags", level="error")
    assert captured[0].meta["flags"] == {"checkout-v2": "enabled", "old-flow": "hidden"}


def test_flag_provider_failure_does_not_block_event(captured: list[MonitorEvent]) -> None:
    """A misbehaving provider must not crash capture — events still ship,
    just without the flags context."""
    def boom() -> dict[str, str]:
        raise RuntimeError("provider down")

    _capture.set_flag_provider(boom)
    monitor.capture_message("still arrives", level="error")
    assert len(captured) == 1
    assert "flags" not in captured[0].meta


def test_init_with_onelo_client_auto_wires_flag_provider() -> None:
    """The killer feature: pass an Onelo client to monitor.init() and
    every captured event automatically carries the active flag set."""
    sink: list[MonitorEvent] = []

    def mock_handle(req: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    onelo = Onelo(
        publishable_key="onelo_pk_test_x",
        api_url="https://example.com",
    )
    # Seed the cache so the snapshot is non-empty.
    onelo._cache.replace_all({"a": "enabled", "b": "beta"}, version=1)

    try:
        # Use a custom transport so the test never touches the network.
        _capture.set_transport(sink.append)
        monitor.init(
            onelo=onelo,
            install_excepthook=False,
            http_transport=httpx.MockTransport(mock_handle),
        )
        # Override transport hook again because monitor.init replaced it
        # with the real transport. We want to inspect events synchronously.
        _capture.set_transport(sink.append)

        monitor.capture_message("auto", level="error")

        assert len(sink) == 1
        assert sink[0].meta["flags"] == {"a": "enabled", "b": "beta"}
    finally:
        monitor.close()
        onelo.close()
        reset_for_tests()


def test_close_clears_flag_provider() -> None:
    _capture.set_flag_provider(lambda: {"x": "y"})
    monitor.close()
    assert _capture.get_flag_provider() is None
