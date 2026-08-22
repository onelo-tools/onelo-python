"""Regression tests for the security hardening fixes (Fala 1).

Each test name starts with the security-finding category from the audit so
the link between report → test is explicit.
"""
from __future__ import annotations

import sys
import threading
from collections import namedtuple
from typing import Any

import httpx
import pytest

from onelo import monitor
from onelo.monitor import _capture, _excepthook
from onelo.monitor._scope import isolation_scope, reset_for_tests
from onelo.monitor._scrub import REDACTED, scrub_text
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


# ───────────────────────────────────────────────────────────────────────────
# Fix 1 — regex \b bypass via non-ASCII / suffix attacks (red team finding)
# ───────────────────────────────────────────────────────────────────────────


def test_scrub_stripe_with_nonascii_prefix_no_longer_bypasses() -> None:
    """Pre-fix the Stripe regex anchored on ``\\b``. Cyrillic / full-width
    letters next to ASCII don't form a Python word boundary, so the secret
    slipped through. Now we use ASCII-only lookarounds."""
    out = scrub_text("prefixаsk_live_abcdefghij1234567890")
    assert "sk_live_" not in out
    assert REDACTED in out


def test_scrub_jwt_with_separator_prefix_is_caught() -> None:
    """JWTs in real error messages are virtually always preceded by a
    separator (``=``, ``"``, ``:``, space, or non-ASCII). All variants
    must catch the secret cleanly."""
    for prefix in ('token=', '"jwt":"', '/oauth/', 'header а'):  # last has Cyrillic 'а'
        msg = f"{prefix}eyJabc.def.ghi end"
        out = scrub_text(msg)
        assert "eyJabc.def.ghi" not in out, f"failed for prefix={prefix!r}"
        assert REDACTED in out, f"failed for prefix={prefix!r}"


def test_scrub_stripe_with_underscore_prefix_no_longer_bypasses() -> None:
    """``_sk_live_…`` — leading underscore in env-var-style identifiers."""
    out = scrub_text("ENV_sk_live_abcdefghij1234567890_SUFFIX")
    assert "sk_live_" not in out


def test_scrub_cc_with_extra_digits_no_longer_bypasses() -> None:
    """A 19-digit blob (16-digit CC + extra) used to escape because the
    trailing ``\\b`` failed when followed by another digit."""
    out = scrub_text("paid 4111111111111111111 ok")
    assert "4111111111111111" not in out
    assert REDACTED in out


def test_scrub_amex_with_extra_digits_no_longer_bypasses() -> None:
    out = scrub_text("AMEX 378282246310005777 declined")
    assert "378282246310005" not in out


def test_scrub_keeps_benign_text_through_lookarounds() -> None:
    assert scrub_text("Plan upgraded to pro") == "Plan upgraded to pro"
    # `pk_live_` in a longer word should still match (it IS a Stripe prefix).
    out = scrub_text("Using pk_live_abcdefghij1234567890")
    assert REDACTED in out


# ───────────────────────────────────────────────────────────────────────────
# Fix 2 — scrubber failures must not crash user code (red team DoS)
# ───────────────────────────────────────────────────────────────────────────


def test_scrubber_handles_namedtuple_in_meta(captured: list[MonitorEvent]) -> None:
    """Pre-fix ``type(value)(generator)`` raised ``TypeError`` on a
    namedtuple subclass and the exception escaped through ``_emit`` into
    user code — breaking the "monitoring never crashes the host" promise."""
    Point = namedtuple("Point", ["x", "y"])
    monitor.capture_message("with named", level="error", meta={"loc": Point(1, 2)})
    assert len(captured) == 1
    # The namedtuple is collapsed to a list/tuple of values — exact shape
    # is implementation-detail, but it must exist (no crash).
    assert "loc" in captured[0].meta


def test_scrubber_handles_userlist_in_meta(captured: list[MonitorEvent]) -> None:
    from collections import UserList

    class MyList(UserList):
        pass

    monitor.capture_message("with userlist", level="error",
                            meta={"items": MyList([1, 2, 3])})
    # Must not crash — UserList is not a `list` subclass, so it falls through
    # the "unknown" branch unchanged. The important thing is no exception.
    assert len(captured) == 1


# ───────────────────────────────────────────────────────────────────────────
# Fix 3 — carrier scrubs PII and only forwards user.id (security finding K3/K4)
# ───────────────────────────────────────────────────────────────────────────


