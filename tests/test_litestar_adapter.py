"""Tests for the Litestar auth integration (onelo.litestar).

Skipped entirely when the [litestar] extra is not installed.
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

pytest.importorskip("litestar")

from litestar import Litestar, get  # noqa: E402
from litestar.testing import TestClient  # noqa: E402

from onelo import Onelo, OneloUser  # noqa: E402
from onelo.litestar import (  # noqa: E402
    OneloGuardFactory,
    provide_onelo_user,
    provide_optional_onelo_user,
)


SAMPLE_PAYLOAD = {
    "id": "user-123",
    "email": "alice@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class AuthTransport(httpx.MockTransport):
    """MockTransport that scripts /api/sdk/auth/user responses and
    silently swallows the SSE/poll calls fired by the background thread.
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
                return httpx.Response(
                    500, json={"error": "script exhausted"}
                )
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


def _make_publishable_client() -> Onelo:
    return Onelo(
        publishable_key="onelo_pk_test_abcdef",
        api_url="https://example.com",
    )


# ── Guard tests ─────────────────────────────────────────────────────────


def test_guard_valid_token_populates_connection_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    guard = OneloGuardFactory(client)

    @get("/me")
    async def me(request: Any) -> dict:
        u = request.scope["user"]
        return {"id": u.id, "email": u.email}

    app = Litestar(route_handlers=[me], guards=[guard])
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me", headers={"Authorization": "Bearer good"})
            assert r.status_code == 200
            assert r.json() == {
                "id": "user-123", "email": "alice@example.com"
            }
    finally:
        client.close()


def test_guard_missing_token_returns_401():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    guard = OneloGuardFactory(client)

    @get("/me")
    async def me() -> dict:
        return {}

    app = Litestar(route_handlers=[me], guards=[guard])
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me")
            assert r.status_code == 401
    finally:
        client.close()


def test_guard_invalid_token_returns_401():
    transport = AuthTransport([(401, {"error": "invalid"})])
    client = _make_secret_client(transport)
    guard = OneloGuardFactory(client)

    @get("/me")
    async def me() -> dict:
        return {}

    app = Litestar(route_handlers=[me], guards=[guard])
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me", headers={"Authorization": "Bearer bad"})
            assert r.status_code == 401
    finally:
        client.close()


def test_guard_forbidden_returns_403():
    # Backend returns 200 but plan gate fails.
    payload = {
        "id": "u1", "email": "x@y.z",
        "metadata": {"plan": "free"},
    }
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    guard = OneloGuardFactory(client, require_plan=["pro"])

    @get("/me")
    async def me() -> dict:
        return {}

    app = Litestar(route_handlers=[me], guards=[guard])
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me", headers={"Authorization": "Bearer t"})
            assert r.status_code == 403
    finally:
        client.close()


def test_guard_5xx_returns_503():
    transport = AuthTransport([(500, None), (500, None), (500, None)])
    client = _make_secret_client(transport)
    guard = OneloGuardFactory(
        client, retry_attempts=3, retry_total_timeout=0.5
    )

    @get("/me")
    async def me() -> dict:
        return {}

    app = Litestar(route_handlers=[me], guards=[guard])
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me", headers={"Authorization": "Bearer t"})
            assert r.status_code == 503
    finally:
        client.close()


# ── Dependency injection tests ──────────────────────────────────────────


def test_provide_valid_token_injects_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)

    @get("/me")
    async def me(user: OneloUser) -> dict:
        return {"id": user.id, "email": user.email}

    app = Litestar(
        route_handlers=[me],
        dependencies={"user": provide_onelo_user(client)},
    )
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me", headers={"Authorization": "Bearer good"})
            assert r.status_code == 200
            assert r.json()["id"] == "user-123"
    finally:
        client.close()


def test_provide_cache_hit_avoids_second_call():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)

    @get("/me")
    async def me(user: OneloUser) -> dict:
        return {"id": user.id}

    app = Litestar(
        route_handlers=[me],
        dependencies={
            "user": provide_onelo_user(client, cache_ttl=60.0),
        },
    )
    try:
        with TestClient(app=app) as tc:
            r1 = tc.get("/me", headers={"Authorization": "Bearer same"})
            r2 = tc.get("/me", headers={"Authorization": "Bearer same"})
            assert r1.status_code == 200
            assert r2.status_code == 200
            # Only 1 call to the backend despite 2 requests.
            assert len(transport.user_endpoint_calls) == 1
    finally:
        client.close()


def test_provide_ttl_expiry_triggers_refetch():
    transport = AuthTransport([
        (200, SAMPLE_PAYLOAD),
        (200, SAMPLE_PAYLOAD),
    ])
    client = _make_secret_client(transport)

    @get("/me")
    async def me(user: OneloUser) -> dict:
        return {"id": user.id}

    # Very short TTL — 0.05s — first call caches, second call after
    # sleep should re-verify.
    app = Litestar(
        route_handlers=[me],
        dependencies={
            "user": provide_onelo_user(client, cache_ttl=0.05),
        },
    )
    try:
        with TestClient(app=app) as tc:
            r1 = tc.get("/me", headers={"Authorization": "Bearer same"})
            assert r1.status_code == 200
            time.sleep(0.1)
            r2 = tc.get("/me", headers={"Authorization": "Bearer same"})
            assert r2.status_code == 200
            assert len(transport.user_endpoint_calls) == 2
    finally:
        client.close()


def test_provide_optional_returns_none_on_missing():
    transport = AuthTransport([])
    client = _make_secret_client(transport)

    @get("/me")
    async def me(user: OneloUser | None) -> dict:
        return {"present": user is not None}

    app = Litestar(
        route_handlers=[me],
        dependencies={"user": provide_optional_onelo_user(client)},
    )
    try:
        with TestClient(app=app) as tc:
            r = tc.get("/me")
            assert r.status_code == 200
            assert r.json() == {"present": False}
    finally:
        client.close()


# ── Construction guards ────────────────────────────────────────────────


def test_guard_factory_rejects_publishable_key():
    client = _make_publishable_client()
    try:
        with pytest.raises(ValueError, match="secret_key"):
            OneloGuardFactory(client)
    finally:
        client.close()


def test_provide_rejects_publishable_key():
    client = _make_publishable_client()
    try:
        with pytest.raises(ValueError, match="secret_key"):
            provide_onelo_user(client)
    finally:
        client.close()
