"""Flask integration for Onelo auth.

Optional — requires ``pip install 'onelo[flask] @ git+https://github.com/onelo-tools/onelo-python.git@staging'``.

Quickstart
----------

.. code-block:: python

    import os
    from flask import Flask
    from onelo import Onelo
    from onelo.flask import require_user, OneloUser

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])
    app = Flask(__name__)

    @app.get("/me")
    @require_user(onelo)
    def me(user: OneloUser):
        return {"id": user.id, "email": user.email}

Status code mapping
-------------------
* **401** — missing or invalid token
* **403** — token valid but ``require_email_verified`` / ``require_plan``
  gate failed (or backend itself returned 403)
* **503** — Onelo backend unreachable after retry exhaustion

For non-Flask integrations, see :mod:`onelo.fastapi` or call the
synchronous :func:`onelo.auth.verify_token_sync` helper directly.
"""
from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

# Lazy import — surface a helpful error when the [flask] extra is missing.
try:
    from flask import jsonify, request
except ImportError as exc:  # pragma: no cover - exercised only without extra
    raise ImportError(
        "onelo.flask requires the [flask] extra: "
        "pip install 'onelo[flask] @ git+https://github.com/onelo-tools/onelo-python.git@staging'"
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

logger = logging.getLogger("onelo.flask")


def _hash_token(token: str) -> str:
    """sha256(token).hexdigest — never store plaintext tokens in cache."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_emit(
    callback: Callable[[str | None, str], None] | None,
    user_id: str | None,
    event: str,
) -> None:
    """Invoke an on_auth_event callback, swallowing any exception.

    Per spec: callbacks must NEVER take down the request — wrap and log.
    """
    if callback is None:
        return
    try:
        callback(user_id, event)
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "on_auth_event callback raised; suppressing", exc_info=True
        )


def _extract_bearer_token(authorization: str | None) -> str | None:
    """Pull a bearer token out of an Authorization header. None if absent."""
    if not authorization:
        return None
    stripped = authorization.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        return token or None
    # Authorization header present but not bearer scheme — treat as missing.
    return None


class _AuthError(Exception):
    """Internal — carries the (status, body) the view should return."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error", "auth_error"))
        self.status = status
        self.body = body