def test_carrier_only_forwards_user_id_not_email() -> None:
    with isolation_scope():
        monitor.set_user({"id": "u-1", "email": "leak@example.com", "plan": "pro"})
        data = monitor.carrier()
    assert data["user"] == {"id": "u-1"}, data["user"]
    # Email / plan must NEVER end up in a queue payload.
    assert "leak@example.com" not in repr(data)
    assert "plan" not in data.get("user", {})


def test_carrier_scrubs_secrets_in_tags() -> None:
    with isolation_scope():
        monitor.set_tag("note", "Bearer eyJabc.def.ghi failed")
        data = monitor.carrier()
    # Tag value scrubbed before it leaves the host.
    assert REDACTED in data["tags"]["note"]


def test_carrier_scrubs_breadcrumb_data() -> None:
    with isolation_scope():
        monitor.add_breadcrumb("Bearer secret-leak", category="info")
        data = monitor.carrier(include_breadcrumbs=True)
    crumbs = data.get("breadcrumbs", [])
    assert crumbs
    assert all("secret-leak" not in c["message"] for c in crumbs)


# ───────────────────────────────────────────────────────────────────────────
# Fix 5 — excepthook chain identity check (architecture finding K4 + concurrency)
# ───────────────────────────────────────────────────────────────────────────


def test_uninstall_does_not_clobber_third_party_excepthook(monkeypatch) -> None:
    """If another SDK (Sentry, OTel) installs its own handler AFTER us,
    ``uninstall`` must NOT restore over it — the user's other tooling stays
    in charge."""
    original = sys.excepthook
    _excepthook.install()
    try:
        # Simulate Sentry-like: installs after us.
        def third_party(*a: Any, **kw: Any) -> None: ...
        sys.excepthook = third_party

        _excepthook.uninstall()

        # The third-party handler must still be the active one.
        assert sys.excepthook is third_party
    finally:
        sys.excepthook = original


def test_uninstall_restores_when_we_are_still_active() -> None:
    original = sys.excepthook
    _excepthook.install()
    try:
        # Onelo handler is still active.
        _excepthook.uninstall()
        assert sys.excepthook is original
    finally:
        sys.excepthook = original


# ───────────────────────────────────────────────────────────────────────────
# Fix 6 — set_user with falsy id is NOT silently dropped (architecture)
# ───────────────────────────────────────────────────────────────────────────


def test_set_user_with_int_zero_is_preserved(captured: list[MonitorEvent]) -> None:
    monitor.set_user({"id": 0})
    monitor.capture_message("hi", level="error")
    assert captured[0].user_id == "0"


def test_set_user_with_empty_string_is_preserved(captured: list[MonitorEvent]) -> None:
    monitor.set_user({"id": ""})
    monitor.capture_message("hi", level="error")
    # Empty string IS a (degenerate) user id — preserved literally.
    assert captured[0].user_id == ""


def test_set_user_with_none_id_is_dropped(captured: list[MonitorEvent]) -> None:
    """``None`` is the actual sentinel for "no user" — must drop."""
    monitor.set_user({"id": None})
    monitor.capture_message("hi", level="error")
    assert captured[0].user_id is None


# ───────────────────────────────────────────────────────────────────────────
# Fix 8 — server-side session id from monitor.init (cross-platform parity)
# ───────────────────────────────────────────────────────────────────────────


def test_init_sets_per_process_session_id_on_events() -> None:
    sink: list[MonitorEvent] = []

    def mock_handle(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    monitor.init(
        publishable_key="onelo_pk_test_x",
        api_url="https://example.com",
        install_excepthook=False,
        http_transport=httpx.MockTransport(mock_handle),
    )
    try:
        # Replace the transport hook so we can inspect the event in-process.
        _capture.set_transport(sink.append)
        monitor.capture_message("event a", level="error")
        monitor.capture_message("event b", level="error")

        assert len(sink) == 2
        assert sink[0].session_id is not None
        # Same process → same session id across events.
        assert sink[0].session_id == sink[1].session_id
        assert len(sink[0].session_id) == 32  # uuid hex
    finally:
        monitor.close()


def test_close_clears_session_id() -> None:
    monitor.init(
        publishable_key="onelo_pk_test_x",
        api_url="https://api.example.com",
        install_excepthook=False,
        http_transport=httpx.MockTransport(lambda _: httpx.Response(204)),
    )
    assert _capture.get_session_id() is not None
    monitor.close()
    assert _capture.get_session_id() is None
