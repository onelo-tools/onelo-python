"""Integration tests for ASGI / WSGI / Django / Flask middleware.

Goals:
  - Each middleware forks an isolation scope per request (no leak between
    concurrent requests).
  - Request context (URL, method, scrubbed headers) ends up on the event.
  - Response status code is tagged.
  - Exceptions from the inner handler are captured AND re-raised.
  - Frameworks specific to this file (FastAPI, Django, Flask) are imported
    lazily — the tests skip cleanly if the optional dep isn't installed.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from onelo import monitor
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


# ─── ASGI ──────────────────────────────────────────────────────────────────


def _run_asgi(app: Any, scope: dict[str, Any]) -> tuple[list[dict[str, Any]], BaseException | None]:
    """Drive an ASGI app to completion. Returns sent messages + any exception."""
    sent: list[dict[str, Any]] = []
    incoming = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    err: BaseException | None = None
    try:
        asyncio.run(app(scope, receive, send))
    except BaseException as e:  # noqa: BLE001
        err = e
    return sent, err


def _http_scope(method: str = "GET", path: str = "/api/x") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [(b"host", b"testserver"), (b"authorization", b"Bearer secret")],
    }


def test_asgi_middleware_captures_exception_and_reraises(captured: list[MonitorEvent]) -> None:
    from onelo.monitor.integrations import OneloMonitorASGIMiddleware

    async def boom_app(scope: dict, receive: Any, send: Any) -> None:
        raise RuntimeError("boom inside handler")

    middleware = OneloMonitorASGIMiddleware(boom_app)
    _, err = _run_asgi(middleware, _http_scope())

    # Exception must propagate so the framework's error handler runs.
    assert isinstance(err, RuntimeError)
    # Event must have been captured BEFORE the re-raise.
    assert len(captured) == 1
    event = captured[0]
    assert event.error == "boom inside handler"
    assert event.feature_name == "http_request"
    # Request context attached…
    assert event.meta["contexts"]["request"]["method"] == "GET"
    # …and Authorization header redacted.
    assert event.meta["contexts"]["request"]["headers"].get("authorization", REDACTED) == REDACTED


def test_asgi_middleware_attaches_response_status(captured: list[MonitorEvent]) -> None:
    from onelo.monitor.integrations import OneloMonitorASGIMiddleware

    async def ok_app(scope: dict, receive: Any, send: Any) -> None:
        await send({
            "type": "http.response.start",
            "status": 201,
            "headers": [],
        })
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = OneloMonitorASGIMiddleware(ok_app)

    # Capture an event from inside the request to verify the status tag landed.
    async def app_then_capture(scope: dict, receive: Any, send: Any) -> None:
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [],
        })
        # Capture an event WHILE inside the request scope so we can assert
        # the http.status_code tag is set by send_with_capture.
        monitor.capture_message("after-status", level="error")
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    middleware = OneloMonitorASGIMiddleware(app_then_capture)
    _run_asgi(middleware, _http_scope())

    assert len(captured) == 1
    assert captured[0].meta["tags"]["http.status_code"] == "503"


def test_asgi_middleware_isolates_concurrent_requests() -> None:
    """The whole point of forking an isolation scope per request: user A's
    breadcrumbs MUST NOT appear in user B's event."""
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    reset_for_tests()

    from onelo.monitor.integrations import OneloMonitorASGIMiddleware

    async def app(scope: dict, receive: Any, send: Any) -> None:
        # Identify the request by its path so we can correlate.
        user_id = scope["path"].lstrip("/")
        monitor.set_user({"id": user_id})
        monitor.add_breadcrumb(f"crumb for {user_id}")
        await asyncio.sleep(0.01)
        monitor.capture_message(f"event from {user_id}", level="error")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    mw = OneloMonitorASGIMiddleware(app)

    async def runner() -> None:
        await asyncio.gather(*[
            _drive_async(mw, _http_scope(path=f"/{name}"))
            for name in ("a", "b", "c")
        ])

    asyncio.run(runner())

    _capture.set_transport(None)
    reset_for_tests()

    by_user = {e.user_id: e for e in sink}
    assert set(by_user.keys()) == {"a", "b", "c"}
    # Each event must contain exactly its own breadcrumb, no leakage. We
    # match on the exact "crumb for X" suffix to avoid substring false-positives
    # (the literal `c` appears in the word "crumb" itself).
    for uid, ev in by_user.items():
        crumbs = ev.meta.get("breadcrumbs", [])
        crumb_messages = [c["message"] for c in crumbs]
        own_marker = f"crumb for {uid}"
        assert own_marker in crumb_messages, (uid, crumb_messages)
        for other in {"a", "b", "c"} - {uid}:
            other_marker = f"crumb for {other}"
            assert other_marker not in crumb_messages, (uid, other, crumb_messages)


