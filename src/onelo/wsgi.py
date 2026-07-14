"""Universal WSGI middleware for Onelo. Sets ``environ['onelo.user']`` on
authenticated requests. Use in any WSGI app.

Usage (Flask / Django / Pyramid / Bottle)::

    from flask import Flask
    from onelo import Onelo
    from onelo.wsgi import OneloWsgiMiddleware

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])

    app = Flask(__name__)
    app.wsgi_app = OneloWsgiMiddleware(app.wsgi_app, onelo=onelo)

    @app.get("/me")
    def me():
        user = request.environ.get("onelo.user")
        if not user:
            return {"error": "unauthorized"}, 401
        return {"id": user.id}

The middleware **never** raises on auth failure — it populates
``environ['onelo.user']`` (with ``None`` on missing/invalid tokens) and
lets the application decide what to do. When the Onelo backend is
**unreachable** it ALSO sets ``environ['onelo.auth_unavailable'] = True``
(with ``onelo.user`` None) so the app can tell "couldn't verify" apart
from "anonymous" and return 503 instead of silently serving the request
unauthenticated. This is the "Tier 2" universal fallback; framework-
idiomatic decorators with proper 401/403/503 mapping live in
:mod:`onelo.flask` / :mod:`onelo.django`.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from onelo._auth_cache import InProcessSyncAuthCache, SyncAuthCache
from onelo.auth import (
    OneloAuthError,
    OneloAuthUnavailable,
    OneloUser,
    _token_from_query_string,
    verify_token_sync,
)

if TYPE_CHECKING:
    from onelo._client import Onelo

logger = logging.getLogger("onelo.wsgi")


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


class OneloWsgiMiddleware:
    """Universal WSGI middleware for Onelo authentication.

    Verifies the ``Authorization: Bearer <token>`` header on every
    request (read from ``environ['HTTP_AUTHORIZATION']`` per WSGI/PEP 3333)
    and populates ``environ['onelo.user']`` with either an
    :class:`OneloUser` instance (on success) or ``None`` (on missing or
    invalid token, or backend unavailability).

    The middleware is thread-safe: the underlying
    :class:`InProcessSyncAuthCache` uses ``threading.Lock``, so it is
    safe under multi-threaded WSGI servers (gunicorn ``--threads``,
    uWSGI threads, waitress).

    Parameters
    ----------
    app:
        The downstream WSGI application callable.
    onelo:
        An ``Onelo`` instance constructed with ``secret_key=...``. A
        ``ValueError`` is raised at construction time if a publishable
        key was used.
    cache:
        Optional ``SyncAuthCache``. Defaults to a fresh
        :class:`InProcessSyncAuthCache`.
    cache_ttl:
        TTL in seconds for cached verifications. Default 30s.
    retry_attempts:
        Total attempts on 5xx (including initial). Default 3.
    retry_total_timeout:
        Wall-clock cap (seconds) on the retry sequence. Default 2.0s.
    header_name:
        The WSGI environ key to read the token from. Default
        ``"HTTP_AUTHORIZATION"``. (PEP 3333: HTTP headers are exposed as
        ``HTTP_*`` keys, uppercase, with hyphens replaced by underscores.)
    on_auth_event:
        Optional callback ``(user_id_or_None, event_name)``. Exceptions
        raised by the callback are swallowed.
    """

    def __init__(
        self,
        app: Callable[..., Any],
        *,
        onelo: "Onelo",
        cache: SyncAuthCache | None = None,
        cache_ttl: float = 30.0,
        retry_attempts: int = 3,
        retry_total_timeout: float = 2.0,
        header_name: str = "HTTP_AUTHORIZATION",
        accept_query_token: bool = False,
        on_auth_event: Callable[[str | None, str], None] | None = None,
    ) -> None:
        if onelo is None:
            raise ValueError("onelo client is required")
        if not getattr(onelo, "_is_secret_key", False):
            raise ValueError(
                "OneloWsgiMiddleware requires an Onelo client constructed "
                "with secret_key=...; you supplied a publishable key. "
                "Backends must use Onelo(secret_key='onelo_sk_live_...')."
            )
        if retry_attempts < 1:
            raise ValueError("retry_attempts must be >= 1")
        if retry_total_timeout <= 0:
            raise ValueError("retry_total_timeout must be > 0")

        self.app = app
        self._onelo = onelo
        self._cache = cache or InProcessSyncAuthCache()
        self._cache_ttl = cache_ttl
        self._retry_attempts = retry_attempts
        self._retry_total_timeout = retry_total_timeout
        self._header_name = header_name
        self._accept_query_token = accept_query_token
        self._on_auth_event = on_auth_event

    def _resolve_user(self, token: str) -> OneloUser | None:
        token_hash = _hash_token(token)

        cached = self._cache.get(token_hash)
        if cached is not None:
            _safe_emit(self._on_auth_event, cached.id, "auth.verify")
            return cached

        try:
            user = verify_token_sync(
                self._onelo,
                token,
                retry_attempts=self._retry_attempts,
                retry_total_timeout=self._retry_total_timeout,
            )
        except OneloAuthUnavailable:
            # Backend unreachable — propagate so __call__ flags it; the app must
            # be able to 503 rather than silently serve as anonymous (A1).
            _safe_emit(self._on_auth_event, None, "auth.fail.unavailable")
            raise
        except OneloAuthError as exc:
            event = f"auth.fail.{type(exc).__name__}"
            _safe_emit(self._on_auth_event, None, event)
            return None
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "unexpected auth error; treating as unauth", exc_info=True
            )
            _safe_emit(self._on_auth_event, None, "auth.fail.unexpected")
            return None

        self._cache.set(token_hash, user, self._cache_ttl)
        _safe_emit(self._on_auth_event, user.id, "auth.verify")
        return user

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        authorization = environ.get(self._header_name)
        token = _extract_bearer(authorization)
        if token is None and self._accept_query_token:
            # SSE/EventSource clients can't set an Authorization header (A4).
            token = _token_from_query_string(environ.get("QUERY_STRING") or "")

        # True ONLY when the backend was unreachable — lets the app tell
        # "couldn't verify" apart from "anonymous" and return 503 instead of
        # silently serving the request unauthenticated (A1). Read via
        # environ.get("onelo.auth_unavailable").
        environ["onelo.auth_unavailable"] = False
        if token is None:
            environ["onelo.user"] = None
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
        else:
            try:
                environ["onelo.user"] = self._resolve_user(token)
            except OneloAuthUnavailable:
                environ["onelo.user"] = None
                environ["onelo.auth_unavailable"] = True

        return self.app(environ, start_response)


__all__ = ["OneloWsgiMiddleware"]
