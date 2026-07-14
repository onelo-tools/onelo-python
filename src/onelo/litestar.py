"""Litestar adapter for Onelo. Optional — requires ``pip install 'onelo[litestar] @ git+https://github.com/onelo-tools/onelo-python.git@staging'``.

Litestar uses guards for authorization and dependency injection for typed
context. We provide both:

Usage with guards (no typed user injection)::

    from litestar import Litestar, get
    from onelo import Onelo
    from onelo.litestar import OneloGuardFactory

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])
    require_user = OneloGuardFactory(onelo)

    @get("/me", guards=[require_user])
    async def me() -> dict:
        # access via connection.user — populated by guard
        return ...

Usage with typed dependency (recommended)::

    from typing import Annotated
    from litestar import Litestar, get
    from litestar.di import Provide
    from onelo.litestar import provide_onelo_user, OneloUser

    @get("/me")
    async def me(user: OneloUser) -> dict:
        return {"id": user.id, "email": user.email}

    app = Litestar(
        route_handlers=[me],
        dependencies={"user": Provide(provide_onelo_user(onelo))},
    )

Status code mapping
-------------------
* **401** — missing or invalid token (``NotAuthorizedException``)
* **403** — token valid but ``require_email_verified`` / ``require_plan``
  gate failed (``PermissionDeniedException``)
* **503** — Onelo backend unreachable after retry exhaustion
  (``ServiceUnavailableException``)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

# Lazy import — surface a helpful error when the [litestar] extra is missing.
try:
    from litestar.connection import ASGIConnection
    from litestar.di import Provide
    from litestar.exceptions import (
        NotAuthorizedException,
        PermissionDeniedException,
        ServiceUnavailableException,
    )
except ImportError as exc:  # pragma: no cover - exercised only without extra
    raise ImportError(
        "onelo.litestar requires the [litestar] extra: "
        "pip install 'onelo[litestar] @ git+https://github.com/onelo-tools/onelo-python.git@staging'"
    ) from exc

from onelo._auth_cache import AuthCache, InProcessAuthCache
from onelo.auth import (
    OneloAuthError,
    OneloAuthForbidden,
    OneloAuthInvalidToken,
    OneloAuthMissingToken,
    OneloAuthRateLimited,
    OneloAuthUnavailable,
    OneloUser,
    verify_token,
)

if TYPE_CHECKING:
    from onelo._client import Onelo

logger = logging.getLogger("onelo.litestar")


_BACKOFF_BASE = 0.1


def _hash_token(token: str) -> str:
    """sha256(token).hexdigest — never store plaintext tokens in cache."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_emit(
    callback: Callable[[str | None, str], None] | None,
    user_id: str | None,
    event: str,
) -> None:
    """Invoke an on_auth_event callback, swallowing any exception."""
    if callback is None:
        return
    try:
        callback(user_id, event)
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "on_auth_event callback raised; suppressing", exc_info=True
        )


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    stripped = authorization.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        return token or None
    return None


class _OneloVerifier:
    """Shared verification core for guards and DI providers.

    Encapsulates the cache + retry + gates pipeline. Distinct from the
    FastAPI ``RequireUser`` so the Litestar layer doesn't take a hard
    dependency on FastAPI.
    """

    def __init__(
        self,
        client: "Onelo",
        *,
        cache: AuthCache | None = None,
        cache_ttl: float = 30.0,
        require_email_verified: bool = False,
        require_plan: list[str] | None = None,
        retry_attempts: int = 3,
        retry_total_timeout: float = 2.0,
        accept_query_token: bool = False,
        on_auth_event: Callable[[str | None, str], None] | None = None,
    ) -> None:
        if not getattr(client, "_is_secret_key", False):
            raise ValueError(
                "onelo.litestar requires an Onelo client constructed with "
                "secret_key=...; you supplied a publishable key. "
                "Backends must use Onelo(secret_key='onelo_sk_live_...')."
            )
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be >= 1")
        if retry_total_timeout <= 0:
            raise ValueError("retry_total_timeout must be > 0")

        self._client = client
        self._cache = cache or InProcessAuthCache()
        self._cache_ttl = cache_ttl
        self._require_email_verified = require_email_verified
        self._require_plan = set(require_plan) if require_plan else None
        self._retry_attempts = retry_attempts
        self._retry_total_timeout = retry_total_timeout
        self._accept_query_token = accept_query_token
        self._on_auth_event = on_auth_event

    def _check_gates(self, user: OneloUser) -> None:
        if self._require_email_verified and not user.email_verified:
            raise OneloAuthForbidden("email_unverified")
        if self._require_plan is not None:
            if user.plan not in self._require_plan:
                raise OneloAuthForbidden("plan")

    async def _verify_with_retry(self, token: str) -> OneloUser:
        deadline = time.monotonic() + self._retry_total_timeout
        last_exc: OneloAuthUnavailable | None = None
        for attempt in range(self._retry_attempts):
            try:
                return await verify_token(self._client, token)
            except OneloAuthUnavailable as exc:
                last_exc = exc
                if isinstance(exc, OneloAuthRateLimited):
                    break  # don't retry a 429 (A3)
                next_attempt = attempt + 1
                if next_attempt >= self._retry_attempts:
                    break
                backoff = _BACKOFF_BASE * (2 ** attempt)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(backoff, remaining))
                if time.monotonic() >= deadline:
                    break
        assert last_exc is not None
        raise last_exc

    async def resolve(
        self, authorization: str | None, query_token: str | None = None
    ) -> OneloUser:
        """Resolve a token to a OneloUser. Raises Litestar exceptions."""
        token = _extract_bearer(authorization)
        if token is None and self._accept_query_token and query_token:
            # SSE/EventSource clients can't set an Authorization header (A4).
            token = query_token.strip() or None
        if token is None:
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
            raise NotAuthorizedException(detail="missing_token")

        token_hash = _hash_token(token)

        cached = await self._cache.get(token_hash)
        if cached is not None:
            try:
                self._check_gates(cached)
            except OneloAuthForbidden as exc:
                _safe_emit(
                    self._on_auth_event,
                    cached.id or None,
                    f"auth.fail.{exc}",
                )
                raise PermissionDeniedException(
                    detail=f"forbidden: {exc}"
                ) from exc
            _safe_emit(self._on_auth_event, cached.id, "auth.verify")
            return cached

        try:
            user = await self._verify_with_retry(token)
        except OneloAuthMissingToken:
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
            raise NotAuthorizedException(detail="missing_token")
        except OneloAuthInvalidToken:
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            raise NotAuthorizedException(detail="invalid_token")
        except OneloAuthForbidden:
            _safe_emit(self._on_auth_event, None, "auth.fail.forbidden")
            raise PermissionDeniedException(detail="forbidden")
        except OneloAuthUnavailable:
            _safe_emit(self._on_auth_event, None, "auth.fail.unavailable")
            raise ServiceUnavailableException(
                detail="auth_service_unavailable"
            )
        except OneloAuthError:
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            raise NotAuthorizedException(detail="invalid_token")

        await self._cache.set(token_hash, user, self._cache_ttl)

        try:
            self._check_gates(user)
        except OneloAuthForbidden as exc:
            _safe_emit(
                self._on_auth_event, user.id or None, f"auth.fail.{exc}"
            )
            raise PermissionDeniedException(
                detail=f"forbidden: {exc}"
            ) from exc

        _safe_emit(self._on_auth_event, user.id, "auth.verify")
        return user


