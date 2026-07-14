"""Feature dataclass and FeaturesClient façade.

Feature: a value object derived from a wire status string. Property checks
are pure logic — no I/O, no locking. Cheap to instantiate per lookup.

FeaturesClient: the public `onelo.features` surface. Reads from cache,
auto-discovers slug names on every `feature()` / `declare()` call and asks
the client to schedule a debounced batch-ping so the dashboard registry
fills in without explicit dev wiring (parity with Swift).
"""
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from onelo._cache import ThreadSafeCache
from onelo._http import create_async_client
from onelo._version import __version__

logger = logging.getLogger("onelo.features")

# The status vocabulary the backend resolver can emit (resolver.py STATIC_MODES
# + plan gating): enabled / new / beta / coming_soon / greyed / hidden / upsell.
# Availability model — kept 1:1 with Swift's `Feature` so cross-language docs match:
#   available (usable):    enabled, new, beta
#   visible, not usable:   coming_soon, greyed, upsell
#   not visible:           hidden — AND any status this SDK build doesn't know
#                          (fail-closed, see is_visible/is_known)
_ENABLED_STATUSES = frozenset({"enabled", "new", "beta"})
_VISIBLE_STATUSES = frozenset({"enabled", "new", "beta", "coming_soon", "greyed", "upsell"})
_KNOWN_STATUSES = _VISIBLE_STATUSES | frozenset({"hidden"})

# Statuses that mean "this feature's visibility depends on the user's plan".
# A multi-user backend that reads them through the process-global feature()
# path is almost certainly resolving for the wrong (shared/anonymous) identity
# — see _maybe_warn_gated.
_GATED_STATUSES = frozenset({"upsell", "greyed"})

# Hard cap on the per-user snapshot cache (for_user). Bounds memory on a backend
# that resolves a long tail of one-shot user_ids; well above any realistic
# concurrent live-user set, so normal traffic never hits eviction.
_USER_SNAPSHOT_MAX_ENTRIES = 10_000


@dataclass(frozen=True)
class Feature:
    """A single feature's resolved state. Returned by FeaturesClient.feature().

    Carries the backend's full upsell metadata so a SERVER-RENDERED app
    (Django/Flask/FastAPI templates, Next.js SSR via the Node SDK) can build an
    "Available in Pro" CTA — not just a binary on/off. All metadata fields are
    optional; a plain gating check only needs is_enabled/is_visible.
    """
    name: str
    status: str
    reason: str | None = None
    required_plan: str | None = None
    required_plan_label: str | None = None
    upgrade_cta: bool = False

    @classmethod
    def from_wire(cls, name: str, state: object) -> "Feature":
        """Build a Feature from a backend wire state dict. Tolerates a bare
        status string (legacy/degenerate shape) and ignores unknown extra keys
        (forward-compat). Missing status → hidden (fail-closed)."""
        if not isinstance(state, dict):
            return cls(name=name, status=str(state))
        return cls(
            name=name,
            status=state.get("status", "hidden"),
            reason=state.get("reason"),
            required_plan=state.get("required_plan"),
            required_plan_label=state.get("required_plan_label"),
            upgrade_cta=bool(state.get("upgrade_cta", False)),
        )

    @property
    def is_enabled(self) -> bool:
        """True for enabled / new / beta. The default check for binary gating."""
        return self.status in _ENABLED_STATUSES

    @property
    def is_visible(self) -> bool:
        """True for any user-visible status (enabled/new/beta/coming_soon/
        greyed/upsell). False for hidden AND for any status this SDK build does
        not recognise — fail-closed, so a typo or a status added by a newer
        backend never renders as a blank, visible item. Use for trigger/menu
        gating."""
        return self.status in _VISIBLE_STATUSES

    @property
    def is_greyed(self) -> bool:
        return self.status == "greyed"

    @property
    def is_new(self) -> bool:
        return self.status == "new"

    @property
    def is_beta(self) -> bool:
        return self.status == "beta"

    @property
    def is_coming_soon(self) -> bool:
        return self.status == "coming_soon"

    @property
    def is_upsell(self) -> bool:
        """Feature is in upsell mode — shown to user as a paid/locked teaser.
        Mirrors Swift's `Feature.isUpsell` so cross-language docs stay 1:1."""
        return self.status == "upsell"

    @property
    def is_known(self) -> bool:
        """True if `status` is one this SDK build understands. False means the
        backend returned a status newer than this SDK (treated as hidden by
        is_visible/is_enabled) — surface it as a hint to upgrade the SDK."""
        return self.status in _KNOWN_STATUSES

    @property
    def upgrade_hint(self) -> str | None:
        """The human plan LABEL to show as an upsell (e.g. "Pro"), or None when
        the backend attached no label. Render as f"Available in {feat.upgrade_hint}".

        The backend is the authority: it attaches required_plan_label only to a
        blocked-but-visible feature that a real plan upgrade would unlock, and
        never to `hidden`/`enabled` — so this is naturally None outside an
        upsell. It is label-only by design: for the raw "is there an upgrade"
        signal (e.g. to render your own CTA when a plan has no configured
        label) use `required_plan` / `upgrade_cta`. NOTE: this is intentionally
        simpler than Swift's client-side `upgradeHint` (which re-gates on
        reason==plan + the machine key); here the label the backend sent wins."""
        return self.required_plan_label or None


