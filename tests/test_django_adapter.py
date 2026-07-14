"""Tests for the Django + DRF auth adapter (onelo.django).

Skipped entirely when Django is not installed.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

pytest.importorskip("django")
pytest.importorskip("rest_framework")

# ── Configure Django before importing anything that touches settings ──

import django
from django.conf import settings as django_settings

if not django_settings.configured:
    django_settings.configure(
        DEBUG=False,
        SECRET_KEY="test-secret-key-not-for-prod",
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "rest_framework",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        ROOT_URLCONF=__name__,
        MIDDLEWARE=[],
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        USE_TZ=True,
    )
    django.setup()

from django.http import JsonResponse  # noqa: E402
from django.test import RequestFactory  # noqa: E402
from rest_framework.exceptions import AuthenticationFailed  # noqa: E402
from rest_framework.test import APIRequestFactory  # noqa: E402

from onelo import Onelo  # noqa: E402
from onelo.django import (  # noqa: E402
    OneloAuthenticationFactory,
    OneloAuthMiddleware,
    OneloDjangoUser,
    require_onelo_user,
)


# ── Mock backend ─────────────────────────────────────────────────────────


SAMPLE_PAYLOAD = {
    "id": "user-123",
    "email": "alice@example.com",
    "metadata": {},
    "created_at": "2024-01-01T00:00:00Z",
}


class AuthTransport(httpx.MockTransport):
    """MockTransport for /api/sdk/auth/user with scripted responses."""

    def __init__(self, script: list[tuple[int, Any]]) -> None:
        self._script = list(script)
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


def _make_publishable_client() -> Onelo:
    return Onelo(
        publishable_key="onelo_pk_test_abc",
        api_url="https://example.com",
    )


# ── DRF authentication class tests ───────────────────────────────────────


def test_drf_valid_token_populates_request_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client)

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    auth = Auth()
    result = auth.authenticate(request)

    assert result is not None
    user, token = result
    assert token == "u_tok_abc"
    assert isinstance(user, OneloDjangoUser)
    assert user.is_authenticated is True
    assert user.is_anonymous is False
    assert user.id == "user-123"
    assert user.email == "alice@example.com"
    assert user.username == "alice@example.com"
    assert user.get_username() == "alice@example.com"
    # Pass-through attribute access
    assert user.email_verified is False
    client.close()


def test_drf_missing_authorization_returns_none():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client)

    factory = APIRequestFactory()
    request = factory.get("/me")  # no Authorization
    auth = Auth()

    assert auth.authenticate(request) is None
    # No backend call should have been made
    assert transport.user_endpoint_calls == []
    client.close()


def test_drf_non_bearer_authorization_returns_none():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client)

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Basic xyz==")
    auth = Auth()

    assert auth.authenticate(request) is None
    client.close()


def test_drf_invalid_token_raises_401():
    transport = AuthTransport([(401, {"error": "invalid_token"})])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client)

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Bearer bogus")
    auth = Auth()

    with pytest.raises(AuthenticationFailed) as ei:
        auth.authenticate(request)

    # status_code is 401 by default for AuthenticationFailed
    assert ei.value.status_code == 401
    detail = ei.value.detail
    assert detail.get("error") == "invalid_token"
    client.close()


def test_drf_forbidden_email_unverified_raises_403():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client, require_email_verified=True)

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    auth = Auth()

    with pytest.raises(AuthenticationFailed) as ei:
        auth.authenticate(request)

    # We pass code=403 to AuthenticationFailed; DRF maps it to 403.
    detail = ei.value.detail
    assert detail.get("error") == "forbidden"
    assert detail.get("reason") == "email_unverified"
    client.close()


def test_drf_forbidden_plan_mismatch_raises_403():
    payload = dict(SAMPLE_PAYLOAD, plan="free")
    transport = AuthTransport([(200, payload)])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client, require_plan=["pro"])

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    auth = Auth()

    with pytest.raises(AuthenticationFailed) as ei:
        auth.authenticate(request)

    detail = ei.value.detail
    assert detail.get("reason") == "plan"
    client.close()


def test_drf_backend_5xx_maps_to_503():
    # All retry attempts fail with 500 — should surface as 503.
    transport = AuthTransport([
        (500, {"error": "boom"}),
        (500, {"error": "boom"}),
        (500, {"error": "boom"}),
    ])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(
        client, retry_attempts=3, retry_total_timeout=2.0
    )

    factory = APIRequestFactory()
    request = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    auth = Auth()

    with pytest.raises(AuthenticationFailed) as ei:
        auth.authenticate(request)

    assert ei.value.status_code == 503
    detail = ei.value.detail
    assert detail.get("error") == "auth_service_unavailable"
    client.close()


def test_drf_publishable_key_raises_value_error():
    client = _make_publishable_client()
    with pytest.raises(ValueError, match="secret_key"):
        OneloAuthenticationFactory(client)
    client.close()


def test_drf_caches_verifications():
    # Only one upstream response — second call must hit the cache.
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    Auth = OneloAuthenticationFactory(client, cache_ttl=60.0)

    factory = APIRequestFactory()
    auth = Auth()

    req1 = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    user1, _ = auth.authenticate(req1)
    req2 = factory.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")
    user2, _ = auth.authenticate(req2)

    assert user1.id == user2.id == "user-123"
    assert len(transport.user_endpoint_calls) == 1
    client.close()


# ── Plain Django middleware tests ────────────────────────────────────────


def _run_middleware(client: Onelo, request: Any) -> Any:
    """Drive the middleware end-to-end and return the response."""
    captured: dict[str, Any] = {}

    def view(req: Any) -> JsonResponse:
        captured["request"] = req
        return JsonResponse({"id": getattr(req.onelo_user, "id", None)})

    # Inject the client via settings override
    from django.test import override_settings

    with override_settings(ONELO_CLIENT=client):
        mw = OneloAuthMiddleware(view)
        response = mw(request)
    return response, captured.get("request")


def test_middleware_populates_onelo_user():
    transport = AuthTransport([(200, SAMPLE_PAYLOAD)])
    client = _make_secret_client(transport)
    rf = RequestFactory()
    request = rf.get("/me", HTTP_AUTHORIZATION="Bearer u_tok_abc")

    response, processed = _run_middleware(client, request)

    assert response.status_code == 200
    assert processed.onelo_user is not None
    assert processed.onelo_user.id == "user-123"
    assert processed.onelo_auth_error is None
    client.close()


def test_middleware_missing_token_leaves_user_none():
    transport = AuthTransport([])
    client = _make_secret_client(transport)
    rf = RequestFactory()
    request = rf.get("/me")  # no Authorization

    response, processed = _run_middleware(client, request)

    # Middleware itself does NOT raise — view ran.
    assert response.status_code == 200
    assert processed.onelo_user is None
    assert processed.onelo_auth_error == "missing_token"
    assert processed.onelo_auth_status == 401
    client.close()


def test_middleware_invalid_token_leaves_user_none():
    transport = AuthTransport([(401, {"error": "invalid"})])
    client = _make_secret_client(transport)
    rf = RequestFactory()
    request = rf.get("/me", HTTP_AUTHORIZATION="Bearer bogus")

    response, processed = _run_middleware(client, request)

    assert processed.onelo_user is None
    assert processed.onelo_auth_error == "invalid_token"
    assert processed.onelo_auth_status == 401
    client.close()


def test_middleware_publishable_key_raises():
    client = _make_publishable_client()
    rf = RequestFactory()
    from django.test import override_settings

    with override_settings(ONELO_CLIENT=client):
        with pytest.raises(ValueError, match="secret_key"):
            OneloAuthMiddleware(lambda req: JsonResponse({}))
    client.close()


def test_middleware_missing_settings_client_raises():
    from django.test import override_settings

    with override_settings():
        # Ensure ONELO_CLIENT is not set
        if hasattr(django_settings, "ONELO_CLIENT"):
            delattr(django_settings, "ONELO_CLIENT")
        with pytest.raises(RuntimeError, match="ONELO_CLIENT"):
            OneloAuthMiddleware(lambda req: JsonResponse({}))


# ── require_onelo_user decorator tests ───────────────────────────────────


@require_onelo_user
def _protected_view(request: Any) -> JsonResponse:
    return JsonResponse({"id": request.onelo_user.id})


def test_decorator_missing_returns_401():
    rf = RequestFactory()
    request = rf.get("/me")
    # Simulate middleware having run with no token
    request.onelo_user = None
    request.onelo_auth_error = "missing_token"
    request.onelo_auth_status = 401

    response = _protected_view(request)
    assert response.status_code == 401
    body = json.loads(response.content)
    assert body["error"] == "missing_token"


def test_decorator_invalid_returns_401():
    rf = RequestFactory()
    request = rf.get("/me")
    request.onelo_user = None
    request.onelo_auth_error = "invalid_token"
    request.onelo_auth_status = 401

    response = _protected_view(request)
    assert response.status_code == 401
    body = json.loads(response.content)
    assert body["error"] == "invalid_token"


def test_decorator_forbidden_returns_403():
    rf = RequestFactory()
    request = rf.get("/me")
    request.onelo_user = None
    request.onelo_auth_error = "forbidden"
    request.onelo_auth_status = 403

    response = _protected_view(request)
    assert response.status_code == 403
    body = json.loads(response.content)
    assert body["error"] == "forbidden"


def test_decorator_unavailable_returns_503():
    rf = RequestFactory()
    request = rf.get("/me")
    request.onelo_user = None
    request.onelo_auth_error = "auth_service_unavailable"
    request.onelo_auth_status = 503

    response = _protected_view(request)
    assert response.status_code == 503


def test_decorator_passes_through_when_authenticated():
    from onelo.auth import OneloUser

    rf = RequestFactory()
    request = rf.get("/me")
    request.onelo_user = OneloUser(id="u1", email="a@b.com")

    response = _protected_view(request)
    assert response.status_code == 200
    body = json.loads(response.content)
    assert body["id"] == "u1"


def test_decorator_no_middleware_returns_401():
    """If middleware never ran, request has no onelo_user attribute at all."""
    rf = RequestFactory()
    request = rf.get("/me")  # plain request — no attrs

    response = _protected_view(request)
    assert response.status_code == 401


# ── OneloDjangoUser unit tests ───────────────────────────────────────────


def test_django_user_wrapper_attrs():
    from onelo.auth import OneloUser

    user = OneloUser(
        id="u1",
        email="a@b.com",
        email_verified=True,
        plan="pro",
        metadata={"k": "v"},
    )
    wrapped = OneloDjangoUser(user)

    assert wrapped.is_authenticated is True
    assert wrapped.is_anonymous is False
    assert wrapped.id == "u1"
    assert wrapped.pk == "u1"
    assert wrapped.email == "a@b.com"
    assert wrapped.username == "a@b.com"
    assert wrapped.get_username() == "a@b.com"
    # Pass-through
    assert wrapped.email_verified is True
    assert wrapped.plan == "pro"
    assert wrapped.metadata == {"k": "v"}
    assert wrapped.onelo_user is user
