"""Universal ASGI middleware for Onelo. Sets ``scope['onelo_user']`` on
authenticated requests. Use in any ASGI 3 framework.

Usage (FastAPI / Starlette / Litestar / Quart)::

    from onelo import Onelo
    from onelo.asgi import OneloAsgiMiddleware

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])

    app = FastAPI()
    app.add_middleware(OneloAsgiMiddleware, onelo=onelo)

    @app.get("/me")
    async def me(request: Request):
        user = request.scope.get("onelo_user")
        if not user:
            raise HTTPException(401)
        return {"id": user.id}

The middleware **never** raises on auth failure — it populates
``scope['onelo_user']`` (with ``None`` on missing/invalid tokens) and
lets the application decide what to do. When the Onelo backend is
**unreachable** it ALSO sets ``scope['onelo_auth_unavailable'] = True``
(with ``onelo_user`` None) so the app can tell "couldn't verify" apart
from "anonymous" and return 503 instead of silently serving the request
unauthenticated::

    if request.scope.get("onelo_auth_unavailable"):
        raise HTTPException(503, "auth temporarily unavailable")

This is the "Tier 2" universal fallback: framework-idiomatic auth (with
proper 401/403/503 mapping) lives in :mod:`onelo.fastapi`,
:mod:`onelo.flask`, etc.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from onelo._auth_cache import AuthCache, InProcessAuthCache
from onelo.auth import (
    OneloAuthError,
    OneloAuthUnavailable,
    OneloUser,
    _token_from_query_string,
    verify_token,
)

if TYPE_CHECKING:
    from onelo._client import Onelo

logger = logging.getLogger("onelo.asgi")


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
    """Pull a bearer token out of an Authorization header. None if absent."""
    if not authorization:
        return None
    stripped = authorization.strip()
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        return token or None
    return None


def _read_authorization_from_scope(
    scope: dict[str, Any], header_name: str
) -> str | None:
    """ASGI scope headers are an iterable of (bytes, bytes) tuples."""
    target = header_name.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name.lower() == target:
            try:
                return raw_value.decode("latin-1")
            except Exception:  # pragma: no cover - defensive
                return None
    return None


class OneloAsgiMiddleware:
    """Universal ASGI 3 middleware for Onelo authentication.

    Verifies the ``Authorization: Bearer <token>`` header on every HTTP
    request and populates ``scope['onelo_user']`` with either an
    :class:`OneloUser` instance (on success) or ``None`` (on missing or
    invalid token, or backend unavailability). The application is then
    free to decide how to respond — typically by reading
    ``request.scope['onelo_user']`` in a handler.

    Non-HTTP scopes (``websocket``, ``lifespan``) are passed through
    unchanged in v1.

    Parameters
    ----------
    app:
        The downstream ASGI application.
    onelo:
        An ``Onelo`` instance constructed with ``secret_key=...``. A
        ``ValueError`` is raised at construction time if a publishable
        key was used.
    cache:
        Optional ``AuthCache`` implementation. Defaults to a fresh
        :class:`InProcessAuthCache`.
    cache_ttl:
        TTL in seconds for cached verifications. Default 30s.
    retry_attempts:
        Total attempts on 5xx (including initial). Default 3.
    retry_total_timeout:
        Wall-clock cap (seconds) on the retry sequence. Default 2.0s.
    header_name:
        The header to read the token from. Default ``"authorization"``.
    on_auth_event:
        Optional callback ``(user_id_or_None, event_name)``. Exceptions
        raised by the callback are swallowed.
    identify_monitor:
        When ``True`` (default) and ``onelo.monitor`` is initialised, the
        resolved user's **id** is attached to a per-request monitor
        isolation scope for the duration of the request, so monitor events
        captured downstream carry ``user_id`` — the same canonical id the
        frontend SDK reports. Only the **opaque id** is propagated, never
        email / name: monitor events are logs and must stay PII-free (the
        dashboard resolves id → identity live at display time from a
        separate, access-controlled store — pseudonymisation, GDPR
        Art. 4(5)). Set ``False`` to opt out.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        onelo: "Onelo",
        cache: AuthCache | None = None,
        cache_ttl: float = 30.0,
        retry_attempts: int = 3,
        retry_total_timeout: float = 2.0,
        header_name: str = "authorization",
        accept_query_token: bool = False,
        on_auth_event: Callable[[str | None, str], None] | None = None,
        identify_monitor: bool = True,
    ) -> None:
        if onelo is None:
            raise ValueError("onelo client is required")
        if not getattr(onelo, "_is_secret_key", False):
            raise ValueError(
                "OneloAsgiMiddleware requires an Onelo client constructed "
                "with secret_key=...; you supplied a publishable key. "
                "Backends must use Onelo(secret_key='onelo_sk_live_...')."
            )
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be >= 1")
        if retry_total_timeout <= 0:
            raise ValueError("retry_total_timeout must be > 0")

        self.app = app
        self._onelo = onelo
        self._cache = cache or InProcessAuthCache()
        self._cache_ttl = cache_ttl
        self._retry_attempts = retry_attempts
        self._retry_total_timeout = retry_total_timeout
        self._header_name = header_name.lower()
        self._accept_query_token = accept_query_token
        self._on_auth_event = on_auth_event
        self._identify_monitor = identify_monitor

    async def _verify_with_retry(self, token: str) -> OneloUser:
        """verify_token + 5xx retry policy with a wall-clock budget."""
        from onelo.auth import OneloAuthRateLimited, OneloAuthUnavailable

        deadline = time.monotonic() + self._retry_total_timeout
        last_exc: OneloAuthUnavailable | None = None

        for attempt in range(self._retry_attempts):
            try:
                return await verify_token(self._onelo, token)
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

    async def _resolve_user(self, token: str) -> OneloUser | None:
        """Cache → verify. Returns None on any auth/network failure."""
        token_hash = _hash_token(token)

        cached = await self._cache.get(token_hash)
        if cached is not None:
            _safe_emit(self._on_auth_event, cached.id, "auth.verify")
            return cached

        try:
            user = await self._verify_with_retry(token)
        except OneloAuthUnavailable:
            # Backend unreachable — NOT the same as an invalid/missing token.
            # Propagate so __call__ flags it; the app must be able to 503 rather
            # than silently serve the request as anonymous (A1).
            _safe_emit(self._on_auth_event, None, "auth.fail.unavailable")
            raise
        except OneloAuthError as exc:
            event = f"auth.fail.{type(exc).__name__}"
            _safe_emit(self._on_auth_event, None, event)
            return None
        except Exception:  # pragma: no cover - defensive
            logger.warning("unexpected auth error; treating as unauth", exc_info=True)
            _safe_emit(self._on_auth_event, None, "auth.fail.unexpected")
            return None

        await self._cache.set(token_hash, user, self._cache_ttl)
        _safe_emit(self._on_auth_event, user.id, "auth.verify")
        return user

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        # Pass non-HTTP scopes through unchanged (websocket / lifespan).
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Make a shallow copy so we don't mutate caller-owned dicts in
        # surprising ways. Most ASGI frameworks pass us a fresh dict per
        # request, but defensive copy is cheap.
        authorization = _read_authorization_from_scope(scope, self._header_name)
        token = _extract_bearer(authorization)
        if token is None and self._accept_query_token:
            # SSE/EventSource clients can't set an Authorization header (A4).
            token = _token_from_query_string(scope.get("query_string") or b"")

        # True ONLY when the backend was unreachable — lets the app distinguish
        # "couldn't verify" from "anonymous" and return 503 instead of silently
        # serving the request unauthenticated (A1). Read via
        # scope.get("onelo_auth_unavailable").
        scope["onelo_auth_unavailable"] = False
        if token is None:
            scope["onelo_user"] = None
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
        else:
            try:
                scope["onelo_user"] = await self._resolve_user(token)
            except OneloAuthUnavailable:
                scope["onelo_user"] = None
                scope["onelo_auth_unavailable"] = True

        user = scope.get("onelo_user")
        if self._identify_monitor and user is not None and getattr(user, "id", None):
            # Fork a per-request monitor isolation scope carrying ONLY the
            # opaque user id (never email / name — logs stay PII-free). The
            # ``with`` block wraps the whole downstream call, including
            # streaming responses, and forks a fresh ContextVar slot so
            # concurrent requests never share an identity — correct
            # regardless of where this middleware sits relative to
            # OneloMonitorASGIMiddleware.
            bridged = self._enter_monitor_identity(user.id)
            if bridged is not None:
                with bridged:
                    await self.app(scope, receive, send)
                return

        await self.app(scope, receive, send)

    def _enter_monitor_identity(self, user_id: str) -> Any | None:
        """Return a ``monitor.identified_scope(user_id)`` context manager, or
        ``None`` when monitor isn't initialised / unavailable. Never raises —
        the identity bridge must not break request handling."""
        try:
            from onelo import monitor

            if not monitor.is_initialised():
                return None
            return monitor.identified_scope(user_id)
        except Exception:  # pragma: no cover - defensive
            logger.warning("monitor identity bridge failed; ignoring", exc_info=True)
            return None


__all__ = ["OneloAsgiMiddleware"]
