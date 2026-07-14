"""Django + DRF adapter for Onelo. Optional — requires ``pip install 'onelo[django] @ git+https://github.com/onelo-tools/onelo-python.git@staging'``.

Two flavours are exposed:

1. **DRF authentication class** — idiomatic for Django REST Framework apps.
   Build a class via :func:`OneloAuthenticationFactory` and wire it into
   ``REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']``.

2. **Plain Django middleware + decorator** — for non-DRF Django apps.
   :class:`OneloAuthMiddleware` populates ``request.onelo_user`` (or
   ``None``); the :func:`require_onelo_user` decorator enforces 401/403.

Status code mapping (consistent across both flavours):

* **401** — missing or invalid token
* **403** — token valid but ``require_email_verified`` / ``require_plan``
  gate failed (or backend itself returned 403)
* **503** — Onelo backend unreachable after retry exhaustion

Quickstart — DRF::

    # settings.py
    REST_FRAMEWORK = {
        "DEFAULT_AUTHENTICATION_CLASSES": ["myapp.auth.OneloAuthentication"],
    }

    # myapp/auth.py
    import os
    from onelo import Onelo
    from onelo.django import OneloAuthenticationFactory

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])
    OneloAuthentication = OneloAuthenticationFactory(onelo)

    # views.py
    from rest_framework.decorators import api_view
    from rest_framework.response import Response

    @api_view(["GET"])
    def me(request):
        return Response({"id": request.user.id, "email": request.user.email})

Quickstart — plain Django::

    # settings.py
    from onelo import Onelo
    ONELO_CLIENT = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])
    MIDDLEWARE = [
        ...,
        "onelo.django.OneloAuthMiddleware",
    ]

    # views.py
    from onelo.django import require_onelo_user

    @require_onelo_user
    def me(request):
        return JsonResponse({"id": request.onelo_user.id})
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

# Lazy import — surface a helpful error when the [django] extra is missing.
try:
    from django.http import JsonResponse
except ImportError as exc:  # pragma: no cover - exercised only without extra
    raise ImportError(
        "onelo.django requires the [django] extra: "
        "pip install 'onelo[django] @ git+https://github.com/onelo-tools/onelo-python.git@staging'"
    ) from exc

from onelo._auth_cache import InProcessSyncAuthCache, SyncAuthCache
from onelo.auth import (
    OneloAuthError,
    OneloAuthForbidden,
    OneloAuthInvalidToken,
    OneloAuthMissingToken,
    OneloAuthUnavailable,
    OneloUser,
    verify_token_sync,
)

if TYPE_CHECKING:
    from onelo._client import Onelo

logger = logging.getLogger("onelo.django")


def _hash_token(token: str) -> str:
    """sha256(token).hexdigest — never store plaintext tokens in cache."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_emit(
    callback: Callable[[str | None, str], None] | None,
    user_id: str | None,
    event: str,
) -> None:
    if callback is None:
        return
    try:
        callback(user_id, event)
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "on_auth_event callback raised; suppressing", exc_info=True
        )


def _extract_bearer(authorization: str | None) -> str | None:
    """Pull a bearer token out of an Authorization header. Returns None if absent."""
    if not authorization:
        return None
    stripped = authorization.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        return token or None
    return None


def _check_gates(
    user: OneloUser,
    require_email_verified: bool,
    require_plan: set[str] | None,
) -> None:
    """Apply post-verify gates. Raises OneloAuthForbidden on failure."""
    if require_email_verified and not user.email_verified:
        raise OneloAuthForbidden("email_unverified")
    if require_plan is not None and user.plan not in require_plan:
        raise OneloAuthForbidden("plan")


# ── Django user wrapper ────────────────────────────────────────────────


