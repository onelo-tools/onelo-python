"""Thread-safe dict-backed cache for feature state.

Stores the FULL per-feature wire state dict — ``{"status", "reason",
"required_plan", "required_plan_label", "upgrade_cta"}`` — so callers can
surface upsell metadata (e.g. server-rendered "Available in Pro" CTAs), not
just the binary status. Reads and writes are guarded by an RLock (reentrant)
so the same thread can re-enter without deadlock. Missing feature → a
``{"status": "hidden"}`` state — fail-closed default per the SDK protocol spec.
"""
import threading


class ThreadSafeCache:
    """Wire-state cache for a single Onelo client instance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._features: dict[str, dict] = {}
        self._config_version: int = 0
        # #739: identity generation. Bumped by begin_identity() whenever the
        # process-global identity changes; a stream/poll result is applied
        # (and ready signalled) only for the generation current when its
        # request started, so a late answer for the previous identity can
        # never land on — or mark ready — the new one.
        self._generation: int = 0

    @staticmethod
    def _normalize(value: object) -> dict:
        """Canonicalise a per-feature value to a state dict. Accepts either a
        full wire dict (kept, defensively copied) or a bare status string
        (legacy/degenerate → ``{"status": value}``). Keeps every existing
        ``replace_all({name: "enabled"})`` caller working while the wire now
        carries richer dicts."""
        if isinstance(value, dict):
            return dict(value)
        return {"status": str(value)}

    def get_state(self, name: str) -> dict:
        """Return the full wire state dict for `name`, or ``{"status": "hidden"}``
        on miss (fail-closed). Defensive copy — callers can mutate freely."""
        with self._lock:
            state = self._features.get(name)
            return dict(state) if state is not None else {"status": "hidden"}

    def get(self, name: str) -> str:
        """Return wire status string for `name`, or "hidden" on miss
        (fail-closed). Kept for the monitor flag snapshot and status-only
        callers; use get_state() for the full metadata."""
        with self._lock:
            state = self._features.get(name)
            return state.get("status", "hidden") if state is not None else "hidden"

    def replace_all(self, features: dict, version: int) -> None:
        """Atomically replace the entire feature snapshot. Each value may be a
        full wire dict or a bare status string (see _normalize)."""
        with self._lock:
            self._features = {k: self._normalize(v) for k, v in features.items()}
            self._config_version = version

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def request_view(self) -> tuple[int, int]:
        """(generation, since_version) read atomically at request start. The
        version is non-zero only once a snapshot was CONFIRMED for this
        generation (begin_identity resets it to 0)."""
        with self._lock:
            return self._generation, self._config_version

    def begin_identity(self, ready=None) -> None:
        """New identity: drop the previous identity's snapshot (fail-closed),
        forget its config_version so the next request asks for a full
        snapshot, and re-arm `ready` — all under the cache lock."""
        with self._lock:
            self._generation += 1
            self._features = {}
            self._config_version = 0
            if ready is not None:
                ready.clear()

    def apply(self, features: dict, version: int, generation: int, ready=None) -> bool:
        """replace_all() bound to `generation`: a no-op returning False when the
        identity changed since the request started. Sets `ready` on success."""
        with self._lock:
            if generation != self._generation:
                return False
            self.replace_all(features, version)
            if ready is not None:
                ready.set()
            return True

    def confirm(self, generation: int, ready) -> bool:
        """Signal `ready` for an up-to-date answer, only for the current
        generation."""
        with self._lock:
            if generation != self._generation:
                return False
            ready.set()
            return True

    @property
    def config_version(self) -> int:
        with self._lock:
            return self._config_version

    def snapshot(self) -> dict[str, str]:
        """Return a stable copy of the current ``{name: status}`` mapping.

        Used by the monitor module to attach active feature-flag state to
        every captured event (the cross-platform "killer feature" — error
        events ship with the flag values that were live at the moment).
        Status-only by design — the monitor correlates flag values, not upsell
        metadata. Defensive copy so callers can iterate without holding the lock.
        """
        with self._lock:
            return {name: state.get("status", "hidden") for name, state in self._features.items()}