async def _drive_async(app: Any, scope: dict[str, Any]) -> None:
    incoming = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(_msg: dict[str, Any]) -> None:
        pass

    await app(scope, receive, send)


def test_asgi_middleware_passes_through_websocket(captured: list[MonitorEvent]) -> None:
    """Non-http scopes (websocket, lifespan) must not crash and must not
    fork an isolation scope (we'd leak it because there's no "end of request")."""
    from onelo.monitor.integrations import OneloMonitorASGIMiddleware

    called = []

    async def app(scope: dict, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    middleware = OneloMonitorASGIMiddleware(app)
    asyncio.run(middleware({"type": "websocket"}, _noop_receive, _noop_send))
    asyncio.run(middleware({"type": "lifespan"}, _noop_receive, _noop_send))
    assert called == ["websocket", "lifespan"]


async def _noop_receive() -> dict[str, Any]:
    return {"type": "noop"}


async def _noop_send(_msg: dict[str, Any]) -> None:
    pass


# ─── WSGI ──────────────────────────────────────────────────────────────────


def test_wsgi_middleware_captures_exception_and_reraises(captured: list[MonitorEvent]) -> None:
    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    def boom_app(environ: dict, start_response: Any) -> Any:
        raise RuntimeError("wsgi boom")

    mw = OneloMonitorWSGIMiddleware(boom_app)
    environ = _wsgi_environ()

    # __call__ now returns a generator (streaming); the app runs — and raises —
    # when the WSGI server iterates the response, so drive it with list().
    with pytest.raises(RuntimeError, match="wsgi boom"):
        list(mw(environ, _wsgi_start))

    assert len(captured) == 1
    event = captured[0]
    assert event.error == "wsgi boom"
    assert event.meta["contexts"]["request"]["method"] == "POST"


def test_wsgi_middleware_streams_body_lazily_and_closes(captured: list[MonitorEvent]) -> None:
    """The response body must be streamed chunk-by-chunk (not list()-materialised)
    and the underlying iterable's close() honoured — so SSE / large downloads work."""
    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    produced: list[bytes] = []
    closed: list[bool] = []

    class StreamingBody:
        def __init__(self) -> None:
            self._chunks = [b"a", b"b", b"c"]
            self._i = 0

        def __iter__(self) -> "StreamingBody":
            return self

        def __next__(self) -> bytes:
            if self._i >= len(self._chunks):
                raise StopIteration
            chunk = self._chunks[self._i]
            self._i += 1
            produced.append(chunk)
            return chunk

        def close(self) -> None:
            closed.append(True)

    def app(environ: dict, start_response: Any) -> Any:
        start_response("200 OK", [])
        return StreamingBody()

    resp = OneloMonitorWSGIMiddleware(app)(_wsgi_environ(), _wsgi_start)
    # Calling the middleware must NOT consume the body (generator is lazy).
    assert produced == []
    it = iter(resp)
    assert next(it) == b"a"
    assert produced == [b"a"]  # only one chunk pulled — genuinely streaming
    assert next(it) == b"b"
    assert next(it) == b"c"
    with pytest.raises(StopIteration):
        next(it)
    assert closed == [True]  # WSGI close() contract honoured


def test_wsgi_middleware_captures_exception_raised_mid_stream(
    captured: list[MonitorEvent],
) -> None:
    """An exception raised WHILE producing the body (not just at call time) is
    captured and re-raised — only possible because we stream inside the scope."""
    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    def app(environ: dict, start_response: Any) -> Any:
        start_response("200 OK", [])

        def gen() -> Any:
            yield b"ok"
            raise RuntimeError("stream broke")

        return gen()

    it = iter(OneloMonitorWSGIMiddleware(app)(_wsgi_environ(), _wsgi_start))
    assert next(it) == b"ok"
    with pytest.raises(RuntimeError, match="stream broke"):
        next(it)

    assert len(captured) == 1
    assert captured[0].error == "stream broke"


def test_wsgi_middleware_tags_status_code(captured: list[MonitorEvent]) -> None:
    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    def app(environ: dict, start_response: Any) -> Any:
        start_response("404 Not Found", [])
        # Capture inside the request to see the tagged status.
        monitor.capture_message("post-404", level="error")
        return [b""]

    mw = OneloMonitorWSGIMiddleware(app)
    list(mw(_wsgi_environ(), _wsgi_start))

    assert captured[0].meta["tags"]["http.status_code"] == "404"


def _wsgi_environ() -> dict[str, Any]:
    return {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/api/x",
        "QUERY_STRING": "token=secret&keep=1",
        "SERVER_NAME": "testserver",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        "HTTP_AUTHORIZATION": "Bearer xyz",
        "HTTP_HOST": "testserver",
    }


def _wsgi_start(_status: str, *_args: Any, **_kwargs: Any) -> Any:
    return None


def test_wsgi_url_redacts_sensitive_query() -> None:
    """End-to-end: the request context attached during a WSGI call must
    contain a scrubbed URL."""
    sink: list[MonitorEvent] = []
    _capture.set_transport(sink.append)
    reset_for_tests()

    from onelo.monitor.integrations import OneloMonitorWSGIMiddleware

    def app(environ: dict, start_response: Any) -> Any:
        start_response("200 OK", [])
        monitor.capture_message("seen", level="error")
        return [b""]

    list(OneloMonitorWSGIMiddleware(app)(_wsgi_environ(), _wsgi_start))

    _capture.set_transport(None)
    reset_for_tests()

    url = sink[0].meta["contexts"]["request"]["url"]
    assert "token=" + REDACTED in url or "token=%5BREDACTED%5D" in url
    assert "keep=1" in url


# ─── Django (skipped if not installed) ─────────────────────────────────────

django = pytest.importorskip("django")


def test_django_middleware_captures_via_process_exception(captured: list[MonitorEvent]) -> None:
    """Django's `process_exception` is the canonical capture hook."""
    from onelo.monitor.integrations.django import OneloMonitorMiddleware

    class FakeRequest:
        method = "GET"
        path = "/api/x"
        headers: dict[str, str] = {"X-Test": "1", "Authorization": "Bearer s"}

        def build_absolute_uri(self) -> str:
            return "https://app/api/x?token=secret"

    middleware = OneloMonitorMiddleware(get_response=lambda r: None)
    with isolation_scope():
        middleware.process_exception(FakeRequest(), ValueError("django boom"))

    # Note: process_exception runs OUTSIDE the middleware's `__call__` block,
    # so an event captured here uses whatever isolation scope is active.
    assert len(captured) == 1
    assert captured[0].error == "django boom"
    assert captured[0].meta["error_type"] == "ValueError"


# ─── Flask (skipped if not installed) ──────────────────────────────────────

flask = pytest.importorskip("flask")


def test_flask_install_captures_exception(captured: list[MonitorEvent]) -> None:
    from flask import Flask

    from onelo.monitor.integrations.flask import install

    app = Flask(__name__)

    @app.get("/boom")
    def _boom() -> Any:
        raise RuntimeError("flask boom")

    install(app)
    client = app.test_client()
    # Flask wraps the exception into a 500 by default — we just need to know
    # the exception fired so our got_request_exception handler ran.
    response = client.get("/boom?token=hidden&keep=1")
    assert response.status_code == 500

    # We expect at least one event with our error.
    boom_events = [e for e in captured if e.error == "flask boom"]
    assert boom_events, [e.error for e in captured]
    ev = boom_events[0]
    assert ev.feature_name == "http_request"
    # URL scrubbing happened
    url = ev.meta["contexts"]["request"]["url"]
    assert "token=" + REDACTED in url or "token=%5BREDACTED%5D" in url
