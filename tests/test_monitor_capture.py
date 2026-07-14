"""Tests for the public capture pipeline.

We register a list-based test sink in place of the HTTP transport so we can
inspect the events that would have left the process.
"""
from __future__ import annotations

import pytest

from onelo.monitor import (
    Breadcrumb,
    add_breadcrumb,
    capture_event,
    capture_exception,
    capture_message,
    isolation_scope,
    set_context,
    set_extra,
    set_tag,
    set_transport,
    set_user,
)
from onelo.monitor._scope import reset_for_tests
from onelo.monitor._scrub import REDACTED
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    set_transport(sink.append)
    reset_for_tests()
    yield sink
    set_transport(None)
    reset_for_tests()


# ── capture_exception ──────────────────────────────────────────────────────

def test_capture_exception_emits_event_with_stack(captured: list[MonitorEvent]) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        capture_exception()

    assert len(captured) == 1
    event = captured[0]
    assert event.ok is False
    assert event.error == "boom"
    assert event.feature_name == "manual"
    assert "exception" in event.meta
    assert event.meta["exception"]["type"] == "ValueError"
    assert event.meta["exception"]["frames"]


def test_capture_exception_silent_outside_except(captured: list[MonitorEvent]) -> None:
    capture_exception()  # no active exc_info
    assert captured == []


def test_capture_exception_with_explicit_exception(captured: list[MonitorEvent]) -> None:
    try:
        raise RuntimeError("explicit")
    except RuntimeError as e:
        err = e
    capture_exception(err)
    assert len(captured) == 1
    assert captured[0].error == "explicit"


def test_capture_exception_attaches_breadcrumbs(captured: list[MonitorEvent]) -> None:
    add_breadcrumb(Breadcrumb.info("step 1"))
    add_breadcrumb(Breadcrumb.info("step 2"))
    try:
        raise ValueError("boom")
    except ValueError:
        capture_exception()

    crumbs = captured[0].meta["breadcrumbs"]
    assert [c["message"] for c in crumbs] == ["step 1", "step 2"]


def test_capture_exception_runs_pii_scrubber(captured: list[MonitorEvent]) -> None:
    try:
        raise RuntimeError("Bearer eyJabc.def.ghi failed")
    except RuntimeError:
        capture_exception(meta={"password": "leaked", "plan": "pro"})

    event = captured[0]
    # Error string scrubbed
    assert REDACTED in event.error
    assert "eyJabc.def.ghi" not in event.error
    # Meta key denylisted
    assert event.meta["password"] == REDACTED
    assert event.meta["plan"] == "pro"


# ── capture_message ────────────────────────────────────────────────────────

def test_capture_message_info_level_marks_event_ok(captured: list[MonitorEvent]) -> None:
    capture_message("hello", level="info")
    assert captured[0].ok is True
    assert captured[0].error is None


def test_capture_message_error_level_marks_event_failed(captured: list[MonitorEvent]) -> None:
    capture_message("something broke", level="error")
    event = captured[0]
    assert event.ok is False
    assert event.error == "something broke"


def test_capture_message_attaches_stacktrace_when_requested(captured: list[MonitorEvent]) -> None:
    capture_message("trace me", level="info", attach_stacktrace=True)
    assert "stack" in captured[0].meta
    assert captured[0].meta["stack"]


# ── set_user / set_tag / etc apply to event ────────────────────────────────

def test_set_user_propagates_to_event(captured: list[MonitorEvent]) -> None:
    set_user({"id": "u123"})
    capture_message("hi")
    assert captured[0].user_id == "u123"


def test_set_tag_attaches_to_meta(captured: list[MonitorEvent]) -> None:
    set_tag("env", "prod")
    capture_message("hi")
    assert captured[0].meta["tags"]["env"] == "prod"


def test_set_context_attaches_to_meta(captured: list[MonitorEvent]) -> None:
    set_context("request", {"method": "POST", "path": "/api/x"})
    capture_message("hi")
    assert captured[0].meta["contexts"]["request"]["path"] == "/api/x"


def test_set_extra_attaches_to_meta(captured: list[MonitorEvent]) -> None:
    set_extra("trace_id", "abc-123")
    capture_message("hi")
    assert captured[0].meta["extra"]["trace_id"] == "abc-123"


# ── isolation between requests ─────────────────────────────────────────────

def test_isolation_scope_prevents_user_leak(captured: list[MonitorEvent]) -> None:
    """The fundamental correctness property: user A and user B in
    sequential 'requests' must not contaminate each other."""
    with isolation_scope():
        set_user({"id": "user-A"})
        capture_message("from A", level="error")

    with isolation_scope():
        # No set_user — fresh isolation scope inherits empty user.
        capture_message("from B", level="error")

    assert len(captured) == 2
    assert captured[0].user_id == "user-A"
    assert captured[1].user_id is None


# ── transport hooks ────────────────────────────────────────────────────────

def test_capture_is_silent_when_no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    set_transport(None)
    # Should not raise even though there's no sink.
    capture_message("dropped")


def test_transport_exception_does_not_propagate(captured: list[MonitorEvent]) -> None:
    """A misbehaving transport must never crash user code."""
    def boom_sink(_event: MonitorEvent) -> None:
        raise RuntimeError("transport down")
    set_transport(boom_sink)
    # If this raises, the SDK has a critical defect.
    capture_message("hi")


# ── add_breadcrumb shapes ──────────────────────────────────────────────────

def test_add_breadcrumb_string_shape(captured: list[MonitorEvent]) -> None:
    add_breadcrumb("loaded users", category="db", duration_ms=42)
    capture_message("e", level="error")
    crumbs = captured[0].meta["breadcrumbs"]
    assert crumbs[0]["category"] == "db"
    assert crumbs[0]["message"] == "loaded users"
    assert crumbs[0]["data"] == {"duration_ms": 42}


def test_add_breadcrumb_object_shape(captured: list[MonitorEvent]) -> None:
    add_breadcrumb(Breadcrumb.http("GET", "https://x", status=200))
    capture_message("e", level="error")
    assert captured[0].meta["breadcrumbs"][0]["category"] == "http"


# ── capture_event lowlevel ─────────────────────────────────────────────────

def test_capture_event_low_level_runs_scrub_and_scope(captured: list[MonitorEvent]) -> None:
    set_user({"id": "u"})
    event = MonitorEvent(
        feature_name="custom",
        ok=False,
        error="Bearer xyz123",
        meta={"token": "secret"},
    )
    capture_event(event)
    out = captured[0]
    assert out.user_id == "u"
    assert REDACTED in out.error
    assert out.meta["token"] == REDACTED
