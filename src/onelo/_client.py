"""User-facing Onelo client.

Wires together cache, features facade, background thread, identify, close,
context manager, atexit, and fork handling. This is the only class users
import directly.
"""
import asyncio
import atexit
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import urlparse

import httpx

from onelo._backend import BackendThread
from onelo._cache import ThreadSafeCache
from onelo._detection import resolve_strategy
from onelo._features import Feature, FeaturesClient
from onelo._http import create_async_client
from onelo._instance import get_instance_id


logger = logging.getLogger("onelo")

_ANY_KEY_RE = re.compile(r"^onelo_(pk|sk)_[a-zA-Z0-9_]+$")

# Debounce window for auto-discovery batch-ping. Matches Swift's
# `_scheduleBatchPing` 1s nanosleep so the cross-platform "first call to
# `feature(x)` reaches the dashboard within ~1s" contract is identical.
_BATCH_PING_DEBOUNCE_SECONDS = 1.0

# Per-request identify() misuse detection. Identity is process-global, so a
# multi-user backend calling identify(user.id) on every request races
# (user A evaluated with user B's targeting) and forces an SSE reconnect on
# every switch. A legitimate single-user process (CLI, worker, single-tenant
# service) never switches between this many DISTINCT identities this fast —
# 5 distinct user_ids within 60s is a reliable signal of the antipattern.
_IDENTIFY_CHURN_WINDOW_SECONDS = 60.0
_IDENTIFY_CHURN_THRESHOLD = 5


