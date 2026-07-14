"""Regression tests for Fala 2 hardening — items the audit raised after
Fala 1 had already shipped. Each test name links the audit category."""
from __future__ import annotations

import httpx
import pytest

from onelo import monitor
from onelo.monitor import _capture
from onelo.monitor._scope import isolation_scope, reset_for_tests
from onelo.monitor._scrub import REDACTED, scrub_text, scrub_url
from onelo.monitor._transport import (
    _MAX_RETRY_AFTER_SECONDS,
    _parse_retry_after,
)
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    _capture.set_flag_provider(None)
    _capture.set_session_id(None)
    reset_for_tests()
    yield sink
    _capture.set_transport(None)
    _capture.set_flag_provider(None)
    _capture.set_session_id(None)
    reset_for_tests()


# ─── F2.1 — Retry-After clamp ──────────────────────────────────────────────


def test_retry_after_clamp_to_one_hour() -> None:
    """A malicious or MITM'd backend must not be able to wedge the SDK
    for years with ``Retry-After: 99999999``."""
    assert _parse_retry_after("99999999") == _MAX_RETRY_AFTER_SECONDS == 3600.0
    assert _parse_retry_after("3600") == 3600.0
    assert _parse_retry_after("3601") == 3600.0  # at the cap
    assert _parse_retry_after("60") == 60.0     # below the cap, untouched


def test_retry_after_negative_or_garbage_returns_zero() -> None:
    assert _parse_retry_after("-100") == 0.0
    assert _parse_retry_after("garbage") == 0.0
    assert _parse_retry_after(None) == 0.0
    assert _parse_retry_after("") == 0.0


# ─── F2.2 — scrub_url scrubs OAuth implicit-grant fragments ───────────────


def test_scrub_url_redacts_oauth_implicit_grant_fragment_token() -> None:
    """OAuth implicit grant returns access_token in the URL fragment.
    Pre-fix the fragment was emitted verbatim into breadcrumbs."""
    out = scrub_url("https://app.example.com/callback#access_token=eyJabc.def.ghi&token_type=Bearer")
    assert out is not None
    assert "eyJabc.def.ghi" not in out
    # urlencode percent-encodes the brackets, so accept either form.
    assert REDACTED in out or "%5BREDACTED%5D" in out


def test_scrub_url_redacts_id_token_in_fragment() -> None:
    out = scrub_url("https://app.example.com/cb#id_token=secret&state=xyz")
    assert out is not None
    assert "id_token=" + REDACTED in out or "id_token=%5BREDACTED%5D" in out
    # Non-sensitive fragment fields stay readable.
    assert "state=xyz" in out


def test_scrub_url_keeps_benign_fragment() -> None:
    out = scrub_url("https://app.example.com/page#section-3")
    assert out == "https://app.example.com/page#section-3"


# ─── F2.7 — context_line scrubs literal secrets ───────────────────────────


def test_stack_context_line_scrubs_literal_secret(captured: list[MonitorEvent]) -> None:
    """A stack frame whose source line contains a literal Bearer token
    must NOT ship that literal into the dashboard."""
    # Synthesise a function whose source line has a Bearer literal so
    # linecache.getline returns the secret-bearing source.
    code = (
        "def boom():\n"
        "    token = 'Bearer abcDEF1234567890_real_secret'  # literal\n"
        "    raise ValueError('boom')\n"
    )
    ns: dict = {}
    # Use a fake filename so linecache can resolve it via the cache.
    import linecache
    filename = "<onelo-test-frame>"
    linecache.cache[filename] = (
        len(code),
        None,
        code.splitlines(keepends=True),
        filename,
    )
    exec(compile(code, filename, "exec"), ns)
    try:
        ns["boom"]()
    except ValueError:
        monitor.capture_exception()

    # The stack frame should be scrubbed.
    assert len(captured) == 1
    frames = captured[0].meta["exception"]["frames"]
    leaked = [f for f in frames if f.get("context_line") and "abcDEF1234567890_real_secret" in f["context_line"]]
    assert not leaked, "literal Bearer token leaked through context_line"


# ─── F2.8 — HMAC carrier integrity ────────────────────────────────────────


def test_signed_carrier_round_trip_succeeds(captured: list[MonitorEvent]) -> None:
    """Producer signs with key K, worker verifies with key K → carrier
    is trusted and applied to the worker's scope."""
    key = "shared-app-secret"
    with isolation_scope():
        monitor.set_user({"id": "producer-user"})
        signed = monitor.carrier(key=key)

    # Verify it really is signed (not a raw dict).
    assert "_sig" in signed
    assert "_data" in signed
    assert "user" in signed["_data"]

    # Worker side.
    with monitor.continue_trace(signed, key=key):
        monitor.capture_message("worker", level="error")

    assert captured[0].user_id == "producer-user"


def test_signed_carrier_with_wrong_key_is_rejected(captured: list[MonitorEvent]) -> None:
    """An attacker-forged carrier (wrong HMAC) must NOT impersonate a user."""
    with isolation_scope():
        monitor.set_user({"id": "attacker-target-user"})
        signed = monitor.carrier(key="real-app-secret")

    # Worker has a different shared secret — verification fails.
    with monitor.continue_trace(signed, key="different-secret"):
        monitor.capture_message("worker", level="error")

    # The captured event should NOT carry the producer's user_id.
    assert captured[0].user_id is None


def test_signed_carrier_tampered_payload_is_rejected(captured: list[MonitorEvent]) -> None:
    """Modifying the carrier dict invalidates the HMAC."""
    key = "shared-secret"
    with isolation_scope():
        monitor.set_user({"id": "u1"})
        signed = monitor.carrier(key=key)

    # Tamper — flip user.id under the original signature.
    signed["_data"]["user"]["id"] = "victim"

    with monitor.continue_trace(signed, key=key):
        monitor.capture_message("worker", level="error")

    assert captured[0].user_id is None


def test_unsigned_carrier_with_key_required_is_rejected(captured: list[MonitorEvent]) -> None:
    """If the worker requires signed carriers (passes ``key=``) and the
    producer didn't sign, the carrier is rejected — fail-closed."""
    with isolation_scope():
        monitor.set_user({"id": "producer-user"})
        unsigned = monitor.carrier()  # no key — raw dict

    with monitor.continue_trace(unsigned, key="worker-key"):
        monitor.capture_message("worker", level="error")

    assert captured[0].user_id is None


def test_signed_carrier_without_worker_key_is_rejected(captured: list[MonitorEvent]) -> None:
    """A worker that doesn't pass ``key=`` cannot verify a signed carrier
    — better to drop than to silently trust unverified data."""
    with isolation_scope():
        monitor.set_user({"id": "u-1"})
        signed = monitor.carrier(key="some-key")

    with monitor.continue_trace(signed):  # no key on worker
        monitor.capture_message("worker", level="error")

    assert captured[0].user_id is None


def test_continue_trace_task_with_key_factory_form() -> None:
    """The decorator accepts ``@continue_trace_task(key=…)`` as well as
    bare ``@continue_trace_task``."""
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    reset_for_tests()
    try:
        @monitor.continue_trace_task(key="k")
        def task(*args, **kwargs):
            monitor.capture_message("ran", level="error")

        with isolation_scope():
            monitor.set_user({"id": "u1"})
            signed = monitor.carrier(key="k")

        task(onelo=signed)
        assert sink[0].user_id == "u1"
    finally:
        _capture.set_transport(None)
        reset_for_tests()
