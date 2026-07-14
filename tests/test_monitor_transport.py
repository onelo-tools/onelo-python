"""Tests for the HTTP transport — batching, retries, quota awareness, atexit."""
from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from onelo.monitor._transport import (
    DEFAULT_FLUSH_INTERVAL,
    MonitorTransport,
    _event_to_wire,
    _parse_retry_after,
)
from onelo.monitor._types import MonitorEvent


# ── helpers ─────────────────────────────────────────────────────────────────


class CapturingTransport:
    """httpx mock transport that records every request it sees and returns
    a configurable response. Used in place of the real network.
    """

    def __init__(self, status: int = 204, headers: dict[str, str] | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.status = status
        self.headers = headers or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Materialise the body so callers can inspect it.
        await request.aread()
        self.calls.append(request)
        return httpx.Response(self.status, headers=self.headers, request=request)


def _event(name: str = "test", ok: bool = True, source: str = "event") -> MonitorEvent:
    return MonitorEvent(feature_name=name, ok=ok, source=source)


# ── basic send + flush ──────────────────────────────────────────────────────


def test_send_buffers_event_and_flush_pushes_batch() -> None:
    mock = CapturingTransport(status=204)
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,  # disable timer-driven flush for this test
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        for i in range(5):
            t.send(_event(f"feature-{i}"))
        assert t.buffer_size == 5
        t.flush(timeout=2.0)
    finally:
        t.stop()

    assert len(mock.calls) == 1
    body = mock.calls[0].read()
    assert b"feature-0" in body
    assert b"feature-4" in body
    assert b'"publishableKey":"k"' in body


def test_error_event_triggers_immediate_flush() -> None:
    mock = CapturingTransport()
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("err", ok=False))
        # Wait briefly for the wake -> flush cycle.
        for _ in range(40):
            if mock.calls:
                break
            time.sleep(0.025)
        assert mock.calls, "errored event should flush without waiting for the timer"
    finally:
        t.stop()


def test_flush_waits_for_inflight_error_send() -> None:
    """Regression: monitor.flush() must not return until an error event that
    already woke the transport loop has actually been delivered.

    An error event (ok=False) wakes the loop to flush immediately. That wake
    flush and the explicit flush() race on _do_flush(); before the _flush_lock
    fix, flush() could observe the buffer already drained by the wake flush and
    signal `done` while the POST was still in flight — returning before delivery
    (the event is then lost when a short-lived process exits right after
    flush()). A deliberately slow send widens the window: with the lock, flush()
    blocks on the in-flight send; without it, slow.calls is still empty on
    return.
    """
    send_started = threading.Event()

    class SlowTransport:
        def __init__(self) -> None:
            self.calls: list[httpx.Request] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await request.aread()
            send_started.set()
            await asyncio.sleep(0.3)  # hold the send in flight
            self.calls.append(request)
            return httpx.Response(204, request=request)

    slow = SlowTransport()
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(slow.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("err", ok=False))  # wakes the loop -> wake flush drains + sends
        assert send_started.wait(timeout=2.0), "wake flush never started the send"
        # Buffer is already drained by the wake flush and the POST is mid-await.
        # flush() must block on _flush_lock until that send completes.
        t.flush(timeout=5.0)
        assert len(slow.calls) == 1, "flush() returned before the in-flight error send completed"
    finally:
        t.stop()


def test_buffer_overflow_drops_oldest_and_increments_counter() -> None:
    mock = CapturingTransport()
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        buffer_size=3,
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        for i in range(10):
            t.send(_event(f"f{i}"))
        # 7 dropped (10 - capacity 3)
        assert t.dropped_count == 7
        assert t.buffer_size == 3
    finally:
        t.stop()


# ── error-response handling ─────────────────────────────────────────────────


def test_4xx_response_drops_batch_without_retry() -> None:
    mock = CapturingTransport(status=403)
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("x"))
        t.flush(timeout=2.0)
    finally:
        t.stop()
    # Single attempt — 4xx is non-retryable.
    assert len(mock.calls) == 1


def test_429_honours_retry_after_window() -> None:
    """The transport should not flush again until Retry-After expires."""
    mock = CapturingTransport(status=429, headers={"Retry-After": "60"})
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=0.05,  # fast loop so we'd see repeated calls without the gate
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("first"))
        # First flush returns 429 with Retry-After.
        t.flush(timeout=2.0)
        first_count = len(mock.calls)
        assert first_count >= 1

        # Send more events while the cooldown is active.
        for i in range(3):
            t.send(_event(f"during-cooldown-{i}"))
        # Allow several flush ticks. None should escape because of Retry-After.
        time.sleep(0.3)
    finally:
        t.stop()
    # At most the first 429 + one final atexit drain attempt that also returns
    # 429 — but no flood of retries.
    assert len(mock.calls) <= 2


# ── exception safety ───────────────────────────────────────────────────────


def test_send_does_not_raise_when_loop_is_dead() -> None:
    """Calling .send after stop must be silent — shutdown timing is unpredictable."""
    mock = CapturingTransport()
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    t.stop()
    # Should not raise.
    t.send(_event("late"))
    t.flush(timeout=0.1)


# ── wire format ─────────────────────────────────────────────────────────────