@dataclass(frozen=True)
class UserFeatures:
    """Resolved feature snapshot for ONE specific user — returned by
    FeaturesClient.for_user(). Read-only; safe to share across coroutines.

    Unlike the process-global feature() path (which reads a single cached
    identity), this carries a per-user snapshot and never mutates SDK identity
    — so it is SAFE to use per request/connection in a multi-user backend
    (HTTP handlers, WebSocket servers). No identify(), no SSE reconnect, no
    cross-user race."""

    user_id: str
    _states: dict[str, dict] = field(default_factory=dict)

    def feature(self, name: str) -> Feature:
        """This feature's state (incl. upsell metadata) FOR THIS USER. Missing
        from the snapshot → hidden (fail-closed), mirroring the global
        feature()."""
        return Feature.from_wire(name, self._states.get(name, {"status": "hidden"}))

    def is_enabled(self, name: str) -> bool:
        """Shorthand for feature(name).is_enabled."""
        return self.feature(name).is_enabled


class _UserSnapshotCache:
    """Tiny TTL cache of per-user feature snapshots keyed by user_id. Stops a
    busy multi-user backend from calling /resolve on every request for the same
    user. Thread-safe; entries expire after their stored deadline.

    Bounded — two mechanisms keep it from growing without limit on a backend
    that serves a long tail of one-shot user_ids (the previous version leaked:
    an expired entry lingered until the SAME user returned to overwrite it):
      1. lazy prune — an expired entry is DELETED on the get() that finds it;
      2. hard cap — at more than `max_entries`, put() purges expired entries
         and, if still over, evicts the ones closest to expiry.
    """

    def __init__(self, max_entries: int = _USER_SNAPSHOT_MAX_ENTRIES) -> None:
        self._d: dict[str, tuple[float, dict[str, dict]]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, user_id: str) -> dict[str, dict] | None:
        with self._lock:
            entry = self._d.get(user_id)
            if entry is None:
                return None
            if entry[0] > time.monotonic():
                return entry[1]
            # Expired — delete it now so a long tail of one-shot users can't
            # accumulate dead entries between put()s (real lazy pruning).
            del self._d[user_id]
            return None

    def put(self, user_id: str, snapshot: dict[str, dict], ttl: float) -> None:
        with self._lock:
            self._d[user_id] = (time.monotonic() + ttl, snapshot)
            if len(self._d) > self._max_entries:
                self._evict_locked()

    def _evict_locked(self) -> None:
        """Bring the map back under `max_entries`. Caller must hold `_lock`.
        Drops expired entries first (free to remove); if still over cap, evicts
        the entries closest to expiry (least valuable — they'd lapse soonest)."""
        now = time.monotonic()
        for k in [k for k, (deadline, _) in self._d.items() if deadline <= now]:
            del self._d[k]
        overflow = len(self._d) - self._max_entries
        if overflow > 0:
            for k, _ in sorted(self._d.items(), key=lambda kv: kv[1][0])[:overflow]:
                del self._d[k]

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._d.pop(user_id, None)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