def _build_decorator(
    client: "Onelo",
    *,
    optional: bool,
    cache: SyncAuthCache | None,
    cache_ttl: float,
    require_email_verified: bool,
    require_plan: list[str] | None,
    retry_attempts: int,
    retry_total_timeout: float,
    accept_query_token: bool,
    on_auth_event: Callable[[str | None, str], None] | None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Construct a Flask view decorator with the given configuration.

    Shared by ``require_user`` and ``optional_user``. The cache is
    instantiated once per decorator invocation and shared across all
    requests to the wrapped view.
    """
    if not getattr(client, "_is_secret_key", False):
        raise ValueError(
            "onelo.flask.require_user requires an Onelo client constructed "
            "with secret_key=...; you supplied a publishable key. "
            "Backends must use Onelo(secret_key='onelo_sk_live_...')."
        )
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be >= 1")
    if retry_total_timeout <= 0:
        raise ValueError("retry_total_timeout must be > 0")

    plan_set = set(require_plan) if require_plan else None
    _cache: SyncAuthCache = cache if cache is not None else InProcessSyncAuthCache()

    def _check_gates(user: OneloUser) -> None:
        if require_email_verified and not user.email_verified:
            raise OneloAuthForbidden("email_unverified")
        if plan_set is not None and user.plan not in plan_set:
            raise OneloAuthForbidden("plan")

    def _resolve_user() -> OneloUser:
        """Run extraction → cache → verify → gates. Raises _AuthError."""
        token = _extract_bearer_token(request.headers.get("Authorization"))
        if token is None and accept_query_token:
            # SSE/EventSource clients can't set an Authorization header (A4).
            q = request.args.get("token")
            token = q.strip() if q and q.strip() else None
        if token is None:
            _safe_emit(on_auth_event, None, "auth.fail.missing_token")
            raise _AuthError(401, {"error": "missing_token"})

        token_hash = _hash_token(token)
        cached = _cache.get(token_hash)
        if cached is not None:
            try:
                _check_gates(cached)
            except OneloAuthForbidden as exc:
                _safe_emit(
                    on_auth_event, cached.id or None, f"auth.fail.{exc}"
                )
                raise _AuthError(
                    403, {"error": "forbidden", "reason": str(exc)}
                ) from exc
            _safe_emit(on_auth_event, cached.id, "auth.verify")
            return cached

        try:
            user = verify_token_sync(
                client,
                token,
                retry_attempts=retry_attempts,
                retry_total_timeout=retry_total_timeout,
            )
        except OneloAuthMissingToken:
            _safe_emit(on_auth_event, None, "auth.fail.missing_token")
            raise _AuthError(401, {"error": "missing_token"})
        except OneloAuthInvalidToken:
            _safe_emit(on_auth_event, None, "auth.fail.invalid_token")
            raise _AuthError(401, {"error": "invalid_token"})
        except OneloAuthForbidden:
            _safe_emit(on_auth_event, None, "auth.fail.forbidden")
            raise _AuthError(403, {"error": "forbidden"})
        except OneloAuthUnavailable:
            _safe_emit(on_auth_event, None, "auth.fail.unavailable")
            raise _AuthError(503, {"error": "auth_service_unavailable"})
        except OneloAuthError:
            _safe_emit(on_auth_event, None, "auth.fail.invalid_token")
            raise _AuthError(401, {"error": "invalid_token"})

        # Cache the raw verification result; gates are per-request.
        _cache.set(token_hash, user, cache_ttl)

        try:
            _check_gates(user)
        except OneloAuthForbidden as exc:
            _safe_emit(
                on_auth_event, user.id or None, f"auth.fail.{exc}"
            )
            raise _AuthError(
                403, {"error": "forbidden", "reason": str(exc)}
            ) from exc

        _safe_emit(on_auth_event, user.id, "auth.verify")
        return user

    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                user = _resolve_user()
            except _AuthError as exc:
                if optional and exc.status in (401, 403):
                    return view(*args, user=None, **kwargs)
                response = jsonify(exc.body)
                response.status_code = exc.status
                return response
            return view(*args, user=user, **kwargs)

        return wrapper

    return decorator


def require_user(
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
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Flask decorator factory that enforces a verified Onelo user.

    Returns a decorator. The decorated view receives the verified
    :class:`OneloUser` as a keyword argument named ``user``. On missing
    or invalid tokens the wrapper returns a 401 JSON response; on plan
    or email-verification gate failures, 403; on backend 5xx after retry
    exhaustion, 503.

    Parameters
    ----------
    client:
        An ``Onelo`` instance constructed with ``secret_key=...``. A
        ``ValueError`` is raised at decorator-creation time if a
        publishable key was used.
    cache:
        Optional ``SyncAuthCache``. Defaults to a fresh
        ``InProcessSyncAuthCache`` per ``require_user`` invocation.
    cache_ttl:
        TTL in seconds for cached verifications. Default 30s.
    require_email_verified, require_plan:
        Optional post-verify gates — see :class:`onelo.fastapi.RequireUser`.
    retry_attempts, retry_total_timeout:
        Retry policy for backend 5xx — see
        :func:`onelo.auth.verify_token_sync`.
    on_auth_event:
        Optional callback ``(user_id_or_None, event_name)``. Exceptions
        raised by the callback are swallowed.
    """
    return _build_decorator(
        client,
        optional=False,
        cache=cache,
        cache_ttl=cache_ttl,
        require_email_verified=require_email_verified,
        require_plan=require_plan,
        retry_attempts=retry_attempts,
        retry_total_timeout=retry_total_timeout,
        accept_query_token=accept_query_token,
        on_auth_event=on_auth_event,
    )


def optional_user(
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
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Like :func:`require_user` but injects ``user=None`` when missing.

    Missing / invalid / forbidden tokens result in the view being
    invoked with ``user=None`` rather than returning 401/403. Backend
    unavailability (5xx after retries) still returns 503 — those are
    operational failures and silently degrading them would mask incidents.
    """
    return _build_decorator(
        client,
        optional=True,
        cache=cache,
        cache_ttl=cache_ttl,
        require_email_verified=require_email_verified,
        require_plan=require_plan,
        retry_attempts=retry_attempts,
        retry_total_timeout=retry_total_timeout,
        accept_query_token=accept_query_token,
        on_auth_event=on_auth_event,
    )


__all__ = ["require_user", "optional_user", "OneloUser"]
