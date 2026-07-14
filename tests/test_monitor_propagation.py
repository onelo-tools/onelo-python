"""Tests for background-task scope propagation (Celery / RQ / Cloud Tasks)."""
from __future__ import annotations

import pytest

from onelo import monitor
from onelo.monitor import _capture
from onelo.monitor._propagation import _extract_carrier
from onelo.monitor._scope import (
    get_isolation_scope,
    isolation_scope,
    reset_for_tests,
)
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    reset_for_tests()
    yield sink
    _capture.set_transport(None)
    reset_for_tests()


# ─── carrier (serialise) ───────────────────────────────────────────────────


def test_carrier_includes_user_id_only_and_tags() -> None:
    """Carrier deliberately whitelists ``user.id`` — extra fields like
    ``email``, ``username``, custom metadata never travel through the queue
    payload (which may end up persisted in Redis AOF / SQS log without a
    DPA). See `_propagation.carrier` privacy notes.
    """
    with isolation_scope():
        monitor.set_user({"id": "u1", "email": "x@y.com"})
        monitor.set_tag("plan", "pro")
        monitor.set_tag("region", "eu")

        data = monitor.carrier()
        assert data["user"] == {"id": "u1"}
        # Email must not leak through the carrier.
        assert "x@y.com" not in repr(data)
        assert data["tags"] == {"plan": "pro", "region": "eu"}
        assert "trace_id" in data
        assert len(data["trace_id"]) == 32  # uuid hex


def test_carrier_omits_breadcrumbs_by_default() -> None:
    with isolation_scope():
        monitor.add_breadcrumb("step 1")
        monitor.add_breadcrumb("step 2")
        data = monitor.carrier()
        assert "breadcrumbs" not in data


def test_carrier_includes_breadcrumbs_when_requested() -> None:
    with isolation_scope():
        monitor.add_breadcrumb("step 1")
        monitor.add_breadcrumb("step 2")
        data = monitor.carrier(include_breadcrumbs=True)
        assert len(data["breadcrumbs"]) == 2
        assert [c["message"] for c in data["breadcrumbs"]] == ["step 1", "step 2"]


# ─── continue_trace ─────────────────────────────────────────────────────────


def test_continue_trace_restores_user_and_tags(captured: list[MonitorEvent]) -> None:
    payload = {
        "user": {"id": "u-from-producer"},
        "tags": {"region": "us"},
        "trace_id": "abc123",
    }
    with monitor.continue_trace(payload):
        monitor.capture_message("worker ran", level="error")

    assert len(captured) == 1
    event = captured[0]
    assert event.user_id == "u-from-producer"
    assert event.meta["tags"]["region"] == "us"
    assert event.meta["tags"]["trace_id"] == "abc123"


def test_continue_trace_restores_breadcrumbs(captured: list[MonitorEvent]) -> None:
    payload = {
        "breadcrumbs": [
            {"category": "info", "message": "scheduled at producer", "ts": 1.0, "level": "info"},
        ],
    }
    with monitor.continue_trace(payload):
        monitor.capture_message("worker", level="error")

    crumbs = captured[0].meta["breadcrumbs"]
    assert any(c["message"] == "scheduled at producer" for c in crumbs)


def test_continue_trace_does_not_leak_outside_block() -> None:
    payload = {"user": {"id": "inside"}}
    with monitor.continue_trace(payload):
        assert get_isolation_scope().user is not None
        assert get_isolation_scope().user["id"] == "inside"

    # Outside the block, no leakage.
    user = get_isolation_scope().user
    assert user is None or user.get("id") != "inside"


def test_continue_trace_with_none_is_safe() -> None:
    """No carrier (e.g. legacy task without scheduler-side wiring) should
    still run the body — just without an inherited scope."""
    with monitor.continue_trace(None):
        get_isolation_scope().set_user({"id": "fallback"})


# ─── continue_trace_task decorator ─────────────────────────────────────────


def test_continue_trace_task_picks_up_kwarg_carrier(captured: list[MonitorEvent]) -> None:
    """RQ / direct call convention: producer passes onelo=carrier as kwarg."""
    @monitor.continue_trace_task
    def my_task(*args, **kwargs):
        monitor.capture_message("task ran", level="error")
        return "ok"

    payload = {"user": {"id": "kwarg-user"}, "tags": {"src": "kwarg"}}
    result = my_task(onelo=payload)

    assert result == "ok"
    assert captured[0].user_id == "kwarg-user"
    assert captured[0].meta["tags"]["src"] == "kwarg"


def test_continue_trace_task_strips_onelo_kwarg() -> None:
    """The wrapped function must NOT see ``onelo=`` in its kwargs — that
    would break tasks with strict argument signatures."""
    received_kwargs: dict[str, object] = {}

    @monitor.continue_trace_task
    def my_task(**kwargs):
        received_kwargs.update(kwargs)

    my_task(onelo={"user": {"id": "x"}}, real_arg=123)

    assert "onelo" not in received_kwargs
    assert received_kwargs == {"real_arg": 123}


def test_continue_trace_task_picks_up_celery_request_headers(
    captured: list[MonitorEvent],
) -> None:
    """Celery `bind=True` passes self with .request.headers — the helper
    pulls our carrier from there."""
    class FakeCeleryRequest:
        headers = {"onelo": {"user": {"id": "from-celery-headers"}}}

    class FakeBoundTask:
        request = FakeCeleryRequest()

    @monitor.continue_trace_task
    def task(self_, *args, **kwargs):
        monitor.capture_message("celery", level="error")

    task(FakeBoundTask())
    assert captured[0].user_id == "from-celery-headers"


def test_extract_carrier_returns_none_when_absent() -> None:
    """No producer-side wiring → no carrier → task still runs (handled by
    `continue_trace(None)`)."""
    assert _extract_carrier((), {}) is None
    assert _extract_carrier(("not a self",), {}) is None
    assert _extract_carrier((), {"unrelated": "x"}) is None