class OneloDjangoUser:
    """Wraps :class:`OneloUser` to satisfy Django's ``request.user`` contract.

    Provides ``is_authenticated``, ``is_anonymous``, ``id``, ``email``,
    ``username``, and ``get_username()``. Other attribute accesses pass
    through to the underlying :class:`OneloUser` — so e.g. ``user.plan``,
    ``user.email_verified``, ``user.metadata`` continue to work.
    """

    is_authenticated: bool = True
    is_anonymous: bool = False
    is_active: bool = True
    is_staff: bool = False
    is_superuser: bool = False

    def __init__(self, onelo_user: OneloUser) -> None:
        self._user = onelo_user

    @property
    def id(self) -> str:
        return self._user.id

    @property
    def pk(self) -> str:
        return self._user.id

    @property
    def email(self) -> str:
        return self._user.email

    @property
    def username(self) -> str:
        # Django convention — username is the primary identifier.
        return self._user.email

    def get_username(self) -> str:
        return self._user.email

    @property
    def onelo_user(self) -> OneloUser:
        """Escape hatch back to the underlying :class:`OneloUser`."""
        return self._user

    def __getattr__(self, item: str) -> Any:
        # Only invoked when normal attribute lookup fails — pass through
        # to the wrapped OneloUser (plan, email_verified, metadata, raw…).
        return getattr(self._user, item)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"OneloDjangoUser(id={self._user.id!r}, email={self._user.email!r})"


# ── DRF authentication class factory ───────────────────────────────────


def OneloAuthenticationFactory(
    client: "Onelo",
    *,
    cache: SyncAuthCache | None = None,
    cache_ttl: float = 30.0,
    require_email_verified: bool = False,
    require_plan: list[str] | None = None,
    retry_attempts: int = 3,
    retry_total_timeout: float = 2.0,
    accept_query_token: bool = False,
    on_auth_event: Callable[[str | None, str], None] | None = None,
) -> type:
    """Build a DRF ``BaseAuthentication`` class wired to an Onelo client.

    Returns a class (not an instance) — DRF instantiates auth classes
    itself per request.

    Parameters
    ----------
    client:
        An ``Onelo`` instance constructed with ``secret_key=...``. Raises
        ``ValueError`` immediately on a publishable-key client.
    cache:
        Optional ``SyncAuthCache`` implementation. Defaults to
        :class:`InProcessSyncAuthCache`.
    cache_ttl:
        Per-entry TTL in seconds. Default 30s.
    require_email_verified, require_plan:
        Optional gates applied after verification.
    retry_attempts, retry_total_timeout:
        Retry policy for 5xx upstream failures (forwarded to
        ``verify_token_sync``).
    on_auth_event:
        Optional ``(user_id|None, event_name)`` callback. Exceptions are
        swallowed.
    """
    # Lazy import — DRF is in the [django] extra but we still surface a
    # nice error if only Django (no DRF) is installed.
    try:
        from rest_framework.authentication import BaseAuthentication
        from rest_framework.exceptions import AuthenticationFailed
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "OneloAuthenticationFactory requires Django REST Framework. "
            "Install via: pip install 'onelo[django] @ git+https://github.com/onelo-tools/onelo-python.git@staging'"
        ) from exc

    if not getattr(client, "_is_secret_key", False):
        raise ValueError(
            "OneloAuthenticationFactory requires an Onelo client constructed "
            "with secret_key=...; you supplied a publishable key. Backends "
            "must use Onelo(secret_key='onelo_sk_live_...')."
        )
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be >= 1")
    if retry_total_timeout <= 0:
        raise ValueError("retry_total_timeout must be > 0")

    _cache: SyncAuthCache = cache or InProcessSyncAuthCache()
    _require_plan = set(require_plan) if require_plan else None

    class OneloAuthentication(BaseAuthentication):
        """DRF authentication class verifying Onelo bearer tokens.

        Returns ``(OneloDjangoUser, token)`` on success, ``None`` when the
        Authorization header is absent (DRF will then fall through to the
        next configured authentication class). Raises
        ``AuthenticationFailed`` for invalid tokens, gate failures, and
        upstream errors.
        """

        # DRF reads this for the WWW-Authenticate header when raising.
        keyword = "Bearer"

        def authenticate_header(self, request: Any) -> str:  # noqa: D401
            return "Bearer"

        def authenticate(
            self, request: Any
        ) -> tuple[OneloDjangoUser, str] | None:
            authorization = request.META.get("HTTP_AUTHORIZATION") or ""
            token = _extract_bearer(authorization)
            if token is None and accept_query_token:
                # SSE/EventSource clients can't set an Authorization header (A4).
                q = request.GET.get("token")
                token = q.strip() if q and q.strip() else None
            if token is None:
                # DRF convention: returning None means "this auth class
                # does not handle this request" — DRF tries the next.
                return None

            token_hash = _hash_token(token)

            # Cache lookup before any network work.
            cached = _cache.get(token_hash)
            if cached is not None:
                try:
                    _check_gates(cached, require_email_verified, _require_plan)
                except OneloAuthForbidden as exc:
                    _safe_emit(
                        on_auth_event,
                        cached.id or None,
                        f"auth.fail.{exc}",
                    )
                    raise AuthenticationFailed(
                        {"error": "forbidden", "reason": str(exc)},
                        code=403,
                    ) from exc
                _safe_emit(on_auth_event, cached.id, "auth.verify")
                return OneloDjangoUser(cached), token

            try:
                user = verify_token_sync(
                    client,
                    token,
                    retry_attempts=retry_attempts,
                    retry_total_timeout=retry_total_timeout,
                )
            except OneloAuthMissingToken:
                _safe_emit(on_auth_event, None, "auth.fail.missing_token")
                raise AuthenticationFailed(
                    {"error": "missing_token"}, code=401
                )
            except OneloAuthInvalidToken:
                _safe_emit(on_auth_event, None, "auth.fail.invalid_token")
                raise AuthenticationFailed(
                    {"error": "invalid_token"}, code=401
                )
            except OneloAuthForbidden:
                _safe_emit(on_auth_event, None, "auth.fail.forbidden")
                raise AuthenticationFailed(
                    {"error": "forbidden"}, code=403
                )
            except OneloAuthUnavailable as exc:
                _safe_emit(on_auth_event, None, "auth.fail.unavailable")
                # DRF doesn't have a built-in 503 mapper — surface via the
                # exception's status_code so middleware/handlers can see it.
                err = AuthenticationFailed(
                    {"error": "auth_service_unavailable"}, code=503
                )
                # Some DRF versions hardcode status_code=401 on
                # AuthenticationFailed — override it explicitly.
                err.status_code = 503
                raise err from exc
            except OneloAuthError:
                _safe_emit(on_auth_event, None, "auth.fail.invalid_token")
                raise AuthenticationFailed(
                    {"error": "invalid_token"}, code=401
                )

            _cache.set(token_hash, user, cache_ttl)

            try:
                _check_gates(user, require_email_verified, _require_plan)
            except OneloAuthForbidden as exc:
                _safe_emit(
                    on_auth_event, user.id or None, f"auth.fail.{exc}"
                )
                raise AuthenticationFailed(
                    {"error": "forbidden", "reason": str(exc)},
                    code=403,
                ) from exc

            _safe_emit(on_auth_event, user.id, "auth.verify")
            return OneloDjangoUser(user), token

    OneloAuthentication.__name__ = "OneloAuthentication"
    OneloAuthentication.__qualname__ = "OneloAuthentication"
    return OneloAuthentication


