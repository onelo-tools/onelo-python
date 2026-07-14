"""Regression tests for Fala 3 hardening — opt-in email scrub + executor
context propagation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from onelo import monitor
from onelo.monitor import _capture
from onelo.monitor._scope import isolation_scope, reset_for_tests
from onelo.monitor._scrub import REDACTED, scrub_text, set_strict_email_scrub
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    _capture.set_session_id(None)
    set_strict_email_scrub(False)
    reset_for_tests()
    yield sink
    _capture.set_transport(None)
    _capture.set_session_id(None)
    set_strict_email_scrub(False)
    reset_for_tests()


# ─── F3.2 — opt-in email scrub ─────────────────────────────────────────────


def test_email_default_off_keeps_email_in_text() -> None:
    set_strict_email_scrub(False)
    out = scrub_text("user x@example.com signed in")
    assert "x@example.com" in out


def test_email_strict_mode_redacts_emails() -> None:
    set_strict_email_scrub(True)
    try:
        out = scrub_text("user x@example.com signed in")
        assert "x@example.com" not in out
        assert REDACTED in out
    finally:
        set_strict_email_scrub(False)


def test_email_strict_mode_via_init() -> None:
    """Wiring through ``monitor.init(strict_email_scrub=True)``."""
    monitor.init(
        publishable_key="onelo_pk_test_x",
        install_excepthook=False,
        strict_email_scrub=True,
        http_transport=httpx.MockTransport(lambda _: httpx.Response(204)),
    )
    try:
        out = scrub_text("contact: a.b@onelo.tools")
        assert "a.b@onelo.tools" not in out
    finally:
        monitor.close()


def test_close_resets_strict_email_scrub() -> None:
    monitor.init(
        publishable_key="onelo_pk_test_x",
        install_excepthook=False,
        strict_email_scrub=True,
        http_transport=httpx.MockTransport(lambda _: httpx.Response(204)),
    )
    monitor.close()
    # After close, default behaviour restored — email kept.
    out = scrub_text("hello@example.com")
    assert "hello@example.com" in out


# ─── F3.3 — ScopeAwareExecutor / scope_aware ───────────────────────────────


def test_thread_pool_without_wrapper_loses_scope(captured: list[MonitorEvent]) -> None:
    """Demonstrates the bug we're fixing: raw ThreadPoolExecutor.submit
    does NOT propagate ContextVars, so user_id from the caller's scope
    doesn't reach the worker."""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        with isolation_scope():
            monitor.set_user({"id": "caller-user"})

            def work() -> None:
                # Worker thread has its own (empty) ContextVar context.
                monitor.capture_message("from worker", level="error")

            future = pool.submit(work)
            future.result(timeout=2.0)

        # Without copy_context, the worker's event has no user_id.
        worker_events = [e for e in captured if e.error == "from worker"]
        assert len(worker_events) == 1
        assert worker_events[0].user_id is None
    finally:
        pool.shutdown(wait=True)


def test_scope_aware_call_site_helper_propagates_scope(
    captured: list[MonitorEvent],
) -> None:
    """``scope_aware`` is a call-site helper — wrap the callable inside
    the request handler so the wrapper captures the active context."""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        def work() -> None:
            monitor.capture_message("from worker", level="error")

        with isolation_scope():
            monitor.set_user({"id": "caller-2"})
            wrapped = monitor.scope_aware(work)  # captures context HERE
            future = pool.submit(wrapped)
            future.result(timeout=2.0)

        ev = next(e for e in captured if e.error == "from worker")
        assert ev.user_id == "caller-2"
    finally:
        pool.shutdown(wait=True)


def test_scope_aware_executor_propagates_scope(captured: list[MonitorEvent]) -> None:
    inner = ThreadPoolExecutor(max_workers=2)
    pool = monitor.ScopeAwareExecutor(inner)
    try:
        with isolation_scope():
            monitor.set_user({"id": "caller-3"})

            def work() -> None:
                monitor.capture_message("from worker", level="error")

            future = pool.submit(work)
            future.result(timeout=2.0)

        ev = next(e for e in captured if e.error == "from worker")
        assert ev.user_id == "caller-3"
    finally:
        pool.shutdown(wait=True)


def test_scope_aware_executor_isolates_concurrent_callers(
    captured: list[MonitorEvent],
) -> None:
    """Two concurrent callers each wrap their own scope — workers see
    their own caller's user_id, never cross-contaminated."""
    inner = ThreadPoolExecutor(max_workers=4)
    pool = monitor.ScopeAwareExecutor(inner)
    try:
        def caller(name: str) -> None:
            with isolation_scope():
                monitor.set_user({"id": name})
                future = pool.submit(
                    lambda: monitor.capture_message(f"event from {name}", level="error")
                )
                future.result(timeout=2.0)

        # Two sequential "requests" — the wrapped executor must see each
        # caller's user_id correctly.
        caller("alpha")
        caller("beta")

        events_by_user = {e.user_id: e for e in captured if e.error and "from" in e.error}
        assert events_by_user.get("alpha") is not None
        assert events_by_user.get("beta") is not None
        assert "alpha" in events_by_user["alpha"].error
        assert "beta" in events_by_user["beta"].error
    finally:
        pool.shutdown(wait=True)
