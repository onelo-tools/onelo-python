"""Tests for the universal WSGI middleware (onelo.wsgi).

These tests use raw WSGI environ dicts — no werkzeug dependency.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from onelo import Onelo, OneloUser
from onelo.wsgi import OneloWsgiMiddleware


SAMPLE_PAYLOAD = {
    "id": "user-wsgi-1",
    "email": "wsgi@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class SyncAuthTransport(httpx.MockTransport):
    """Sync-capable mock transport (httpx.MockTransport supports both)."""

    def __init__(self, script: list[tuple[int, Any]]):
        self._script = list(script)
        self.calls: list[httpx.Request] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sdk/auth/user":
            self.calls.append(request)
            if not self._script:
                return httpx.Response(500, json={"error": "exhausted"})
            status, body = self._script.pop(0)
            if body is None:
                return httpx.Response(status)
            return httpx.Response(status, json=body)
        if path == "/api/sdk/features/stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='event: up_to_date\ndata: {"config_version": 0}\n\n',
            )
        if path == "/api/sdk/features/poll":
            return httpx.Response(304)
        return httpx.Response(404, json={"error": f"unmocked {path}"})


def _make_client(transport: httpx.MockTransport) -> Onelo:
    return Onelo(
        secret_key="onelo_sk_test_abcdef",
        api_url="https://example.com",
        transport=transport,
    )


def _make_stub_app(captured: dict[str, Any]):
    def app(environ, start_response):
        captured["environ"] = environ
        captured["user"] = environ.get("onelo.user")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    return app


def _invoke(middleware, *, authorization: str | None = None):
    """Drive a WSGI middleware once with a synthetic environ."""
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/me",
        "QUERY_STRING": "",
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
    }
    if authorization is not None:
        environ["HTTP_AUTHORIZATION"] = authorization

    status_calls: list[tuple[str, list[tuple[str, str]]]] = []

    def start_response(status, headers, exc_info=None):
        status_calls.append((status, headers))

        def _write(_):  # pragma: no cover
            pass

        return _write

    body = list(middleware(environ, start_response))
    return environ, status_calls, body


# ── Tests ────────────────────────────────────────────────────────────────


def test_rejects_publishable_key():
    client = Onelo(
        publishable_key="onelo_pk_test_abcdef",
        api_url="https://example.com",
        transport=SyncAuthTransport([]),
    )
    with pytest.raises(ValueError, match="secret_key"):
        OneloWsgiMiddleware(lambda e, s: [b""], onelo=client)


def test_missing_header_sets_user_none():
    transport = SyncAuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)

    environ, status, body = _invoke(mw)

    assert environ["onelo.user"] is None
    assert captured["user"] is None
    assert transport.calls == []
    assert status[0][0] == "200 OK"
    assert body == [b"ok"]


def test_valid_token_populates_user():
    transport = SyncAuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)

    environ, _status, _body = _invoke(mw, authorization="Bearer good-token")

    user = environ["onelo.user"]
    assert isinstance(user, OneloUser)
    assert user.id == "user-wsgi-1"
    assert user.email == "wsgi@example.com"
    assert len(transport.calls) == 1


def test_invalid_token_sets_user_none_no_raise():
    transport = SyncAuthTransport([(401, {"detail": "bad"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)

    environ, status, body = _invoke(mw, authorization="Bearer bad-token")

    assert environ["onelo.user"] is None
    assert status[0][0] == "200 OK"
    assert body == [b"ok"]


def test_non_bearer_header_treated_as_missing():
    transport = SyncAuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)

    environ, _, _ = _invoke(mw, authorization="Basic dXNlcjpwYXNz")

    assert environ["onelo.user"] is None
    assert transport.calls == []


def test_cache_hit_does_not_call_backend_twice():
    transport = SyncAuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(
        _make_stub_app(captured), onelo=client, cache_ttl=60.0
    )

    e1, _, _ = _invoke(mw, authorization="Bearer same-token")
    e2, _, _ = _invoke(mw, authorization="Bearer same-token")

    assert isinstance(e1["onelo.user"], OneloUser)
    assert isinstance(e2["onelo.user"], OneloUser)
    assert e1["onelo.user"].id == e2["onelo.user"].id
    assert len(transport.calls) == 1


def test_on_auth_event_called():
    events: list[tuple[str | None, str]] = []

    def cb(uid, ev):
        events.append((uid, ev))

    transport = SyncAuthTransport(
        [(200, SAMPLE_PAYLOAD), (401, {"detail": "x"})]
    )
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(
        _make_stub_app(captured), onelo=client, on_auth_event=cb
    )

    _invoke(mw, authorization="Bearer good")
    _invoke(mw, authorization="Bearer bad")
    _invoke(mw)

    kinds = [e[1] for e in events]
    assert any(k == "auth.verify" for k in kinds)
    assert any(k.startswith("auth.fail.") for k in kinds)
    assert "auth.fail.missing_token" in kinds


def test_backend_5xx_yields_user_none_after_retries():
    transport = SyncAuthTransport([(503, None), (503, None), (503, None)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(
        _make_stub_app(captured),
        onelo=client,
        retry_attempts=3,
        retry_total_timeout=0.5,
    )

    environ, status, body = _invoke(mw, authorization="Bearer t")
    assert environ["onelo.user"] is None
    assert status[0][0] == "200 OK"


def test_concurrent_invocations_thread_safety():
    """Hammer the middleware from multiple threads — cache uses
    threading.Lock so no race / KeyError should occur."""
    import threading

    transport = SyncAuthTransport([(200, SAMPLE_PAYLOAD)] * 50)
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(
        _make_stub_app(captured), onelo=client, cache_ttl=60.0
    )

    errors: list[BaseException] = []

    def worker(idx: int):
        try:
            _invoke(mw, authorization=f"Bearer token-{idx % 5}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []


def test_wsgi_flags_backend_unavailable_distinctly():
    """A1: backend unreachable → onelo.auth_unavailable=True (not silently
    anonymous)."""
    transport = SyncAuthTransport([(503, {"error": "down"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client, retry_attempts=1)
    try:
        environ, _, _ = _invoke(mw, authorization="Bearer tok")
        assert environ["onelo.user"] is None
        assert environ["onelo.auth_unavailable"] is True
    finally:
        client.close()


def test_wsgi_invalid_token_not_flagged_unavailable():
    transport = SyncAuthTransport([(401, {"error": "bad"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)
    try:
        environ, _, _ = _invoke(mw, authorization="Bearer tok")
        assert environ["onelo.user"] is None
        assert environ["onelo.auth_unavailable"] is False
    finally:
        client.close()


def test_wsgi_missing_token_not_flagged_unavailable():
    transport = SyncAuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)
    try:
        environ, _, _ = _invoke(mw, authorization=None)
        assert environ["onelo.user"] is None
        assert environ["onelo.auth_unavailable"] is False
    finally:
        client.close()


def _invoke_environ(middleware, environ_extra):
    environ: dict[str, Any] = {
        "REQUEST_METHOD": "GET", "PATH_INFO": "/sse", "QUERY_STRING": "",
        "SERVER_NAME": "test", "SERVER_PORT": "80", "wsgi.url_scheme": "http",
    }
    environ.update(environ_extra)
    def start_response(status, headers, exc_info=None):
        return lambda _: None
    body = list(middleware(environ, start_response))
    return environ, body


def test_wsgi_accepts_query_token_when_enabled():
    transport = SyncAuthTransport([(200, {"id": "user-wsgi-q", "email": "q@e.z", "metadata": {}})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client, accept_query_token=True)
    try:
        environ, _ = _invoke_environ(mw, {"QUERY_STRING": "token=abc.def.ghi&x=1"})
        assert environ["onelo.user"] is not None
        assert environ["onelo.user"].id == "user-wsgi-q"
    finally:
        client.close()


def test_wsgi_ignores_query_token_by_default():
    transport = SyncAuthTransport([(200, {"id": "u", "email": "e", "metadata": {}})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloWsgiMiddleware(_make_stub_app(captured), onelo=client)  # disabled
    try:
        environ, _ = _invoke_environ(mw, {"QUERY_STRING": "token=abc.def.ghi"})
        assert environ["onelo.user"] is None
        assert transport.calls == []
    finally:
        client.close()
