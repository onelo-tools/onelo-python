"""Tests for auto-instrumentation integrations: httpx, requests, logging."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from onelo.monitor import _capture
from onelo.monitor._scope import isolation_scope, reset_for_tests
from onelo.monitor._scrub import REDACTED
from onelo.monitor._types import MonitorEvent


@pytest.fixture
def captured() -> list[MonitorEvent]:
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    reset_for_tests()
    yield sink
    _capture.set_transport(None)
    reset_for_tests()


# ─── httpx ─────────────────────────────────────────────────────────────────


def test_httpx_sync_transport_records_breadcrumb_and_scrubs_url(
    captured: list[MonitorEvent],
) -> None:
    from onelo.monitor.integrations.httpx import wrap_transport

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    inner = httpx.MockTransport(handler)
    client = httpx.Client(transport=wrap_transport(inner))

    with isolation_scope():
        client.get("https://api.example.com/users?token=secret&keep=1")
        # Capture an event so we can inspect attached breadcrumbs.
        from onelo.monitor import capture_message

        capture_message("seen", level="error")

    assert len(captured) == 1
    crumbs = captured[0].meta["breadcrumbs"]
    http_crumbs = [c for c in crumbs if c["category"] == "http"]
    assert len(http_crumbs) == 1
    crumb = http_crumbs[0]
    assert crumb["data"]["status"] == 200
    assert crumb["data"]["method"] == "GET"
    assert "duration_ms" in crumb["data"]
    # URL must be scrubbed inside the breadcrumb.
    url_str = crumb["data"]["url"]
    assert "token=" + REDACTED in url_str or "token=%5BREDACTED%5D" in url_str
    assert "keep=1" in url_str


def test_httpx_sync_transport_records_breadcrumb_on_error(
    captured: list[MonitorEvent],
) -> None:
    from onelo.monitor.integrations.httpx import wrap_transport

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=wrap_transport(httpx.MockTransport(handler)))

    with isolation_scope():
        with pytest.raises(httpx.ConnectError):
            client.get("https://api.example.com/x")
        from onelo.monitor import capture_message

        capture_message("after-fail", level="error")

    crumbs = captured[0].meta["breadcrumbs"]
    http_crumbs = [c for c in crumbs if c["category"] == "http"]
    # We still record the breadcrumb even though the request failed.
    assert len(http_crumbs) == 1
    # status is None for failed requests — Breadcrumb.http omits it from data.
    assert "status" not in http_crumbs[0].get("data", {})


def test_httpx_async_transport_records_breadcrumb(
    captured: list[MonitorEvent],
) -> None:
    from onelo.monitor.integrations.httpx import wrap_async_transport

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async def runner() -> None:
        with isolation_scope():
            client = httpx.AsyncClient(
                transport=wrap_async_transport(httpx.MockTransport(handler)),
            )
            try:
                await client.delete("https://api.example.com/y")
                from onelo.monitor import capture_message

                capture_message("done", level="error")
            finally:
                await client.aclose()

    asyncio.run(runner())

    crumbs = captured[0].meta["breadcrumbs"]
    http_crumbs = [c for c in crumbs if c["category"] == "http"]
    assert len(http_crumbs) == 1
    assert http_crumbs[0]["data"]["status"] == 204
    assert http_crumbs[0]["data"]["method"] == "DELETE"


# ─── requests ──────────────────────────────────────────────────────────────


def _skip_if_no_requests() -> None:
    pytest.importorskip("requests")


def test_requests_adapter_records_breadcrumb(captured: list[MonitorEvent]) -> None:
    _skip_if_no_requests()
    """Use a HTTPAdapter mock by intercepting at the connection level via
    the `responses` library if installed; otherwise skip the network part
    and verify by directly invoking the breadcrumb hook used by the adapter.
    """
    # We don't want a real network call in tests. Use the internal helper
    # directly to assert that `_record_breadcrumb` is shape-correct, which
    # exercises the same code path the adapter uses.
    from onelo.monitor.integrations.requests import _record_breadcrumb

    class _FakeReq:
        method = "POST"
        url = "https://api.example.com/x?token=hide&keep=1"

    with isolation_scope():
        _record_breadcrumb(_FakeReq(), status=201, duration_ms=42)
        from onelo.monitor import capture_message

        capture_message("seen", level="error")

    crumbs = captured[0].meta["breadcrumbs"]
    http_crumbs = [c for c in crumbs if c["category"] == "http"]
    assert len(http_crumbs) == 1
    assert http_crumbs[0]["data"]["status"] == 201
    assert http_crumbs[0]["data"]["duration_ms"] == 42
    assert "token=" + REDACTED in http_crumbs[0]["data"]["url"] or "token=%5BREDACTED%5D" in http_crumbs[0]["data"]["url"]


def test_requests_adapter_install_session() -> None:
    """``install_session`` mounts the adapter on both schemes so the user
    doesn't have to remember to do both."""
    requests = pytest.importorskip("requests")
    from onelo.monitor.integrations.requests import OneloRequestsAdapter, install_session

    session = requests.Session()
    install_session(session)
    # requests.Session.adapters is an OrderedDict {scheme: adapter}
    assert isinstance(session.get_adapter("http://x"), OneloRequestsAdapter)
    assert isinstance(session.get_adapter("https://x"), OneloRequestsAdapter)


