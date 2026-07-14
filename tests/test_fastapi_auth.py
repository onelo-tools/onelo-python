"""Tests for the FastAPI auth integration (onelo.fastapi).

Skipped entirely when the [fastapi] extra is not installed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from onelo import (  # noqa: E402
    Onelo,
    OneloAuthForbidden,
    OneloAuthInvalidToken,
    OneloAuthUnavailable,
    OneloUser,
    verify_token,
)
from onelo._auth_cache import InProcessAuthCache  # noqa: E402
from onelo.fastapi import OptionalUser, RequireUser  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────


SAMPLE_PAYLOAD = {
    "id": "user-123",
    "email": "alice@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class AuthTransport(httpx.MockTransport):
    """A MockTransport that also routes the SSE/poll endpoints used by Onelo
    so the background thread doesn't spam errors while tests run.

    The script_for_user_endpoint controls /api/sdk/auth/user responses.
    Each tuple is (status_code, json_body or None).
    """

    def __init__(self, user_endpoint_script: list[tuple[int, Any]]):
        self._script = list(user_endpoint_script)
        self.user_endpoint_calls: list[httpx.Request] = []
        super().__init__(self._handler)

    async def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sdk/auth/user":
            self.user_endpoint_calls.append(request)
            if not self._script:
                return httpx.Response(500, json={"error": "script exhausted"})
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


def _make_secret_client(transport: httpx.MockTransport) -> Onelo:
    return Onelo(
        secret_key="onelo_sk_test_abcdef",
        api_url="https://example.com",
        transport=transport,
    )


# ── verify_token (low-level) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_token_happy_path():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        user = await verify_token(client, "abc.def.ghi")
        assert isinstance(user, OneloUser)
        assert user.id == "user-123"
        assert user.email == "alice@example.com"
        assert user.email_verified is False  # default when missing
        assert user.plan is None
        assert user.raw == SAMPLE_PAYLOAD
        # Verify auth headers were sent.
        req = transport.user_endpoint_calls[0]
        assert req.headers["authorization"] == "Bearer abc.def.ghi"
        assert req.headers["x-onelo-secret-key"] == "onelo_sk_test_abcdef"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_verify_token_reads_email_verified_and_plan_from_metadata():
    payload = {
        "id": "u1",
        "email": "x@y.z",
        "metadata": {"email_verified": True, "plan": "pro"},
    }
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    try:
        user = await verify_token(client, "tok")
        assert user.email_verified is True
        assert user.plan == "pro"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_verify_token_401_raises_invalid():
    transport = AuthTransport([(401, {"detail": "bad"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthInvalidToken):
            await verify_token(client, "tok")
    finally:
        client.close()


@pytest.mark.asyncio
async def test_verify_token_403_raises_forbidden():
    transport = AuthTransport([(403, {"detail": "nope"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthForbidden):
            await verify_token(client, "tok")
    finally:
        client.close()


@pytest.mark.asyncio
async def test_verify_token_5xx_raises_unavailable():
    transport = AuthTransport([(503, {"detail": "down"})])
    client = _make_secret_client(transport)
    try:
        with pytest.raises(OneloAuthUnavailable):
            await verify_token(client, "tok")
    finally:
        client.close()


# ── RequireUser construction ─────────────────────────────────────────────


def test_require_user_rejects_publishable_key_client():
    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=AuthTransport([]),
    )
    try:
        with pytest.raises(ValueError, match="secret_key"):
            RequireUser(onelo)
    finally:
        onelo.close()


# ── RequireUser request handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_user_missing_authorization_returns_401():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client)
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization=None, token=None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == {"error": "missing_token"}
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_query_token_when_enabled():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        dep = RequireUser(client, accept_query_token=True)
        user = await dep(authorization=None, token="abc")
        assert user.id == "user-123"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_query_token_ignored_when_disabled():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client)  # accept_query_token=False default
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization=None, token="abc")
        assert excinfo.value.status_code == 401
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_invalid_token_returns_401():
    transport = AuthTransport([(401, {})])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client)
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization="Bearer bad", token=None)
        assert excinfo.value.status_code == 401
        assert excinfo.value.detail == {"error": "invalid_token"}
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_cache_hit_avoids_second_call():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])  # only ONE response
    client = _make_secret_client(transport)
    try:
        dep = RequireUser(client, cache_ttl=60.0)
        u1 = await dep(authorization="Bearer same.token", token=None)
        u2 = await dep(authorization="Bearer same.token", token=None)
        assert u1.id == u2.id == "user-123"
        # Second call should NOT have hit the network.
        assert len(transport.user_endpoint_calls) == 1
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_cache_ttl_expiry(monkeypatch):
    transport = AuthTransport([(200, SAMPLE_PAYLOAD), (200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        dep = RequireUser(client, cache_ttl=10.0)
        await dep(authorization="Bearer t", token=None)
        assert len(transport.user_endpoint_calls) == 1

        # Fast-forward monotonic time past the TTL so the cache evicts.
        import time as _time
        real_monotonic = _time.monotonic
        offset = 1000.0
        monkeypatch.setattr(
            "onelo._auth_cache.time.monotonic",
            lambda: real_monotonic() + offset,
        )

        await dep(authorization="Bearer t", token=None)
        assert len(transport.user_endpoint_calls) == 2
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_email_verified_gate():
    payload = dict(SAMPLE_PAYLOAD, email_verified=False)
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client, require_email_verified=True)
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization="Bearer t", token=None)
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["reason"] == "email_unverified"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_plan_gate():
    payload = dict(SAMPLE_PAYLOAD, plan="free")
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client, require_plan=["pro", "business"])
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization="Bearer t", token=None)
        assert excinfo.value.status_code == 403
        assert excinfo.value.detail["reason"] == "plan"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_retry_succeeds_on_503_503_200():
    transport = AuthTransport(
        [(503, {}), (503, {}), (200, SAMPLE_PAYLOAD)]
    )
    client = _make_secret_client(transport)
    try:
        dep = RequireUser(
            client,
            retry_attempts=3,
            retry_total_timeout=5.0,
        )
        user = await dep(authorization="Bearer t", token=None)
        assert user.id == "user-123"
        assert len(transport.user_endpoint_calls) == 3
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_retry_exhausted_returns_503():
    transport = AuthTransport([(503, {}), (503, {}), (503, {})])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(
            client,
            retry_attempts=3,
            retry_total_timeout=5.0,
        )
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization="Bearer t", token=None)
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == {"error": "auth_service_unavailable"}
        assert len(transport.user_endpoint_calls) == 3
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_on_auth_event_success_and_failure():
    events: list[tuple[Any, str]] = []

    transport = AuthTransport([(200, SAMPLE_PAYLOAD), (401, {})])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = RequireUser(client, on_auth_event=lambda uid, ev: events.append((uid, ev)))
        # success
        await dep(authorization="Bearer good", token=None)
        # invalid
        with pytest.raises(HTTPException):
            await dep(authorization="Bearer bad", token=None)

        kinds = [ev for _, ev in events]
        assert "auth.verify" in kinds
        assert any(k.startswith("auth.fail.") for k in kinds)
        # success event should carry user id
        succ = next(e for e in events if e[1] == "auth.verify")
        assert succ[0] == "user-123"
    finally:
        client.close()


@pytest.mark.asyncio
async def test_require_user_on_auth_event_callback_exception_swallowed():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)

    def boom(uid, ev):
        raise RuntimeError("kaboom")

    try:
        dep = RequireUser(client, on_auth_event=boom)
        # Must NOT raise.
        user = await dep(authorization="Bearer t", token=None)
        assert user.id == "user-123"
    finally:
        client.close()


# ── OptionalUser ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_optional_user_missing_token_returns_none():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    try:
        dep = OptionalUser(client)
        assert await dep(authorization=None, token=None) is None
    finally:
        client.close()


@pytest.mark.asyncio
async def test_optional_user_invalid_token_returns_none():
    transport = AuthTransport([(401, {})])
    client = _make_secret_client(transport)
    try:
        dep = OptionalUser(client)
        assert await dep(authorization="Bearer bad", token=None) is None
    finally:
        client.close()


@pytest.mark.asyncio
async def test_optional_user_5xx_still_raises_503():
    transport = AuthTransport([(503, {}), (503, {}), (503, {})])
    client = _make_secret_client(transport)
    from fastapi import HTTPException

    try:
        dep = OptionalUser(client, retry_attempts=3, retry_total_timeout=5.0)
        with pytest.raises(HTTPException) as excinfo:
            await dep(authorization="Bearer t", token=None)
        assert excinfo.value.status_code == 503
    finally:
        client.close()


# ── InProcessAuthCache primitives ────────────────────────────────────────


@pytest.mark.asyncio
async def test_inprocess_auth_cache_ttl(monkeypatch):
    cache = InProcessAuthCache()
    user = OneloUser(id="u1", email="a@b.c", raw={})
    await cache.set("h", user, ttl=10.0)
    assert (await cache.get("h")).id == "u1"

    import time as _time
    real_mono = _time.monotonic
    monkeypatch.setattr(
        "onelo._auth_cache.time.monotonic",
        lambda: real_mono() + 1000.0,
    )
    assert await cache.get("h") is None


@pytest.mark.asyncio
async def test_inprocess_auth_cache_max_size_eviction():
    cache = InProcessAuthCache(max_size=2)
    u1 = OneloUser(id="1", email="1@x", raw={})
    u2 = OneloUser(id="2", email="2@x", raw={})
    u3 = OneloUser(id="3", email="3@x", raw={})
    await cache.set("a", u1, ttl=60.0)
    await cache.set("b", u2, ttl=60.0)
    await cache.set("c", u3, ttl=60.0)  # evicts oldest ("a")
    assert await cache.get("a") is None
    assert (await cache.get("b")).id == "2"
    assert (await cache.get("c")).id == "3"


@pytest.mark.asyncio
async def test_inprocess_auth_cache_invalidate():
    cache = InProcessAuthCache()
    u = OneloUser(id="1", email="x", raw={})
    await cache.set("h", u, ttl=60.0)
    await cache.invalidate("h")
    assert await cache.get("h") is None
