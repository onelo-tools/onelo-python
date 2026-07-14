"""Tests for the Flask auth integration (onelo.flask).

Skipped entirely when Flask is not installed.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("flask")

from flask import Flask  # noqa: E402

from onelo import Onelo  # noqa: E402
from onelo.auth import OneloAuthUnavailable, OneloUser  # noqa: E402
from onelo.flask import optional_user, require_user  # noqa: E402


SAMPLE_PAYLOAD = {
    "id": "user-123",
    "email": "alice@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class AuthTransport(httpx.MockTransport):
    """Sync MockTransport scripting /api/sdk/auth/user responses."""

    def __init__(self, user_endpoint_script: list[tuple[int, Any]]):
        self._script = list(user_endpoint_script)
        self.user_endpoint_calls: list[httpx.Request] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
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


def _make_app(view_decorator):
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.get("/me")
    @view_decorator
    def me(user):  # type: ignore[no-untyped-def]
        if user is None:
            return {"anon": True}
        return {"id": user.id, "email": user.email}

    return app


# ── Construction ─────────────────────────────────────────────────────────


def test_require_user_rejects_publishable_key_client():
    onelo = Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
        transport=AuthTransport([]),
    )
    try:
        with pytest.raises(ValueError, match="secret_key"):
            require_user(onelo)
    finally:
        onelo.close()


# ── require_user ─────────────────────────────────────────────────────────


def test_require_user_happy_path():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer abc.def"})
        assert r.status_code == 200
        assert r.get_json() == {"id": "user-123", "email": "alice@example.com"}
        sent = transport.user_endpoint_calls[0]
        assert sent.headers["authorization"] == "Bearer abc.def"
        assert sent.headers["x-onelo-secret-key"] == "onelo_sk_test_abcdef"
    finally:
        client.close()


def test_require_user_missing_authorization_returns_401():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client))
        with app.test_client() as c:
            r = c.get("/me")
        assert r.status_code == 401
        assert r.get_json() == {"error": "missing_token"}
        # Network never hit.
        assert transport.user_endpoint_calls == []
    finally:
        client.close()


def test_require_user_non_bearer_scheme_returns_401():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Basic abc"})
        assert r.status_code == 401
        assert r.get_json() == {"error": "missing_token"}
    finally:
        client.close()


def test_require_user_invalid_token_returns_401():
    transport = AuthTransport([(401, {"detail": "bad"})])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer bad"})
        assert r.status_code == 401
        assert r.get_json() == {"error": "invalid_token"}
    finally:
        client.close()


def test_require_user_forbidden_returns_403():
    transport = AuthTransport([(403, {"detail": "no"})])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 403
        assert r.get_json() == {"error": "forbidden"}
    finally:
        client.close()


def test_require_user_5xx_exhausted_returns_503():
    # 3 attempts, all 5xx → unavailable
    transport = AuthTransport([(503, None), (503, None), (503, None)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(
            require_user(client, retry_attempts=3, retry_total_timeout=1.0)
        )
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 503
        assert r.get_json() == {"error": "auth_service_unavailable"}
        # All attempts were made (retry policy honored).
        assert len(transport.user_endpoint_calls) == 3
    finally:
        client.close()


def test_require_email_verified_gate_returns_403():
    payload = dict(SAMPLE_PAYLOAD, email_verified=False)
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client, require_email_verified=True))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 403
        body = r.get_json()
        assert body["error"] == "forbidden"
        assert body["reason"] == "email_unverified"
    finally:
        client.close()


def test_require_plan_gate_returns_403():
    payload = dict(SAMPLE_PAYLOAD, plan="free")
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client, require_plan=["pro"]))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 403
        body = r.get_json()
        assert body["error"] == "forbidden"
        assert body["reason"] == "plan"
    finally:
        client.close()


def test_require_user_cache_hit_avoids_second_call():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])  # only ONE response
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client, cache_ttl=60.0))
        with app.test_client() as c:
            r1 = c.get("/me", headers={"Authorization": "Bearer same.tok"})
            r2 = c.get("/me", headers={"Authorization": "Bearer same.tok"})
        assert r1.status_code == r2.status_code == 200
        assert r1.get_json() == r2.get_json()
        assert len(transport.user_endpoint_calls) == 1
    finally:
        client.close()


def test_require_user_cache_ttl_expiry(monkeypatch):
    transport = AuthTransport([(200, SAMPLE_PAYLOAD), (200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(require_user(client, cache_ttl=10.0))
        with app.test_client() as c:
            c.get("/me", headers={"Authorization": "Bearer t"})
            assert len(transport.user_endpoint_calls) == 1

            import time as _time
            real_monotonic = _time.monotonic
            offset = 1000.0
            monkeypatch.setattr(
                "onelo._auth_cache.time.monotonic",
                lambda: real_monotonic() + offset,
            )

            c.get("/me", headers={"Authorization": "Bearer t"})
            assert len(transport.user_endpoint_calls) == 2
    finally:
        client.close()


def test_on_auth_event_callback_fires():
    events: list[tuple[str | None, str]] = []
    transport = AuthTransport([(200, SAMPLE_PAYLOAD), (401, {})])
    client = _make_secret_client(transport)
    try:
        app = _make_app(
            require_user(
                client,
                on_auth_event=lambda uid, ev: events.append((uid, ev)),
            )
        )
        with app.test_client() as c:
            c.get("/me", headers={"Authorization": "Bearer ok"})
            c.get("/me", headers={"Authorization": "Bearer bad"})
            c.get("/me")  # missing
        kinds = [ev for _uid, ev in events]
        assert "auth.verify" in kinds
        assert "auth.fail.invalid_token" in kinds
        assert "auth.fail.missing_token" in kinds
        # user_id is populated on the success event
        success = next((e for e in events if e[1] == "auth.verify"), None)
        assert success is not None
        assert success[0] == "user-123"
    finally:
        client.close()


def test_on_auth_event_callback_exceptions_are_swallowed():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)

    def bad_cb(_uid, _ev):
        raise RuntimeError("boom")

    try:
        app = _make_app(require_user(client, on_auth_event=bad_cb))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 200
    finally:
        client.close()


# ── optional_user ────────────────────────────────────────────────────────


def test_optional_user_missing_passes_none():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    try:
        app = _make_app(optional_user(client))
        with app.test_client() as c:
            r = c.get("/me")
        assert r.status_code == 200
        assert r.get_json() == {"anon": True}
        assert transport.user_endpoint_calls == []
    finally:
        client.close()


def test_optional_user_invalid_passes_none():
    transport = AuthTransport([(401, {})])
    client = _make_secret_client(transport)
    try:
        app = _make_app(optional_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer bad"})
        assert r.status_code == 200
        assert r.get_json() == {"anon": True}
    finally:
        client.close()


def test_optional_user_5xx_still_returns_503():
    transport = AuthTransport([(503, None), (503, None), (503, None)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(
            optional_user(client, retry_attempts=3, retry_total_timeout=1.0)
        )
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 503
        assert r.get_json() == {"error": "auth_service_unavailable"}
    finally:
        client.close()


def test_optional_user_happy_path_passes_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    try:
        app = _make_app(optional_user(client))
        with app.test_client() as c:
            r = c.get("/me", headers={"Authorization": "Bearer t"})
        assert r.status_code == 200
        assert r.get_json() == {"id": "user-123", "email": "alice@example.com"}
    finally:
        client.close()
