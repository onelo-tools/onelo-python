"""Tests for Feature — derives boolean checks from a wire status string."""
import pytest

from onelo._cache import ThreadSafeCache
from onelo._features import Feature, FeaturesClient


@pytest.mark.parametrize("status,enabled,visible", [
    ("enabled",     True,  True),
    ("new",         True,  True),
    ("beta",        True,  True),
    ("coming_soon", False, True),
    ("greyed",      False, True),
    ("upsell",      False, True),
    ("hidden",      False, False),
])
def test_is_enabled_and_is_visible(status, enabled, visible):
    feat = Feature(name="x", status=status)
    assert feat.is_enabled is enabled
    assert feat.is_visible is visible


@pytest.mark.parametrize("status", ["", "disabled", "trial", "ENABLED", "typo-here"])
def test_unknown_status_is_fail_closed(status):
    """A status this SDK build doesn't recognise (typo, or one added by a newer
    backend) must be invisible AND not enabled — never a blank visible item."""
    feat = Feature(name="x", status=status)
    assert feat.is_visible is False
    assert feat.is_enabled is False
    assert feat.is_known is False
    # No badge/marker check fires for an unknown status either.
    assert feat.is_greyed is False
    assert feat.is_upsell is False


@pytest.mark.parametrize("status", [
    "enabled", "new", "beta", "coming_soon", "greyed", "upsell", "hidden",
])
def test_known_statuses_report_is_known(status):
    assert Feature(name="x", status=status).is_known is True


def test_specific_status_checks():
    feat = Feature(name="x", status="greyed")
    assert feat.is_greyed is True
    assert feat.is_new is False
    assert feat.is_beta is False
    assert feat.is_coming_soon is False


def test_features_client_reads_from_cache():
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=1)
    client = FeaturesClient(cache=cache, schedule_batch_ping_callback=lambda: None)

    feat = client.feature("chat")
    assert feat.name == "chat"
    assert feat.status == "enabled"
    assert feat.is_enabled is True


def test_features_client_cache_miss_returns_hidden():
    cache = ThreadSafeCache()
    client = FeaturesClient(cache=cache, schedule_batch_ping_callback=lambda: None)

    feat = client.feature("never-declared")
    assert feat.status == "hidden"
    assert feat.is_enabled is False
    assert feat.is_visible is False


def test_declare_schedules_batch_ping():
    """`declare(names)` records the slugs in the discovery set and asks the
    client to schedule a debounced batch-ping. The names themselves live in
    `snapshot_discovered()`; the schedule callback is arg-less because the
    client reads the snapshot when the debounce timer fires."""
    cache = ThreadSafeCache()
    schedule_calls: list[int] = []
    client = FeaturesClient(
        cache=cache,
        schedule_batch_ping_callback=lambda: schedule_calls.append(1),
    )

    client.declare(["a", "b", "c"])
    assert schedule_calls == [1]
    assert sorted(client.snapshot_discovered()) == ["a", "b", "c"]


def test_feature_call_auto_discovers_and_schedules():
    """First `feature(name)` on a new slug schedules a ping; repeat calls
    don't (the slug is already in the discovery set)."""
    cache = ThreadSafeCache()
    schedule_calls: list[int] = []
    client = FeaturesClient(
        cache=cache,
        schedule_batch_ping_callback=lambda: schedule_calls.append(1),
    )

    client.feature("chat")
    client.feature("chat")  # already discovered — no extra schedule
    client.feature("search")
    assert schedule_calls == [1, 1]
    assert sorted(client.snapshot_discovered()) == ["chat", "search"]


# --- Plan-gated misuse warning (global feature() on a multi-user server) ---

def _gated_client(status: str) -> FeaturesClient:
    cache = ThreadSafeCache()
    cache.replace_all({"face-stream": status}, version=1)
    return FeaturesClient(cache=cache, schedule_batch_ping_callback=lambda: None)


@pytest.mark.parametrize("status", ["upsell", "greyed"])
def test_global_feature_warns_once_on_gated_status(status, caplog, monkeypatch):
    """A plan-gated status read through the process-global feature() path
    fires the for_user() guidance warning — exactly once per slug."""
    monkeypatch.delenv("ONELO_SUPPRESS_GATING_WARNING", raising=False)
    client = _gated_client(status)
    with caplog.at_level("WARNING", logger="onelo.features"):
        client.feature("face-stream")
        client.feature("face-stream")  # second read must NOT re-warn
    hits = [r for r in caplog.records if "for_user" in r.getMessage()]
    assert len(hits) == 1
    assert "face-stream" in hits[0].getMessage()
    assert status in hits[0].getMessage()


@pytest.mark.parametrize("status", ["enabled", "new", "beta", "hidden", "coming_soon"])
def test_global_feature_no_warning_on_non_gated_status(status, caplog, monkeypatch):
    """Non-gated statuses (including the ambiguous `hidden`) never warn —
    keeps the guardrail zero-false-positive."""
    monkeypatch.delenv("ONELO_SUPPRESS_GATING_WARNING", raising=False)
    client = _gated_client(status)
    with caplog.at_level("WARNING", logger="onelo.features"):
        client.feature("face-stream")
    assert not [r for r in caplog.records if "for_user" in r.getMessage()]


def test_gating_warning_suppressed_by_env(caplog, monkeypatch):
    """ONELO_SUPPRESS_GATING_WARNING silences the warning (legit single-user
    CLI that reads a teaser status on purpose)."""
    monkeypatch.setenv("ONELO_SUPPRESS_GATING_WARNING", "1")
    client = _gated_client("upsell")
    with caplog.at_level("WARNING", logger="onelo.features"):
        client.feature("face-stream")
    assert not [r for r in caplog.records if "for_user" in r.getMessage()]