# ─── logging ───────────────────────────────────────────────────────────────


def test_logging_handler_captures_error_records(captured: list[MonitorEvent]) -> None:
    from onelo.monitor.integrations.logging import OneloLoggingHandler

    handler = OneloLoggingHandler()
    logger = logging.getLogger("test_app.payments")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    logger.error("payment failed for order=42")

    assert len(captured) == 1
    event = captured[0]
    assert event.error == "payment failed for order=42"
    assert event.feature_name == "log:test_app.payments"
    assert event.ok is False


def test_logging_handler_captures_exception_with_stack(
    captured: list[MonitorEvent],
) -> None:
    from onelo.monitor.integrations.logging import OneloLoggingHandler

    handler = OneloLoggingHandler()
    logger = logging.getLogger("test_app.handler")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    try:
        raise ValueError("inside try")
    except ValueError:
        logger.exception("caught it")

    assert len(captured) == 1
    event = captured[0]
    assert event.meta["error_type"] == "ValueError"
    # Stack trace flattened by capture_exception under meta.exception.frames
    assert event.meta["exception"]["type"] == "ValueError"
    assert event.meta["exception"]["frames"]
    # Original log message is preserved alongside.
    assert event.meta["log_message"] == "caught it"


def test_logging_handler_warning_becomes_breadcrumb(
    captured: list[MonitorEvent],
) -> None:
    from onelo.monitor import capture_message
    from onelo.monitor.integrations.logging import OneloLoggingHandler

    handler = OneloLoggingHandler()
    logger = logging.getLogger("test_app.flow")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    with isolation_scope():
        logger.warning("low disk")
        capture_message("e", level="error")

    assert len(captured) == 1
    crumbs = captured[0].meta["breadcrumbs"]
    log_crumbs = [c for c in crumbs if c["category"] == "log"]
    assert len(log_crumbs) == 1
    assert log_crumbs[0]["message"] == "low disk"
    assert log_crumbs[0]["level"] == "warning"
    assert log_crumbs[0]["data"]["logger"] == "test_app.flow"


def test_logging_handler_skips_internal_loggers(captured: list[MonitorEvent]) -> None:
    """Records from `onelo.*` and `httpx` must never trigger capture —
    that would cause an infinite log loop on transport errors."""
    from onelo.monitor.integrations.logging import OneloLoggingHandler

    handler = OneloLoggingHandler()

    for name in ("onelo", "onelo.monitor", "onelo.monitor.transport", "httpx", "httpcore"):
        record = logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="should be ignored",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

    # Even nested children of those loggers should be ignored.
    for name in ("onelo.monitor.transport.batcher", "httpx.client"):
        record = logging.LogRecord(
            name=name,
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="nested",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

    assert captured == []
