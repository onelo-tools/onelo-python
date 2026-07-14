"""Onelo auth helpers — token verification + typed user model.

Public, framework-agnostic surface used by:
  * `onelo.fastapi.RequireUser` / `OptionalUser` (FastAPI dependency)
  * Direct callers in websockets / background workers / non-FastAPI stacks
    (use ``verify_token(client, token)``).

Design notes
------------
* ``OneloUser`` is implemented as a ``dataclass`` rather than a Pydantic
  ``BaseModel`` so it can be re-exported from ``onelo.__init__`` without
  forcing a hard dependency on Pydantic. The ``[fastapi]`` extra installs
  Pydantic only because FastAPI needs it; FastAPI happily accepts
  dataclasses in response models / dependency results.
* The HTTP call shape mirrors what backends already use today:
  ``GET {api_url}/api/sdk/auth/user`` with
  ``Authorization: Bearer <user_token>`` and either
  ``X-Onelo-Secret-Key`` or ``X-Publishable-Key``. We send both headers
  with the secret key — backend prefers the dedicated header but accepts
  the legacy one for older deployments.
* The backend response schema today is
  ``{"id", "email", "created_at", "metadata"}``. ``email_verified`` and
  ``plan`` are not always present at the top level — we fall back to
  reading them from ``metadata`` when missing, and otherwise default
  sensibly. The complete payload is preserved on ``OneloUser.raw``.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from onelo._client import Onelo

logger = logging.getLogger("onelo.auth")


# ── Errors ────────────────────────────────────────────────────────────


class OneloAuthError(Exception):
    """Base class for all auth verification failures."""


class OneloAuthMissingToken(OneloAuthError):
    """No bearer token was supplied (header missing / malformed / empty)."""


class OneloAuthInvalidToken(OneloAuthError):
    """Backend returned 401 — token expired, revoked, or malformed."""


class OneloAuthForbidden(OneloAuthError):
    """Backend returned 403 — token valid but principal not allowed.

    Also raised by the FastAPI integration when an authenticated user
    fails an additional gate (``require_email_verified`` / ``require_plan``).
    """


class OneloAuthUnavailable(OneloAuthError):
    """Onelo backend returned a 5xx and retries were exhausted."""


class OneloAuthRateLimited(OneloAuthUnavailable):
    """Onelo backend returned 429 Too Many Requests.

    Subclass of :class:`OneloAuthUnavailable` so existing handlers that map
    "backend unavailable" to a 503 keep working — BUT the retry loops treat it
    specially: they do NOT retry a rate-limit (retrying immediately only makes
    it worse and burns the wall-clock budget). ``retry_after`` carries the
    parsed ``Retry-After`` seconds when the backend supplied one.
    """

    def __init__(self, *args: Any, retry_after: float | None = None) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


# ── HTTP client pooling ───────────────────────────────────────────────
# Reuse a keep-alive client across verifications instead of building a fresh one
# (TCP + TLS handshake) on every cache-miss. Only when no test transport is
# injected — tests pass a MockTransport and get an ephemeral client so they stay
# isolated. A backend runs one long-lived event loop, so the async pool is
# effectively one reused client per process.
_sync_clients: dict[float, httpx.Client] = {}
_sync_clients_lock = threading.Lock()
# Keyed by the running event LOOP OBJECT (not id()) via a WeakKeyDictionary:
# httpx.AsyncClient's connection pool is loop-bound, so we need one per loop —
# and weak keys auto-evict a loop's clients when that loop is garbage-collected.
# This is critical for transient-loop callers (websockets / Celery / RQ tasks
# that do asyncio.run(verify_token(...)) per job): id()-keying would leak a
# client per loop forever AND could hand back a dead-loop client after id reuse.
# A long-lived server loop (uvicorn) simply reuses its one client across requests.
_async_clients: "weakref.WeakKeyDictionary[Any, dict[float, httpx.AsyncClient]]" = (
    weakref.WeakKeyDictionary()
)


def _get_pooled_sync_client(timeout: float) -> httpx.Client:
    """A shared, thread-safe sync client keyed by timeout (``httpx.Client`` is
    safe to use from multiple threads). Rebuilt if a prior one was closed."""
    with _sync_clients_lock:
        c = _sync_clients.get(timeout)
        if c is None or c.is_closed:
            c = httpx.Client(timeout=timeout)
            _sync_clients[timeout] = c
        return c


def _get_pooled_async_client(timeout: float) -> httpx.AsyncClient:
    """A per-event-loop async client, keyed by the loop object so it auto-evicts
    when the loop is GC'd. Rebuilt if a prior one was closed. Only ever called
    from within a running loop (async verify_token). No lock needed: coroutines
    on one loop are cooperatively scheduled and there is no await between the
    get and the set, so the get-or-create is atomic per loop."""
    loop = asyncio.get_running_loop()
    by_timeout = _async_clients.get(loop)
    if by_timeout is None:
        by_timeout = {}
        _async_clients[loop] = by_timeout
    c = by_timeout.get(timeout)
    if c is None or c.is_closed:
        c = httpx.AsyncClient(timeout=timeout)
        by_timeout[timeout] = c
    return c


def _token_from_query_string(query_string: str | bytes) -> str | None:
    """Extract a ``token`` query parameter from a RAW query string. Used by the
    ASGI/WSGI middlewares (they receive the raw string, not a parsed dict) when
    ``accept_query_token`` is on — e.g. browser EventSource/SSE clients that
    cannot set an Authorization header. Opt-in because a token in a URL can leak
    via access logs / Referer."""
    from urllib.parse import parse_qs

    if isinstance(query_string, bytes):
        query_string = query_string.decode("latin-1", "ignore")
    values = parse_qs(query_string).get("token")
    if not values:
        return None
    tok = values[0].strip()
    return tok or None


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header (seconds form). ``None`` if absent /
    invalid / negative. HTTP-date form isn't supported (Onelo sends seconds)."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


# ── Data model ────────────────────────────────────────────────────────


@dataclass
class OneloUser:
    """An authenticated Onelo user, as returned by /api/sdk/auth/user.

    Attributes
    ----------
    id:
        Stable Onelo user id (``app_user_id`` or Supabase user id).
    email:
        User's primary email.
    email_verified:
        Whether the email is verified. Defaults to ``False`` when the
        backend does not surface verification state.
    plan:
        The user's current plan (``free`` / ``pro`` / etc), if known.
    metadata:
        Free-form metadata dict surfaced by the backend.
    raw:
        Full JSON payload returned by the backend, in case you need
        fields the dataclass does not model.
    """

    id: str
    email: str
    email_verified: bool = False
    plan: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OneloUser":
        """Map a backend ``/api/sdk/auth/user`` response to an OneloUser.

        Tolerant of missing fields — the backend currently does not
        expose ``email_verified`` or ``plan`` at the top level for SDK
        users; we fall back to ``metadata`` when present.
        """
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # email_verified: top-level wins, otherwise metadata, otherwise False.
        email_verified = payload.get("email_verified")
        if email_verified is None:
            email_verified = metadata.get("email_verified", False)
        email_verified = bool(email_verified)

        # plan: top-level wins, otherwise metadata.plan, otherwise None.
        plan = payload.get("plan")
        if plan is None:
            plan = metadata.get("plan")

        return cls(
            id=str(payload.get("id") or ""),
            email=str(payload.get("email") or ""),
            email_verified=email_verified,
            plan=plan,
            metadata=metadata,
            raw=dict(payload),
        )


# ── Verification ──────────────────────────────────────────────────────


def _map_response(resp: httpx.Response) -> OneloUser:
    """Map a ``/api/sdk/auth/user`` response to an OneloUser or raise the typed
    auth error. Shared by the async and sync paths so the status contract (incl.
    429 rate-limit handling) can never drift between them."""
    status = resp.status_code
    if status == 200:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OneloAuthUnavailable(
                f"backend returned 200 with invalid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            # Valid JSON but not an object (null / list / string — e.g. a proxy
            # or CDN error page served as application/json). Treat as a malformed
            # backend response, not an authenticated user (A6).
            raise OneloAuthUnavailable("backend returned 200 with a non-object body")
        user = OneloUser.from_payload(payload)
        if not user.id:
            # A 200 without a usable id is a malformed backend response, NOT a
            # valid anonymous user — never let it pass as authenticated (A6).
            raise OneloAuthUnavailable("backend returned 200 without a user id")
        return user
    if status == 401:
        raise OneloAuthInvalidToken("invalid or expired token")
    if status == 403:
        raise OneloAuthForbidden("token valid but principal forbidden")
    if status == 429:
        # NOT a generic 5xx: retrying a rate-limit immediately makes it worse
        # (A3). OneloAuthRateLimited subclasses Unavailable (→ still 503 to the
        # caller) but the retry loops skip it.
        raise OneloAuthRateLimited(
            "backend rate-limited auth (429)",
            retry_after=_parse_retry_after(resp.headers.get("Retry-After")),
        )
    if 500 <= status < 600:
        raise OneloAuthUnavailable(f"backend returned {status}")
    # Any other unexpected status — treat as auth failure conservatively.
    raise OneloAuthUnavailable(f"unexpected status {status}")


async def verify_token(client: "Onelo", token: str) -> OneloUser:
    """Verify a user-supplied access token against the Onelo backend.

    This is the low-level helper for non-FastAPI usage (websockets,
    Celery / RQ tasks, custom middleware, etc). The FastAPI integration
    layers caching, retries, and gates on top — call ``RequireUser``
    instead from FastAPI handlers.

    Parameters
    ----------
    client:
        An ``Onelo`` instance. **Must** be initialized with a secret key
        — verifying a user token requires server-side credentials.
    token:
        The user's access token (the one returned by the Onelo SDK on
        the client after sign-in). Do NOT pass your secret key.

    Raises
    ------
    OneloAuthMissingToken
        If ``token`` is empty / whitespace.
    OneloAuthInvalidToken
        Backend returned 401.
    OneloAuthForbidden
        Backend returned 403.
    OneloAuthUnavailable
        Backend returned 5xx, network failure, or unexpected status.
    """
    if not token or not token.strip():
        raise OneloAuthMissingToken("token is empty")

    if not getattr(client, "_is_secret_key", False):
        raise OneloAuthError(
            "verify_token requires an Onelo client constructed with "
            "secret_key=...; you supplied a publishable key. "
            "Backends must use Onelo(secret_key='onelo_sk_live_...')."
        )

    api_url = client._api_url
    secret_key = client._key
    transport = getattr(client, "_transport", None)
    timeout = getattr(client, "_request_timeout", 5.0)

    headers = {
        "Authorization": f"Bearer {token}",
        # Send both — backend prefers the dedicated header, but the
        # legacy X-Publishable-Key path still works on older deployments.
        "X-Onelo-Secret-Key": secret_key,
        "X-Publishable-Key": secret_key,
    }

    url = f"{api_url}/api/sdk/auth/user"
    try:
        if transport is not None:
            # Ephemeral client for injected (test) transports — keeps tests isolated.
            async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
                resp = await http.get(url, headers=headers)
        else:
            # Pooled keep-alive client — no per-call TLS handshake (A2).
            resp = await _get_pooled_async_client(timeout).get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise OneloAuthUnavailable(f"network error: {exc}") from exc

    return _map_response(resp)


# Sync retry policy — mirrors fastapi.RequireUser._verify_with_retry but
# baked into the low-level sync helper so sync adapters (Flask, Django,
# WSGI) don't each have to re-implement it. Async adapters get retries
# from RequireUser; sync ones get them here.
_SYNC_BACKOFF_BASE = 0.1


def _verify_token_sync_once(
    api_url: str,
    secret_key: str,
    token: str,
    transport: Any,
    timeout: float,
) -> OneloUser:
    """Single attempt — issues one HTTP request and maps the response.

    Raises OneloAuthInvalidToken / OneloAuthForbidden / OneloAuthUnavailable
    just like the async helper. Retry orchestration lives in the public
    ``verify_token_sync`` wrapper.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Onelo-Secret-Key": secret_key,
        "X-Publishable-Key": secret_key,
    }
    url = f"{api_url}/api/sdk/auth/user"
    try:
        if transport is not None:
            with httpx.Client(transport=transport, timeout=timeout) as http:
                resp = http.get(url, headers=headers)
        else:
            # Pooled keep-alive client — no per-call TLS handshake (A2).
            resp = _get_pooled_sync_client(timeout).get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise OneloAuthUnavailable(f"network error: {exc}") from exc

    return _map_response(resp)