def test_for_user_path_never_triggers_global_gating_warning(caplog, monkeypatch):
    """UserFeatures.feature() carries an explicit user_id and must NOT route
    through the global warning, even on a gated status."""
    monkeypatch.delenv("ONELO_SUPPRESS_GATING_WARNING", raising=False)
    from onelo._features import UserFeatures
    uf = UserFeatures(user_id="u1", _states={"face-stream": {"status": "upsell"}})
    with caplog.at_level("WARNING", logger="onelo.features"):
        feat = uf.feature("face-stream")
    assert feat.is_upsell is True
    assert not [r for r in caplog.records if "for_user" in r.getMessage()]


# ── Upsell metadata surfacing (G4) ──────────────────────────────────────────

def test_feature_from_wire_surfaces_upsell_metadata():
    """A plan-gated feature must carry required_plan/label + upgrade_cta so a
    server-rendered app can build an 'Available in Pro' CTA."""
    feat = Feature.from_wire("advanced-export", {
        "status": "upsell",
        "reason": "plan",
        "required_plan": "pro",
        "required_plan_label": "Pro",
        "upgrade_cta": True,
    })
    assert feat.status == "upsell"
    assert feat.is_upsell is True
    assert feat.reason == "plan"
    assert feat.required_plan == "pro"
    assert feat.required_plan_label == "Pro"
    assert feat.upgrade_cta is True
    assert feat.upgrade_hint == "Pro"  # render: f"Available in {feat.upgrade_hint}"


def test_feature_from_wire_defaults_when_metadata_absent():
    """An enabled feature carries no upsell metadata — upgrade_hint is None."""
    feat = Feature.from_wire("chat", {"status": "enabled"})
    assert feat.reason is None
    assert feat.required_plan is None
    assert feat.required_plan_label is None
    assert feat.upgrade_cta is False
    assert feat.upgrade_hint is None


def test_feature_from_wire_tolerates_bare_status_string_and_unknown_keys():
    assert Feature.from_wire("x", "greyed").status == "greyed"
    # Unknown extra keys ignored (forward-compat), status still read.
    feat = Feature.from_wire("x", {"status": "beta", "future_field": 123})
    assert feat.status == "beta"
    assert feat.is_beta is True


def test_features_client_surfaces_metadata_from_cache():
    """The cache now stores the full wire state; feature() must expose it."""
    cache = ThreadSafeCache()
    cache.replace_all({
        "export": {"status": "upsell", "required_plan_label": "Pro", "upgrade_cta": True},
    }, version=1)
    client = FeaturesClient(cache=cache, schedule_batch_ping_callback=lambda: None)
    feat = client.feature("export")
    assert feat.upgrade_hint == "Pro"
    assert feat.upgrade_cta is True


def test_user_features_surfaces_metadata():
    from onelo._features import UserFeatures
    uf = UserFeatures(user_id="u1", _states={
        "export": {"status": "upsell", "required_plan_label": "Pro"},
    })
    assert uf.feature("export").upgrade_hint == "Pro"
    # Missing feature → hidden, no metadata (fail-closed).
    assert uf.feature("missing").status == "hidden"
    assert uf.feature("missing").upgrade_hint is None


def test_cache_normalises_bare_status_string_backcompat():
    """replace_all({name: 'enabled'}) must still work — status-only callers and
    older tests pass bare strings; get()/get_state() must both be sane."""
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=3)
    assert cache.get("chat") == "enabled"
    assert cache.get_state("chat") == {"status": "enabled"}
    # Miss → fail-closed hidden state.
    assert cache.get("nope") == "hidden"
    assert cache.get_state("nope") == {"status": "hidden"}
    # snapshot() stays status-only for the monitor flag provider.
    assert cache.snapshot() == {"chat": "enabled"}


# ── _UserSnapshotCache is bounded (G5) ──────────────────────────────────────

def test_user_snapshot_cache_lazy_prunes_expired_on_get():
    """An expired entry must be DELETED on the get() that finds it — not linger
    until the same user returns (the old leak)."""
    from onelo._features import _UserSnapshotCache
    c = _UserSnapshotCache()
    c.put("u1", {"a": {"status": "enabled"}}, ttl=-1)  # already expired
    assert c.get("u1") is None
    assert len(c._d) == 0  # gone, not lingering


def test_user_snapshot_cache_enforces_max_entries():
    """A long tail of one-shot user_ids must not grow the map past the cap."""
    from onelo._features import _UserSnapshotCache
    c = _UserSnapshotCache(max_entries=3)
    for i in range(6):
        c.put(f"u{i}", {"a": {"status": "enabled"}}, ttl=60)
    assert len(c._d) <= 3


def test_user_snapshot_cache_evicts_expired_before_live_on_overflow():
    """Eviction drops expired entries first, keeping the live ones."""
    from onelo._features import _UserSnapshotCache
    c = _UserSnapshotCache(max_entries=2)
    c.put("live1", {"x": {"status": "enabled"}}, ttl=60)
    c.put("stale", {"x": {"status": "enabled"}}, ttl=-1)   # expired
    c.put("live2", {"x": {"status": "enabled"}}, ttl=60)   # overflow → purge expired
    assert "stale" not in c._d
    assert "live1" in c._d and "live2" in c._d
    assert len(c._d) == 2