def test_event_to_wire_has_camelcase_keys_matching_backend() -> None:
    from datetime import datetime, timezone

    event = MonitorEvent(
        feature_name="checkout",
        ok=False,
        duration_ms=42,
        error="boom",
        source="track",
        user_id="u",
        session_id="s",
        meta={"plan": "pro"},
        timestamp=1_700_000_000.0,
    )
    wire = _event_to_wire(event)
    assert wire == {
        "featureName": "checkout",
        "ok": False,
        "platform": "python",
        "source": "track",
        # M7: real event time on the wire (backend stores as display-only event_ts).
        "ts": datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc).isoformat(),
        "durationMs": 42,
        "error": "boom",
        "userId": "u",
        "sessionId": "s",
        "meta": {"plan": "pro"},
    }


def test_event_to_wire_omits_optional_nones() -> None:
    event = MonitorEvent(feature_name="x", ok=True)
    wire = _event_to_wire(event)
    assert "durationMs" not in wire
    assert "error" not in wire
    assert "userId" not in wire
    assert "sessionId" not in wire
    assert "meta" not in wire


# ── retry-after parser ─────────────────────────────────────────────────────


@pytest.mark.parametrize("header,expected", [
    (None, 0.0),
    ("", 0.0),
    ("0", 0.0),
    ("60", 60.0),
    ("3.5", 3.5),
    ("not-a-number", 0.0),
    ("-10", 0.0),  # negative is clamped — we never sleep into the past
])
def test_parse_retry_after(header: str | None, expected: float) -> None:
    assert _parse_retry_after(header) == expected


# ── default flush interval matches the Swift SDK ───────────────────────────


def test_flush_interval_default_matches_swift_sdk() -> None:
    """Both SDKs aim for the same dashboard latency. If this changes here,
    update the Swift SDK to match (or vice versa)."""
    assert DEFAULT_FLUSH_INTERVAL == 15.0


def test_reinit_after_fork_rebuilds_thread_and_delivers() -> None:
    """After a fork the parent's loop thread is dead; reinit_after_fork must
    rebuild it so the worker actually delivers events (M1)."""
    mock = CapturingTransport(status=204)
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    old_thread = t._thread
    t.send(_event("parent-startup", ok=True))  # buffered by the "parent"
    # Simulate the child side of a fork (same process, so the old daemon thread
    # lingers harmlessly parked on its 999s timer — in a real fork it wouldn't
    # exist in the child at all).
    t.reinit_after_fork()
    try:
        assert t._thread is not old_thread
        assert t._thread.is_alive()
        # The parent's buffered event is dropped in the child (the parent owns
        # and delivers it) — no duplicate re-POST.
        assert t.buffer_size == 0
        t.send(_event("after-fork", ok=False))
        t.flush(timeout=2.0)
    finally:
        t.stop()

    assert len(mock.calls) == 1
    body = mock.calls[0].read()
    assert b"after-fork" in body
    assert b"parent-startup" not in body


# ── M5: quota self-throttle ─────────────────────────────────────────────────

def test_quota_exhausted_holds_off_subsequent_sends() -> None:
    """A 204 with X-Onelo-Quota-Remaining: 0 must make the transport self-throttle
    — the next batch is held off instead of being sent into a guaranteed 429."""
    mock = CapturingTransport(status=204, headers={"X-Onelo-Quota-Remaining": "0"})
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("first", ok=False))
        t.flush(timeout=2.0)
        assert len(mock.calls) == 1
        assert t.quota_remaining == 0
        # Held off now — a second event must NOT reach the network.
        t.send(_event("second", ok=False))
        t.flush(timeout=2.0)
        assert len(mock.calls) == 1
    finally:
        t.stop()


def test_quota_remaining_tracked_and_unlimited_clears() -> None:
    mock = CapturingTransport(status=204, headers={"X-Onelo-Quota-Remaining": "42"})
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    try:
        t.send(_event("e", ok=False))
        t.flush(timeout=2.0)
        assert t.quota_remaining == 42
        # 'unlimited' clears the tracker and never holds off.
        mock.headers = {"X-Onelo-Quota-Remaining": "unlimited"}
        t.send(_event("e2", ok=False))
        t.flush(timeout=2.0)
        assert t.quota_remaining is None
    finally:
        t.stop()


# ── M6: dropped-event signal ────────────────────────────────────────────────

def test_dropped_events_are_reported() -> None:
    import logging

    mock = CapturingTransport(status=204)
    t = MonitorTransport(
        api_url="https://example.com",
        publishable_key="k",
        buffer_size=2,
        flush_interval=999.0,
        http_transport=httpx.MockTransport(mock.handle_async_request),
    )
    t.start()
    caplog_records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            caplog_records.append(record)

    handler = _Grab(level=logging.WARNING)
    logging.getLogger("onelo.monitor.transport").addHandler(handler)
    try:
        # 5 events into a size-2 buffer → 3 dropped (ok=True so no wake/flush).
        for i in range(5):
            t.send(_event(f"e{i}", ok=True))
        assert t.dropped_count == 3
        t.stop()  # final drain + _maybe_report_drops runs on the loop thread
    finally:
        logging.getLogger("onelo.monitor.transport").removeHandler(handler)

    msgs = [r.getMessage() for r in caplog_records]
    assert any("dropped 3 event" in m for m in msgs), msgs