def verify_token_sync(
    client: "Onelo",
    token: str,
    *,
    retry_attempts: int = 3,
    retry_total_timeout: float = 2.0,
) -> OneloUser:
    """Synchronous version of verify_token. Same semantics, same exceptions.

    Use this in sync frameworks (Flask, Django, WSGI middleware). For async
    frameworks (FastAPI, Litestar, ASGI), prefer the async ``verify_token``.

    Internally uses ``httpx.Client`` (sync) constructed per-call. Sync
    adapter cache layers compensate for the per-call client construction
    cost. The retry policy mirrors the async FastAPI integration: up to
    ``retry_attempts`` attempts on 5xx with exponential backoff (0.1, 0.2,
    0.4 ...), bounded by ``retry_total_timeout`` seconds wall-clock.

    Parameters
    ----------
    client:
        An ``Onelo`` instance constructed with ``secret_key=...``.
    token:
        The user's access token.
    retry_attempts:
        Total attempts on 5xx (including initial). Default 3.
    retry_total_timeout:
        Hard wall-clock cap (seconds) for the entire retry sequence.
        Default 2.0s.

    Raises
    ------
    OneloAuthMissingToken, OneloAuthInvalidToken, OneloAuthForbidden,
    OneloAuthUnavailable — same as ``verify_token``.
    """
    import time as _time

    if not token or not token.strip():
        raise OneloAuthMissingToken("token is empty")

    if not getattr(client, "_is_secret_key", False):
        raise OneloAuthError(
            "verify_token_sync requires an Onelo client constructed with "
            "secret_key=...; you supplied a publishable key. "
            "Backends must use Onelo(secret_key='onelo_sk_live_...')."
        )
    if retry_attempts < 1:
        raise ValueError("retry_attempts must be >= 1")
    if retry_total_timeout <= 0:
        raise ValueError("retry_total_timeout must be > 0")

    api_url = client._api_url
    secret_key = client._key
    transport = getattr(client, "_transport", None)
    timeout = getattr(client, "_request_timeout", 5.0)

    deadline = _time.monotonic() + retry_total_timeout
    last_exc: OneloAuthUnavailable | None = None

    for attempt in range(retry_attempts):
        try:
            return _verify_token_sync_once(
                api_url, secret_key, token, transport, timeout
            )
        except OneloAuthUnavailable as exc:
            last_exc = exc
            # A rate-limit is not transient like a 5xx — retrying immediately
            # only deepens it and burns the budget. Fail fast (A3).
            if isinstance(exc, OneloAuthRateLimited):
                break
            next_attempt = attempt + 1
            if next_attempt >= retry_attempts:
                break
            backoff = _SYNC_BACKOFF_BASE * (2 ** attempt)
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            _time.sleep(min(backoff, remaining))
            if _time.monotonic() >= deadline:
                break

    assert last_exc is not None
    raise last_exc


__all__ = [
    "OneloUser",
    "OneloAuthError",
    "OneloAuthMissingToken",
    "OneloAuthInvalidToken",
    "OneloAuthForbidden",
    "OneloAuthUnavailable",
    "OneloAuthRateLimited",
    "verify_token",
    "verify_token_sync",
]
