"""FastAPI integration for Onelo auth.

Optional — requires ``pip install 'onelo[fastapi] @ git+https://github.com/onelo-tools/onelo-python.git@staging'``.

Quickstart
----------

.. code-block:: python

    import os
    from fastapi import FastAPI, Depends
    from onelo import Onelo
    from onelo.fastapi import RequireUser, OneloUser

    onelo = Onelo(secret_key=os.environ["ONELO_SECRET_KEY"])
    require_user = RequireUser(onelo)

    app = FastAPI()

    @app.get("/me")
    async def me(user: OneloUser = Depends(require_user)):
        return {"id": user.id, "email": user.email}

Status code mapping
-------------------
* **401** — missing or invalid token
* **403** — token valid but ``require_email_verified`` / ``require_plan``
  gate failed (or backend itself returned 403)
* **503** — Onelo backend unreachable after retry exhaustion
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

# Lazy import — surface a helpful error when the [fastapi] extra is missing.
try:
    from fastapi import Header, HTTPException, Query
except ImportError as exc:  # pragma: no cover - exercised only without extra
    raise ImportError(
        "onelo.fastapi requires the [fastapi] extra: "
        "pip install 'onelo[fastapi] @ git+https://github.com/onelo-tools/onelo-python.git@staging'"
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

logger = logging.getLogger("onelo.fastapi")


# Per-spec retry schedule (seconds). Indexed by attempt number after the
# initial try: 0.1, 0.2, 0.4 ... capped by ``retry_total_timeout``.
_BACKOFF_BASE = 0.1


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


class RequireUser:
    """FastAPI dependency that verifies an Onelo bearer token.

    Use as ``Depends(require_user)`` where ``require_user`` is an
    instance configured with your ``Onelo`` client. The instance is
    callable and FastAPI will inject the request headers automatically.

    Parameters
    ----------
    client:
        An ``Onelo`` instance constructed with ``secret_key=...``. A
        ``ValueError`` is raised at construction time if a publishable
        key was used — verifying user tokens requires server credentials.
    cache:
        Optional ``AuthCache`` implementation. Defaults to
        ``InProcessAuthCache()``.
    cache_ttl:
        TTL in seconds for cached verifications. Default 30s.
    accept_query_token:
        When ``True``, falls back to ``?token=...`` if the
        ``Authorization`` header is missing. Useful for SSE / EventSource
        clients which cannot set custom headers. Default ``False``.
    require_email_verified:
        When ``True``, rejects users whose ``email_verified`` flag is
        falsy with HTTP 403.
    require_plan:
        Optional list of plan names. When set, the user's ``plan`` must
        be in the list or they are rejected with HTTP 403.
    retry_attempts:
        Total number of attempts on 5xx (including the initial try).
        Default 3 — i.e. up to 2 retries.
    retry_total_timeout:
        Hard wall-clock cap (seconds) on the *entire* retry sequence,
        including backoff sleeps. Default 2.0s.
    on_auth_event:
        Optional callback ``(user_id_or_None, event_name)`` invoked on
        every verification result (``"auth.verify"``, ``"auth.fail.*"``).
        Exceptions raised by the callback are swallowed.
    identify_monitor:
        When ``True`` (default) and ``onelo.monitor`` is initialised, the
        resolved user's **id** is attached to the active monitor isolation
        scope so monitor events captured during the request carry
        ``user_id`` — the same canonical id the frontend SDK reports, so a
        person looks identical across your app and backend in the dashboard.
        Only the **opaque id** is propagated, never email / name: monitor
        events are logs, and logs must stay PII-free (the dashboard resolves
        the id → identity live at display time from a separate, access-
        controlled store — pseudonymisation, GDPR Art. 4(5)). Set ``False``
        to opt out.
    """

    def __init__(
        self,
        client: "Onelo",
        *,
        cache: AuthCache | None = None,
        cache_ttl: float = 30.0,
        accept_query_token: bool = False,
        require_email_verified: bool = False,
        require_plan: list[str] | None = None,
        retry_attempts: int = 3,
        retry_total_timeout: float = 2.0,
        on_auth_event: Callable[[str | None, str], None] | None = None,
        identify_monitor: bool = True,
    ) -> None:
        if not getattr(client, "_is_secret_key", False):
            raise ValueError(
                "RequireUser requires an Onelo client constructed with "
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
        self._accept_query_token = accept_query_token
        self._require_email_verified = require_email_verified
        self._require_plan = set(require_plan) if require_plan else None
        self._retry_attempts = retry_attempts
        self._retry_total_timeout = retry_total_timeout
        self._on_auth_event = on_auth_event
        self._identify_monitor = identify_monitor

    # ── Internal helpers ──────────────────────────────────────────────

    def _identify_to_monitor(self, user: OneloUser) -> None:
        """Attach the resolved user's **id** to the monitor isolation scope.

        Runs inside the request task, where ``OneloMonitorASGIMiddleware``
        already forked a per-request isolation scope that stays open for the
        whole response (incl. streaming) — so events captured downstream,
        including in StreamingResponse generators, inherit the id. No-op when
        monitor isn't initialised. Only the opaque id is set — never email /
        name — so the log store stays PII-free (see ``identify_monitor``).
        """
        if not self._identify_monitor:
            return
        try:
            from onelo import monitor

            if monitor.is_initialised() and user.id:
                monitor.set_user({"id": user.id})
        except Exception:  # pragma: no cover - identity bridge must never break auth
            logger.warning("monitor identity bridge failed; ignoring", exc_info=True)

    def _extract_token(
        self, authorization: str | None, query_token: str | None
    ) -> str | None:
        """Pull a bearer token out of the request. Returns None if absent."""
        if authorization:
            stripped = authorization.strip()
            # Tolerate any case for the scheme.
            if stripped.lower().startswith("bearer "):
                token = stripped[7:].strip()
                if token:
                    return token
                return None
            # Authorization header present but not a bearer scheme — treat
            # as missing so that the right error code surfaces.
            return None
        if self._accept_query_token and query_token:
            tok = query_token.strip()
            return tok or None
        return None

    def _check_gates(self, user: OneloUser) -> None:
        """Apply post-verify gates. Raises OneloAuthForbidden on failure."""
        if self._require_email_verified and not user.email_verified:
            raise OneloAuthForbidden("email_unverified")
        if self._require_plan is not None:
            if user.plan not in self._require_plan:
                raise OneloAuthForbidden("plan")

    async def _verify_with_retry(self, token: str) -> OneloUser:
        """verify_token + 5xx retry policy with a wall-clock budget."""
        deadline = time.monotonic() + self._retry_total_timeout
        last_exc: OneloAuthUnavailable | None = None

        for attempt in range(self._retry_attempts):
            try:
                return await verify_token(self._client, token)
            except OneloAuthUnavailable as exc:
                last_exc = exc
                # A rate-limit (429) is not transient like a 5xx — retrying
                # immediately only deepens it and burns the budget (A3).
                if isinstance(exc, OneloAuthRateLimited):
                    break
                # Decide whether to retry: must have attempts left AND
                # enough budget for at least the next backoff window.
                next_attempt = attempt + 1
                if next_attempt >= self._retry_attempts:
                    break
                backoff = _BACKOFF_BASE * (2 ** attempt)  # 0.1, 0.2, 0.4
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Don't sleep past the deadline.
                await asyncio.sleep(min(backoff, remaining))
                if time.monotonic() >= deadline:
                    break
        assert last_exc is not None
        raise last_exc

    async def _resolve(
        self, authorization: str | None, query_token: str | None
    ) -> OneloUser | None:
        """Core resolution. Returns OneloUser; raises HTTPException.

        Returns ``None`` is *not* used here — the OptionalUser subclass
        catches HTTPExceptions itself.
        """
        token = self._extract_token(authorization, query_token)
        if token is None:
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
            raise HTTPException(
                status_code=401, detail={"error": "missing_token"}
            )

        token_hash = _hash_token(token)

        # Cache lookup BEFORE any network work.
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
                raise HTTPException(
                    status_code=403,
                    detail={"error": "forbidden", "reason": str(exc)},
                ) from exc
            _safe_emit(self._on_auth_event, cached.id, "auth.verify")
            return cached

        # Miss — verify, with retries on 5xx.
        try:
            user = await self._verify_with_retry(token)
        except OneloAuthMissingToken:
            # Defensive — _extract_token already filtered empties.
            _safe_emit(self._on_auth_event, None, "auth.fail.missing_token")
            raise HTTPException(
                status_code=401, detail={"error": "missing_token"}
            )
        except OneloAuthInvalidToken:
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token"}
            )
        except OneloAuthForbidden:
            _safe_emit(self._on_auth_event, None, "auth.fail.forbidden")
            raise HTTPException(
                status_code=403, detail={"error": "forbidden"}
            )
        except OneloAuthUnavailable:
            _safe_emit(
                self._on_auth_event, None, "auth.fail.unavailable"
            )
            raise HTTPException(
                status_code=503,
                detail={"error": "auth_service_unavailable"},
            )
        except OneloAuthError:
            # Catch-all for any future OneloAuthError subclass.
            _safe_emit(self._on_auth_event, None, "auth.fail.invalid_token")
            raise HTTPException(
                status_code=401, detail={"error": "invalid_token"}
            )

        # Cache the raw verification result; gates are per-request.
        await self._cache.set(token_hash, user, self._cache_ttl)

        try:
            self._check_gates(user)
        except OneloAuthForbidden as exc:
            _safe_emit(
                self._on_auth_event, user.id or None, f"auth.fail.{exc}"
            )
            raise HTTPException(
                status_code=403,
                detail={"error": "forbidden", "reason": str(exc)},
            ) from exc

        _safe_emit(self._on_auth_event, user.id, "auth.verify")
        return user

    # ── FastAPI dependency entrypoint ─────────────────────────────────

    async def __call__(
        self,
        authorization: str | None = Header(None),
        token: str | None = Query(None),
    ) -> OneloUser:
        result = await self._resolve(authorization, token)
        # Non-Optional flavour always returns a user (or has raised).
        assert result is not None
        self._identify_to_monitor(result)
        return result


class OptionalUser(RequireUser):
    """Like ``RequireUser`` but returns ``None`` on missing/invalid token.

    Infrastructure failures (5xx after retries) still raise ``503`` —
    those are operational problems, not authentication outcomes, and
    silently degrading them would mask incidents.
    """

    async def __call__(  # type: ignore[override]
        self,
        authorization: str | None = Header(None),
        token: str | None = Query(None),
    ) -> OneloUser | None:
        try:
            user = await self._resolve(authorization, token)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                return None
            raise
        self._identify_to_monitor(user)
        return user


__all__ = ["RequireUser", "OptionalUser", "OneloUser"]