class _DiscoveredNames:
    """Thread-safe set of slug names this process has ever asked about.

    Why a dedicated container: `feature()` is called from arbitrary user
    threads (request handlers, worker pools), but the set drives the
    auto-discovery batch-ping. We need O(1) membership test + insert under
    a lock; using a bare set + module-level lock would leak that detail
    into every call site.
    """

    def __init__(self) -> None:
        self._names: set[str] = set()
        self._lock = threading.Lock()

    def add(self, name: str) -> bool:
        """Insert `name`. Returns True if it was new, False if already present.
        The bool lets the caller decide whether to schedule a ping — avoids
        debounce-thrash on hot paths that look up the same slug per request."""
        with self._lock:
            if name in self._names:
                return False
            self._names.add(name)
            return True

    def add_many(self, names: list[str]) -> bool:
        """Bulk insert. Returns True if at least one name was new."""
        with self._lock:
            before = len(self._names)
            self._names.update(names)
            return len(self._names) > before

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._names)


class FeaturesClient:
    """Public façade for `onelo.features`. Cache-backed reads + auto-discover.

    Every `feature(name)` and `declare([...])` call records the slug in a
    thread-safe set and asks the client to schedule a debounced batch-ping
    via `schedule_batch_ping_callback`. The client owns the timer so the
    debounce window survives across many `feature()` calls in the same
    request without firing a network call per lookup.
    """

    def __init__(
        self,
        cache: ThreadSafeCache,
        schedule_batch_ping_callback: Callable[[], None],
        ready_callback: Callable[[float], bool] | None = None,
        *,
        api_url: str = "",
        publishable_key: str = "",
        request_timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        feature_environment: str | None = None,
    ) -> None:
        self._cache = cache
        self._schedule_batch_ping = schedule_batch_ping_callback
        self._ready = ready_callback
        self._discovered = _DiscoveredNames()
        # Per-user resolve (for_user) — stateless, multi-user-safe. Reuses the
        # same /resolve endpoint and security context as the global poll path.
        self._api_url = api_url.rstrip("/")
        self._publishable_key = publishable_key
        self._request_timeout = request_timeout
        self._transport = transport
        # Explicit env (test|live) forwarded on /resolve so for_user reads the
        # same snapshot the client does. None → omitted → backend uses prefix.
        self._feature_environment = feature_environment
        self._user_cache = _UserSnapshotCache()
        # One-time-per-slug guard for the plan-gated misuse warning below.
        self._warned_gated: set[str] = set()
        self._warned_lock = threading.Lock()

    def feature(self, name: str) -> Feature:
        """Return the Feature for `name`. Cache miss → hidden (fail-closed).

        Side effect: if this is the first time we've seen `name` in this
        process, schedule a debounced batch-ping so the dashboard registry
        picks the slug up without an explicit `declare()`. Mirrors Swift's
        `OneloFeatures.feature(_:)`.
        """
        if self._discovered.add(name):
            self._schedule_batch_ping()
        state = self._cache.get_state(name)
        status = state.get("status", "hidden")
        if status in _GATED_STATUSES:
            self._maybe_warn_gated(name, status)
        return Feature.from_wire(name, state)

    def _maybe_warn_gated(self, name: str, status: str) -> None:
        """Warn ONCE per slug when the process-global feature() resolves a
        plan-gated status (upsell/greyed). On a multi-user backend that means
        the gate was evaluated against the shared/anonymous identity rather
        than the request's user — so EVERY user gets the no-plan answer the
        instant a paywall is switched on, silently. The fix is for_user().

        Scoped to the global path only: for_user()/UserFeatures carry an
        explicit user_id and never come through here. Mirrors the one-time
        bundle-id mismatch warning — a DX guardrail, not a behaviour change.
        Suppress with ONELO_SUPPRESS_GATING_WARNING=1 (e.g. a legitimately
        single-user CLI that reads a teaser status on purpose)."""
        if os.getenv("ONELO_SUPPRESS_GATING_WARNING"):
            return
        with self._warned_lock:
            if name in self._warned_gated:
                return
            self._warned_gated.add(name)
        logger.warning(
            "onelo.features: feature(%r) resolved to %r under the process-global "
            "identity. On a multi-user backend resolve per request instead — "
            "`await onelo.features.for_user(user_id).feature(%r)` — so plan gating "
            "is evaluated for the actual user, not a shared identity. "
            "(Set ONELO_SUPPRESS_GATING_WARNING=1 to silence if this is intentional.)",
            name, status, name,
        )

    def declare(self, names: list[str]) -> None:
        """Register feature names with the backend so they appear in the
        dashboard registry without waiting for code paths to execute.

        Same debounce contract as `feature()` — multiple `declare()` calls
        in quick succession coalesce into a single network round-trip.
        """
        if self._discovered.add_many(list(names)):
            self._schedule_batch_ping()

    def snapshot_discovered(self) -> list[str]:
        """Current set of slug names this process has ever asked about.
        Used by the discovery_requested SSE handler and by the debounced
        batch-ping fire to know what to send."""
        return self._discovered.snapshot()

    def ready(self, timeout: float = 1.5) -> bool:
        """Block until the first SSE event (or polling response) lands.

        Parity with Swift's `onelo.features.ready()` — same name, same place,
        same semantics. `Onelo.ready()` on the client is an alias that calls
        through here, so existing Python code keeps working.
        """
        if self._ready is None:
            return False
        return self._ready(timeout)

    async def for_user(self, user_id: str, *, ttl: float = 30.0) -> UserFeatures:
        """Resolve features for a SPECIFIC user_id — the multi-user-safe path.

        Unlike identify() (process-global, racy in multi-user backends), this
        does a STATELESS POST /api/sdk/features/resolve for THIS user and never
        touches the SDK's global identity. The result is cached per-user for
        `ttl` seconds, so a busy backend (HTTP/WS handlers) doesn't re-hit the
        network on every request. Use it inside request/connection handlers:

            uf = await onelo.features.for_user(ws_user_id)
            if uf.feature("face-stream").is_enabled:
                ...

        Fail-closed: on a network/HTTP error every feature resolves to hidden
        (empty snapshot) — gating stays safe and the call never raises for an
        infra blip. Raises ValueError only for an empty user_id (a programmer
        error, never a runtime condition)."""
        if not user_id:
            raise ValueError("for_user requires a non-empty user_id")
        snapshot = self._user_cache.get(user_id)
        if snapshot is None:
            snapshot = await self._resolve_user(user_id)
            self._user_cache.put(user_id, snapshot, ttl)
        return UserFeatures(user_id=user_id, _states=snapshot)

    async def _resolve_user(self, user_id: str) -> dict[str, dict]:
        """One stateless /resolve call → {feature_name: state_dict}. Keeps the
        FULL wire state (status + upsell metadata) so UserFeatures can surface
        required_plan_label / upgrade_cta for server-rendered upsells. Mirrors
        the poll consumer's request (key + userId, standard headers) so it
        passes the same SDK security check. Returns {} (→ every feature hidden)
        on any failure — never raises for infra errors."""
        if not self._api_url or not self._publishable_key:
            logger.warning("for_user: SDK missing api_url/publishable_key — resolving empty")
            return {}
        client = create_async_client(timeout=self._request_timeout, transport=self._transport)
        body: dict = {
            "publishableKey": self._publishable_key,
            "userId": user_id,
            "sdk_platform": "python",
            "sdk_version": __version__,
        }
        if self._feature_environment is not None:
            body["environment"] = self._feature_environment
        try:
            resp = await client.post(
                f"{self._api_url}/api/sdk/features/resolve",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            feats = data.get("features") or {}
            out: dict[str, dict] = {}
            for name, state in feats.items():
                out[name] = dict(state) if isinstance(state, dict) else {"status": str(state)}
            return out
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # AttributeError guards a non-dict `features` (e.g. a stray list) so
            # for_user() keeps its "never raises for infra errors" contract.
            logger.warning("for_user resolve failed (fail-closed → hidden): %s", exc)
            return {}
        finally:
            await client.aclose()

    def invalidate_user(self, user_id: str | None = None) -> None:
        """Drop the cached per-user snapshot so the NEXT for_user(user_id)
        re-fetches fresh state from the backend.

        Call this the moment YOUR backend learns a user's plan changed — e.g.
        inside your own Stripe webhook handler, or right after you grant/revoke
        access — for immediate consistency instead of waiting out the for_user
        TTL. (Connected SDKs already get pushed a `features_updated` on plan
        changes; this is the server-side counterpart for the for_user cache.)

        Pass a user_id to invalidate that user, or None to clear EVERY cached
        user (e.g. after a dashboard gating deploy that affects everyone).
        Sync + cheap — no network call."""
        if user_id is None:
            self._user_cache.clear()
        else:
            self._user_cache.invalidate(user_id)