class OneloGuardFactory:
    """Build an async Litestar guard that verifies an Onelo bearer token.

    Instances are callable with the Litestar guard signature
    ``async def guard(connection, _) -> None`` and populate
    ``connection.scope["user"]`` so handlers can read ``connection.user``.

    Parameters mirror ``onelo.fastapi.RequireUser``.
    """

    def __init__(
        self,
        client: "Onelo",
        *,
        cache: AuthCache | None = None,
        cache_ttl: float = 30.0,
        require_email_verified: bool = False,
        require_plan: list[str] | None = None,
        retry_attempts: int = 3,
        retry_total_timeout: float = 2.0,
        accept_query_token: bool = False,
        on_auth_event: Callable[[str | None, str], None] | None = None,
    ) -> None:
        self._accept_query_token = accept_query_token
        self._verifier = _OneloVerifier(
            client,
            cache=cache,
            cache_ttl=cache_ttl,
            require_email_verified=require_email_verified,
            require_plan=require_plan,
            retry_attempts=retry_attempts,
            retry_total_timeout=retry_total_timeout,
            accept_query_token=accept_query_token,
            on_auth_event=on_auth_event,
        )

    async def __call__(
        self, connection: ASGIConnection, _: Any
    ) -> None:
        authorization = connection.headers.get("authorization")
        query_token = (
            connection.query_params.get("token")
            if self._accept_query_token
            else None
        )
        user = await self._verifier.resolve(authorization, query_token)
        # Populate connection.scope["user"] so handlers can read
        # ``connection.user`` per Litestar conventions.
        connection.scope["user"] = user


def provide_onelo_user(
    client: "Onelo",
    *,
    cache: AuthCache | None = None,
    cache_ttl: float = 30.0,
    require_email_verified: bool = False,
    require_plan: list[str] | None = None,
    retry_attempts: int = 3,
    retry_total_timeout: float = 2.0,
    on_auth_event: Callable[[str | None, str], None] | None = None,
) -> Provide:
    """Build a Litestar ``Provide`` dependency yielding ``OneloUser``."""
    verifier = _OneloVerifier(
        client,
        cache=cache,
        cache_ttl=cache_ttl,
        require_email_verified=require_email_verified,
        require_plan=require_plan,
        retry_attempts=retry_attempts,
        retry_total_timeout=retry_total_timeout,
        on_auth_event=on_auth_event,
    )

    async def _dependency(headers: dict[str, str]) -> OneloUser:
        authorization = headers.get("authorization")
        return await verifier.resolve(authorization)

    return Provide(_dependency)


def provide_optional_onelo_user(
    client: "Onelo",
    *,
    cache: AuthCache | None = None,
    cache_ttl: float = 30.0,
    require_email_verified: bool = False,
    require_plan: list[str] | None = None,
    retry_attempts: int = 3,
    retry_total_timeout: float = 2.0,
    on_auth_event: Callable[[str | None, str], None] | None = None,
) -> Provide:
    """Like ``provide_onelo_user`` but returns ``None`` on missing/invalid.

    503 (auth service unavailable) still propagates — those are operational
    failures, not authentication outcomes.
    """
    verifier = _OneloVerifier(
        client,
        cache=cache,
        cache_ttl=cache_ttl,
        require_email_verified=require_email_verified,
        require_plan=require_plan,
        retry_attempts=retry_attempts,
        retry_total_timeout=retry_total_timeout,
        on_auth_event=on_auth_event,
    )

    async def _dependency(headers: dict[str, str]) -> OneloUser | None:
        authorization = headers.get("authorization")
        try:
            return await verifier.resolve(authorization)
        except (NotAuthorizedException, PermissionDeniedException):
            return None

    return Provide(_dependency)


__all__ = [
    "OneloGuardFactory",
    "provide_onelo_user",
    "provide_optional_onelo_user",
    "OneloUser",
]