# ── Plain Django middleware + decorator ────────────────────────────────


class OneloAuthMiddleware:
    """Django middleware that populates ``request.onelo_user``.

    Configure by setting ``ONELO_CLIENT`` (an ``Onelo`` instance) in
    ``settings.py`` and adding this class to ``MIDDLEWARE``.

    The middleware does NOT raise on missing/invalid tokens — view code
    is expected to check ``request.onelo_user`` (or wrap with
    :func:`require_onelo_user` for strict 401 behaviour).

    Optional settings:

    * ``ONELO_REQUIRE_EMAIL_VERIFIED`` (bool, default False)
    * ``ONELO_REQUIRE_PLAN`` (list[str] | None, default None)
    * ``ONELO_AUTH_CACHE_TTL`` (float, default 30.0)
    """

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        from django.conf import settings

        client = getattr(settings, "ONELO_CLIENT", None)
        if client is None:
            raise RuntimeError(
                "OneloAuthMiddleware requires settings.ONELO_CLIENT to be "
                "set to an Onelo(secret_key=...) instance."
            )
        if not getattr(client, "_is_secret_key", False):
            raise ValueError(
                "OneloAuthMiddleware requires an Onelo client constructed "
                "with secret_key=...; you supplied a publishable key."
            )

        self.get_response = get_response
        self._client = client
        self._cache: SyncAuthCache = getattr(
            settings, "ONELO_AUTH_CACHE", None
        ) or InProcessSyncAuthCache()
        self._cache_ttl = float(
            getattr(settings, "ONELO_AUTH_CACHE_TTL", 30.0)
        )
        self._require_email_verified = bool(
            getattr(settings, "ONELO_REQUIRE_EMAIL_VERIFIED", False)
        )
        plan = getattr(settings, "ONELO_REQUIRE_PLAN", None)
        self._require_plan = set(plan) if plan else None
        self._retry_attempts = int(
            getattr(settings, "ONELO_RETRY_ATTEMPTS", 3)
        )
        self._retry_total_timeout = float(
            getattr(settings, "ONELO_RETRY_TOTAL_TIMEOUT", 2.0)
        )
        self._on_auth_event: Callable[[str | None, str], None] | None = (
            getattr(settings, "ONELO_ON_AUTH_EVENT", None)
        )

    # The middleware exposes the resolution outcome via three attributes
    # on the request:
    #   * request.onelo_user   — OneloUser or None
    #   * request.onelo_auth_error — one of None / "missing_token" /
    #         "invalid_token" / "forbidden" / "auth_service_unavailable"
    #   * request.onelo_auth_status — int (200/401/403/503)
    def __call__(self, request: Any) -> Any:
        request.onelo_user = None
        request.onelo_auth_error = None
        request.onelo_auth_status = 200

        authorization = request.META.get("HTTP_AUTHORIZATION") or ""
        token = _extract_bearer(authorization)
        if token is None:
            request.onelo_auth_error = "missing_token"
            request.onelo_auth_status = 401
            return self.get_response(request)

        token_hash = _hash_token(token)
        cached = self._cache.get(token_hash)
        if cached is not None:
            try:
                _check_gates(
                    cached,
                    self._require_email_verified,
                    self._require_plan,
                )
            except OneloAuthForbidden as exc:
                _safe_emit(
                    self._on_auth_event,
                    cached.id or None,
                    f"auth.fail.{exc}",
                )
                request.onelo_auth_error = "forbidden"
                request.onelo_auth_status = 403
                return self.get_response(request)
            _safe_emit(self._on_auth_event, cached.id, "auth.verify")
            request.onelo_user = cached
            return self.get_response(request)

        try:
            user = verify_token_sync(
                self._client,
                token,
                retry_attempts=self._retry_attempts,
                retry_total_timeout=self._retry_total_timeout,
            )
        except OneloAuthMissingToken:
            request.onelo_auth_error = "missing_token"
            request.onelo_auth_status = 401
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
            return self.get_response(request)
        except OneloAuthInvalidToken:
            request.onelo_auth_error = "invalid_token"
            request.onelo_auth_status = 401
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            return self.get_response(request)
        except OneloAuthForbidden:
            request.onelo_auth_error = "forbidden"
            request.onelo_auth_status = 403
            _safe_emit(self._on_auth_event, None, "auth.fail.forbidden")
            return self.get_response(request)
        except OneloAuthUnavailable:
            request.onelo_auth_error = "auth_service_unavailable"
            request.onelo_auth_status = 503
            _safe_emit(self._on_auth_event, None, "auth.fail.unavailable")
            return self.get_response(request)
        except OneloAuthError:
            request.onelo_auth_error = "invalid_token"
            request.onelo_auth_status = 401
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            return self.get_response(request)

        self._cache.set(token_hash, user, self._cache_ttl)

        try:
            _check_gates(
                user, self._require_email_verified, self._require_plan
            )
        except OneloAuthForbidden as exc:
            _safe_emit(
                self._on_auth_event, user.id or None, f"auth.fail.{exc}"
            )
            request.onelo_auth_error = "forbidden"
            request.onelo_auth_status = 403
            return self.get_response(request)

        _safe_emit(self._on_auth_event, user.id, "auth.verify")
        request.onelo_user = user
        return self.get_response(request)


def require_onelo_user(view_func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator enforcing a populated ``request.onelo_user``.

    Returns a 401 / 403 / 503 ``JsonResponse`` if :class:`OneloAuthMiddleware`
    did not resolve a user. Use on plain Django views::

        from onelo.django import require_onelo_user

        @require_onelo_user
        def me(request):
            return JsonResponse({"id": request.onelo_user.id})
    """

    @wraps(view_func)
    def _wrapped(request: Any, *args: Any, **kwargs: Any) -> Any:
        user = getattr(request, "onelo_user", None)
        if user is not None:
            return view_func(request, *args, **kwargs)

        error = getattr(request, "onelo_auth_error", None) or "missing_token"
        status = getattr(request, "onelo_auth_status", None) or 401
        body: dict[str, Any] = {"error": error}
        return JsonResponse(body, status=status)

    return _wrapped


__all__ = [
    "OneloAuthenticationFactory",
    "OneloAuthMiddleware",
    "OneloDjangoUser",
    "require_onelo_user",
]
