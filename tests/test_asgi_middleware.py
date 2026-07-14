"""Tests for the universal ASGI middleware (onelo.asgi)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from onelo import Onelo, OneloUser
from onelo.asgi import OneloAsgiMiddleware


SAMPLE_PAYLOAD = {
    "id": "user-asgi-1",
    "email": "asgi@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class AuthTransport(httpx.MockTransport):
    def __init__(self, script: list[tuple[int, Any]]):
        self._script = list(script)
        self.calls: list[httpx.Request] = []
        super().__init__(self._handler)

    async def _handler(self, request: httpx.Request) -> httpx.Response:
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
    """A trivial ASGI app that captures scope and replies 200 OK."""

    async def app(scope, receive, send):
        captured["scope"] = scope
        captured["onelo_user"] = scope.get("onelo_user")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    return app


async def _invoke(middleware, headers: list[tuple[bytes, bytes]]):
    """Drive an ASGI middleware once with a synthetic HTTP scope."""
    sent: list[dict[str, Any]] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/me",
        "raw_path": b"/me",
        "query_string": b"",
        "headers": headers,
    }
    await middleware(scope, receive, send)
    return scope, sent


# ── Tests ────────────────────────────────────────────────────────────────


def test_rejects_publishable_key():
    client = Onelo(
        publishable_key="onelo_pk_test_abcdef",
        api_url="https://example.com",
        transport=AuthTransport([]),
    )
    with pytest.raises(ValueError, match="secret_key"):
        OneloAsgiMiddleware(lambda *a, **kw: None, onelo=client)


@pytest.mark.asyncio
async def test_missing_header_sets_user_none():
    transport = AuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)

    scope, sent = await _invoke(mw, headers=[])

    assert scope["onelo_user"] is None
    assert captured["onelo_user"] is None
    assert transport.calls == []
    # Response still flows through.
    assert any(m["type"] == "http.response.start" for m in sent)


@pytest.mark.asyncio
async def test_valid_token_populates_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)

    scope, _sent = await _invoke(
        mw, headers=[(b"authorization", b"Bearer good-token")]
    )

    user = scope["onelo_user"]
    assert isinstance(user, OneloUser)
    assert user.id == "user-asgi-1"
    assert user.email == "asgi@example.com"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_invalid_token_sets_user_none_no_raise():
    transport = AuthTransport([(401, {"detail": "bad"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)

    scope, sent = await _invoke(
        mw, headers=[(b"authorization", b"Bearer bad-token")]
    )

    assert scope["onelo_user"] is None
    # Middleware swallowed the failure — downstream app still ran.
    assert any(m["type"] == "http.response.start" for m in sent)


@pytest.mark.asyncio
async def test_non_bearer_header_treated_as_missing():
    transport = AuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)

    scope, _ = await _invoke(
        mw, headers=[(b"authorization", b"Basic dXNlcjpwYXNz")]
    )

    assert scope["onelo_user"] is None
    assert transport.calls == []


@pytest.mark.asyncio
async def test_cache_hit_does_not_call_backend_twice():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])  # one response only
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client, cache_ttl=60.0)

    headers = [(b"authorization", b"Bearer same-token")]
    s1, _ = await _invoke(mw, headers=headers)
    s2, _ = await _invoke(mw, headers=headers)

    assert isinstance(s1["onelo_user"], OneloUser)
    assert isinstance(s2["onelo_user"], OneloUser)
    assert s1["onelo_user"].id == s2["onelo_user"].id
    assert len(transport.calls) == 1  # second call cached


@pytest.mark.asyncio
async def test_websocket_scope_passes_through_unchanged():
    transport = AuthTransport([])
    client = _make_client(transport)

    invoked = {"count": 0}

    async def app(scope, receive, send):
        invoked["count"] += 1
        # Should not have onelo_user injected for non-http scopes.
        assert "onelo_user" not in scope

    mw = OneloAsgiMiddleware(app, onelo=client)

    async def receive():
        return {"type": "websocket.connect"}

    async def send(_msg):
        pass

    scope = {"type": "websocket", "headers": [(b"authorization", b"Bearer x")]}
    await mw(scope, receive, send)

    assert invoked["count"] == 1
    assert transport.calls == []


@pytest.mark.asyncio
async def test_on_auth_event_called_for_success_and_failure():
    events: list[tuple[str | None, str]] = []

    def cb(uid, ev):
        events.append((uid, ev))

    transport = AuthTransport(
        [(200, SAMPLE_PAYLOAD), (401, {"detail": "x"})]
    )
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(
        _make_stub_app(captured), onelo=client, on_auth_event=cb
    )

    await _invoke(mw, headers=[(b"authorization", b"Bearer good")])
    await _invoke(mw, headers=[(b"authorization", b"Bearer bad")])
    await _invoke(mw, headers=[])

    kinds = [e[1] for e in events]
    assert any(k == "auth.verify" for k in kinds)
    assert any(k.startswith("auth.fail.") for k in kinds)
    assert "auth.fail.missing_token" in kinds


@pytest.mark.asyncio
async def test_backend_5xx_yields_user_none_after_retries():
    # Three 5xx responses — exhaust retries, fall back to None.
    transport = AuthTransport([(503, None), (503, None), (503, None)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(
        _make_stub_app(captured),
        onelo=client,
        retry_attempts=3,
        retry_total_timeout=0.5,
    )

    scope, _ = await _invoke(
        mw, headers=[(b"authorization", b"Bearer t")]
    )
    assert scope["onelo_user"] is None


@pytest.mark.asyncio
async def test_asgi_flags_backend_unavailable_distinctly():
    """A1: backend unreachable must NOT look like 'anonymous' — the middleware
    sets onelo_auth_unavailable=True so the app can 503 instead of serving the
    request unauthenticated."""
    transport = AuthTransport([(503, {"error": "down"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client, retry_attempts=1)
    try:
        scope, _ = await _invoke(mw, headers=[(b"authorization", b"Bearer tok")])
        assert scope["onelo_user"] is None
        assert scope["onelo_auth_unavailable"] is True
    finally:
        client.close()


@pytest.mark.asyncio
async def test_asgi_invalid_token_not_flagged_unavailable():
    """An invalid token is a real verdict, not an outage — flag stays False."""
    transport = AuthTransport([(401, {"error": "bad"})])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)
    try:
        scope, _ = await _invoke(mw, headers=[(b"authorization", b"Bearer tok")])
        assert scope["onelo_user"] is None
        assert scope["onelo_auth_unavailable"] is False
    finally:
        client.close()


@pytest.mark.asyncio
async def test_asgi_missing_token_not_flagged_unavailable():
    transport = AuthTransport([])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)
    try:
        scope, _ = await _invoke(mw, headers=[])
        assert scope["onelo_user"] is None
        assert scope["onelo_auth_unavailable"] is False
    finally:
        client.close()


async def _invoke_scope(middleware, scope):
    sent: list[dict[str, Any]] = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(msg):
        sent.append(msg)
    await middleware(scope, receive, send)
    return scope, sent


@pytest.mark.asyncio
async def test_asgi_accepts_query_token_when_enabled():
    """A4: with accept_query_token, an SSE/EventSource request (no auth header)
    can authenticate via ?token=."""
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client, accept_query_token=True)
    try:
        scope = {"type": "http", "method": "GET", "path": "/sse",
                 "query_string": b"token=abc.def.ghi&x=1", "headers": []}
        scope, _ = await _invoke_scope(mw, scope)
        assert scope["onelo_user"] is not None
        assert scope["onelo_user"].id == "user-asgi-1"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_asgi_ignores_query_token_by_default():
    """Opt-in: without accept_query_token, ?token= is ignored (no verification)."""
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_client(transport)
    captured: dict[str, Any] = {}
    mw = OneloAsgiMiddleware(_make_stub_app(captured), onelo=client)  # disabled
    try:
        scope = {"type": "http", "method": "GET", "path": "/sse",
                 "query_string": b"token=abc.def.ghi", "headers": []}
        scope, _ = await _invoke_scope(mw, scope)
        assert scope["onelo_user"] is None
        assert transport.calls == []  # never even attempted a network verify
    finally:
        client.close()