class Onelo:
    """Onelo SDK client — main user-facing surface.

    Example (FastAPI):
        from contextlib import asynccontextmanager
        from onelo import Onelo

        # One env var per deployment — paste a test publishable key
        # (onelo_pk_test_*) in dev/staging or a live secret key
        # (onelo_sk_live_*) in prod. The SDK detects which kind it is
        # from the prefix.
        onelo = Onelo(key=os.environ["ONELO_KEY"])

        @asynccontextmanager
        async def lifespan(app):
            onelo.ready(timeout=2.0)
            yield
            onelo.close()

        @app.get("/chat")
        async def chat(user: User = Depends(current_user)):
            if not onelo.features.feature("chat-stream").is_enabled:
                raise HTTPException(404)

    Note: `identify()` sets a single process-global identity — see its
    docstring before calling it in a multi-user backend.
    """

    def __init__(
        self,
        key: str | None = None,
        *,
        publishable_key: str | None = None,
        secret_key: str | None = None,
        api_url: str = "https://app.onelo.tools",
        strategy: str = "auto",
        poll_interval: float = 30.0,
        request_timeout: float = 5.0,
        log_level: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        app_version: str | None = None,
        feature_environment: str | None = None,
        **deprecated: object,   # forward-compat: tolerate removed/unknown kwargs
    ) -> None:
        # Forward-compat: never crash on a kwarg we no longer accept (e.g. the
        # removed `discovery_key`). Warn + ignore so an SDK upgrade can't break a
        # caller's startup; an unknown kwarg is likely a typo, so warn too.
        if deprecated:
            import logging
            _dep_log = logging.getLogger("onelo")
            for _name in deprecated:
                _dep_log.warning(
                    "Onelo() got an unexpected keyword argument %r — ignored "
                    "(forward-compat). Remove it from your Onelo(...) call or "
                    "check for a typo.",
                    _name,
                )
        # ── Primary key resolution ────────────────────────────────────
        # The primary key is the server-trust credential — used for SSE
        # stream, identify, monitor, auth verification, and every other
        # operation. In production this is always your live secret key
        # (`onelo_sk_live_*`). Pass it via `key=`, `secret_key=`, or
        # `publishable_key=`; exactly one is required.
        provided = [p for p in (key, publishable_key, secret_key) if p]
        if not provided:
            raise ValueError(
                "Onelo requires a key — pass key=, publishable_key=, or secret_key="
            )
        if len(provided) > 1:
            raise ValueError(
                "Provide exactly one of key=, publishable_key=, secret_key=, not multiple"
            )
        chosen_key = provided[0]
        if not _ANY_KEY_RE.match(chosen_key):
            raise ValueError(
                "Key must match "
                f"{_ANY_KEY_RE.pattern!r} (onelo_pk_* or onelo_sk_*), "
                f"got {chosen_key!r}"
            )
        # If caller used the generic `key=`, classify by prefix so the rest
        # of the client behaves identically to the explicit forms. The
        # `_sk_` segment appears in the middle of `onelo_sk_*` keys, so a
        # substring check beats a startswith chain.
        if secret_key is not None:
            is_secret = True
        elif publishable_key is not None:
            is_secret = False
        else:
            is_secret = "_sk_" in chosen_key
        # ── Explicit feature environment ──────────────────────────────
        # Decouples the feature snapshot (test|live) from the key prefix so a
        # server on a live secret key can read the Test snapshot — and a client
        # and its backend, sharing this value, resolve the same env. Precedence:
        # constructor arg → ONELO_FEATURE_ENVIRONMENT env var → unset. When
        # unset the field is omitted from requests and the backend falls back to
        # the key prefix (old behavior). See
        # docs/architecture/feature-environment-explicit.md.
        feat_env = (
            feature_environment
            if feature_environment is not None
            else os.environ.get("ONELO_FEATURE_ENVIRONMENT")
        )
        # An env var that's set-but-blank ("" or whitespace) means "unset" — deploy
        # tooling commonly exports ONELO_FEATURE_ENVIRONMENT unconditionally as an
        # empty string. Treat that as None (fall back by key prefix) rather than
        # crashing the caller's startup. Genuine typos (e.g. "bogus") still raise.
        if isinstance(feat_env, str):
            feat_env = feat_env.strip() or None
        if feat_env not in (None, "test", "live"):
            raise ValueError(
                "feature_environment must be 'test', 'live', or None "
                f"(ONELO_FEATURE_ENVIRONMENT), got {feat_env!r}"
            )
        self._feature_environment: str | None = feat_env
        parsed = urlparse(api_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"api_url must be an http(s) URL, got {api_url!r}"
            )
        # ── Optional logging convenience ──────────────────────────────
        if log_level is not None:
            logging.getLogger("onelo").setLevel(log_level)
        # ── State ─────────────────────────────────────────────────────
        self._key = chosen_key
        self._is_secret_key = is_secret
        self._api_url = api_url.rstrip("/")
        self._strategy = resolve_strategy(strategy)
        self._poll_interval = poll_interval
        self._request_timeout = request_timeout
        self._transport = transport
        self._app_version = app_version
        self._user_id: str | None = None
        self._user_id_lock = threading.Lock()
        # Timestamps of identity SWITCHES (new user_id != previous) for the
        # per-request identify() misuse warning. Guarded by _user_id_lock.
        self._identify_switches: deque[float] = deque()
        self._identify_churn_warned = False
        self._closed = False
        self._pid = os.getpid()
        self._cache = ThreadSafeCache()
        # Debounce state for auto-discovery batch-ping. The pending task is
        # owned by the backend's asyncio loop and reset on each schedule call.
        self._pending_ping_task: asyncio.Task | None = None
        self.features = FeaturesClient(
            cache=self._cache,
            schedule_batch_ping_callback=self._schedule_batch_ping,
            ready_callback=self._features_ready,
            # Per-user resolve (features.for_user) — stateless, multi-user-safe.
            api_url=self._api_url,
            publishable_key=self._key,
            request_timeout=self._request_timeout,
            transport=self._transport,
            feature_environment=self._feature_environment,
        )
        # ── Background thread ─────────────────────────────────────────
        self._backend = BackendThread(
            api_url=self._api_url,
            publishable_key=self._key,
            cache=self._cache,
            strategy=self._strategy,
            get_user_id=self._get_user_id,
            poll_interval=self._poll_interval,
            request_timeout=self._request_timeout,
            transport=self._transport,
            app_version=self._app_version,
            on_discovery_requested=self._on_discovery_requested,
            feature_environment=self._feature_environment,
        )
        self._backend.start()
        # ── Cleanup hooks ─────────────────────────────────────────────
        atexit.register(self.close)
        # Fork handling — only on platforms that support fork (Unix).
        if hasattr(os, "register_at_fork"):
            os.register_at_fork(after_in_child=self._after_fork)

    # ── Public API ────────────────────────────────────────────────────

    def identify(self, user_id: str | None) -> None:
        """Set the PROCESS-GLOBAL user identity for feature targeting.

        The SDK holds one resolved snapshot per process; setting a new
        identity swaps the whole cache to that user's targeted state via
        a full SSE reconnect. Meant for processes that act as a single
        user — CLI tools, worker jobs, single-tenant services. Do NOT
        call per request in a multi-user backend: concurrent requests
        share the one identity (user A can be evaluated with user B's
        targeting) and every switch costs a reconnect. With no identity
        set, flags resolve to their global (non-targeted) state.
        Idempotent for the same user_id."""
        with self._user_id_lock:
            switched = user_id != self._user_id and user_id is not None
            self._user_id = user_id
            if switched and not self._identify_churn_warned:
                now = time.monotonic()
                self._identify_switches.append(now)
                cutoff = now - _IDENTIFY_CHURN_WINDOW_SECONDS
                while self._identify_switches and self._identify_switches[0] < cutoff:
                    self._identify_switches.popleft()
                if len(self._identify_switches) >= _IDENTIFY_CHURN_THRESHOLD:
                    self._identify_churn_warned = True
                    self._identify_switches.clear()
                    logger.warning(
                        "identify() was called with %d distinct user ids within %.0fs — "
                        "this looks like per-request identify() in a multi-user backend. "
                        "Identity is PROCESS-GLOBAL: concurrent requests share one identity "
                        "(user A can be evaluated with user B's targeting) and every switch "
                        "forces a full SSE reconnect. If your flags have no per-user "
                        "targeting, do not call identify() at all; otherwise identify() "
                        "belongs only in single-user processes (CLI, worker, single-tenant "
                        "service). This warning is shown once per process.",
                        _IDENTIFY_CHURN_THRESHOLD,
                        _IDENTIFY_CHURN_WINDOW_SECONDS,
                    )
        self._backend.signal_user_change()

    def ready(self, timeout: float = 1.5) -> bool:
        """Block until first SSE event (or polling response) is received.

        Returns True if event received within timeout, False otherwise.
        Defensive: re-creates the background thread if a fork left the
        previous one in the parent process.
        """
        if os.getpid() != self._pid:
            self._after_fork()
        return self._backend.first_event.wait(timeout=timeout)

    def close(self) -> None:
        """Stop the background thread and release resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._backend.stop(timeout=2.0)
        except Exception:
            logger.exception("error during Onelo.close()")

    def __enter__(self) -> "Onelo":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Internals ─────────────────────────────────────────────────────

    def _get_user_id(self) -> str | None:
        with self._user_id_lock:
            return self._user_id

    def _features_ready(self, timeout: float) -> bool:
        """Backing impl for `onelo.features.ready()`. Same fork-safety as the
        client-level `ready()` so re-entering from a forked child works."""
        if os.getpid() != self._pid:
            self._after_fork()
        return self._backend.first_event.wait(timeout=timeout)

    def _on_discovery_requested(self) -> None:
        """Re-batch-ping known slug names so the dashboard's "Discover
        Features" click reaches this process. Uses the auto-discovery set
        (everything the dev has called `feature(...)` or `declare([...])`
        on) instead of just the cache — so locally-seen slugs that the
        server hasn't confirmed yet still reach the registry. Live keys
        bump `last_seen_at` only; test keys also INSERT unknown slugs
        (gating handled server-side).

        Fires IMMEDIATELY (bypassing the 1s debounce). This is deliberate
        and the asymmetry vs Swift's `_scheduleBatchPing()` is load-bearing:

          • Swift runs on a client where `feature()` calls come in tight
            bursts during view renders and then quiet down. The 1s debounce
            always settles in those quiet gaps, so Swift can route
            discovery_requested through the same debounced path safely.
          • Python runs server-side under continuous request traffic. With
            cancel-and-reschedule debounce, every concurrent `feature()`
            call resets the timer — under steady load the debounce window
            can fail to settle for arbitrarily long, leaving newly seen
            slugs trapped in the in-memory discovered set.
          • `discovery_requested` is the dashboard's explicit "flush now"
            signal: the user just clicked Discover Features and is waiting
            for a result. Routing it through the same debounce would mean
            a busy backend never reports back, and the click silently
            does nothing. Firing immediately is the safety valve that
            guarantees the discovered set reaches the server regardless
            of traffic.
        """
        names = self.features.snapshot_discovered()
        if names:
            self._fire_batch_ping(names)

    def _schedule_batch_ping(self) -> None:
        """Debounced auto-discovery ping. Coalesces a burst of `feature()` /
        `declare()` calls into a single network round-trip after the window
        elapses. Safe to call from any thread — the timer task is created on
        the backend's asyncio loop via `call_soon_threadsafe`."""
        loop = self._backend._loop
        if loop is None:
            # Backend not ready yet (rare — happens between __init__ and the
            # daemon thread setting _loop). Drop quietly; the next call
            # after startup will catch up because the slug is still in
            # FeaturesClient._discovered.
            return
        try:
            loop.call_soon_threadsafe(self._reset_pending_ping_task)
        except RuntimeError:
            pass  # loop closed

    def _reset_pending_ping_task(self) -> None:
        """Runs on the backend loop. Cancels any in-flight debounce timer
        and starts a fresh one."""
        if self._pending_ping_task is not None and not self._pending_ping_task.done():
            self._pending_ping_task.cancel()
        self._pending_ping_task = asyncio.get_event_loop().create_task(
            self._debounced_batch_ping()
        )

    async def _debounced_batch_ping(self) -> None:
        try:
            await asyncio.sleep(_BATCH_PING_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        names = self.features.snapshot_discovered()
        if names:
            await self._batch_ping_async(names)

    def _fire_batch_ping(self, names: list[str]) -> None:
        """Schedule an immediate fire-and-forget batch-ping POST on the
        background loop, bypassing the debounce. Used only by
        `_on_discovery_requested` — see that method's docstring for the
        full rationale on why discovery skips the debounce (short version:
        cancel-and-reschedule debounce can fail to settle under steady
        server traffic, so the dashboard's "flush now" signal needs a
        direct path)."""
        if not names:
            return
        if self._backend._loop is None:
            logger.warning("batch_ping called before backend loop is ready; dropping")
            return
        coro = self._batch_ping_async(names)
        try:
            self._backend._loop.call_soon_threadsafe(
                lambda: self._backend._loop.create_task(coro)
            )
        except RuntimeError:
            pass  # loop closed

    async def _batch_ping_async(self, names: list[str]) -> None:
        # The registry-mutating call uses the primary key. Test presence and
        # the Discover-Features signal come from feature_environment="test"
        # (sent as the `environment` body field below), with the binding
        # anchored on app+instance via X-Onelo-Instance-Id.
        ping_key = self._key
        # Test keys require X-Onelo-Instance-Id on every batch-ping (dual
        # anchor binding — see backend `sdk_features.py:123-132`). Without
        # it the server returns 403 missing_instance_id. We always send the
        # header, regardless of key type, because the live-key path simply
        # ignores it — cheap belt-and-braces.
        client = create_async_client(timeout=self._request_timeout, transport=self._transport)
        body: dict = {"publishableKey": ping_key, "features": names}
        # Explicit env wins over the key prefix server-side (effective_env); when
        # unset we omit it so the backend derives env from ping_key (old path).
        if self._feature_environment is not None:
            body["environment"] = self._feature_environment
        try:
            resp = await client.post(
                f"{self._api_url}/api/sdk/features/batch-ping",
                json=body,
                headers={"X-Onelo-Instance-Id": get_instance_id()},
            )
            if resp.status_code >= 400:
                # A 4xx here is almost always a bound test key rejecting a
                # different device/app (`key_bound_to_different_device` /
                # `key_bound_to_different_app`). Staying silent means the dev
                # never learns why discovery does nothing — surface the
                # backend's error code and remedy (dashboard → Features →
                # Deploy access → Unbind) at warning level.
                logger.warning(
                    "batch-ping rejected (HTTP %s): %s",
                    resp.status_code,
                    resp.text[:300],
                )
        except httpx.HTTPError as exc:
            # Network-level failure (timeout, DNS, conn reset) — transient,
            # debounced ping will retry on the next discovery; keep at debug.
            logger.debug("batch-ping failed: %s", exc)
        finally:
            await client.aclose()

    def _after_fork(self) -> None:
        """Restart the background thread in the forked child process.

        Called automatically via os.register_at_fork. The parent's daemon
        thread doesn't survive fork; we replace the BackendThread and the
        cache with fresh instances so the child gets its own state.
        """
        self._pid = os.getpid()
        self._closed = False
        self._cache = ThreadSafeCache()
        # Drop the parent's debounce timer reference — that asyncio.Task was
        # bound to a loop that no longer exists in this process. A fresh
        # task will be created on the new backend loop the next time the
        # user calls `feature()` / `declare()`.
        self._pending_ping_task = None
        self.features = FeaturesClient(
            cache=self._cache,
            schedule_batch_ping_callback=self._schedule_batch_ping,
            ready_callback=self._features_ready,
            # Per-user resolve (features.for_user) — stateless, multi-user-safe.
            api_url=self._api_url,
            publishable_key=self._key,
            request_timeout=self._request_timeout,
            transport=self._transport,
            # Must survive fork: a preload+fork server (gunicorn/uwsgi) that set
            # an explicit test/live env would otherwise silently revert to the
            # key-prefix env in every worker and resolve the wrong snapshot.
            feature_environment=self._feature_environment,
        )
        self._backend = BackendThread(
            api_url=self._api_url,
            publishable_key=self._key,
            cache=self._cache,
            strategy=self._strategy,
            get_user_id=self._get_user_id,
            poll_interval=self._poll_interval,
            request_timeout=self._request_timeout,
            transport=self._transport,
            app_version=self._app_version,
            on_discovery_requested=self._on_discovery_requested,
            feature_environment=self._feature_environment,
        )
        self._backend.start()
