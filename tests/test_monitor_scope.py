"""Tests for the global / isolation / current scope model.

Critical correctness properties (a regression here = cross-request leak,
i.e. user A's breadcrumbs in user B's crash report):

  - isolation_scope() must not leak state to / from siblings
  - new_scope() must layer above isolation, not replace it
  - Concurrent asyncio tasks must each see their own isolation scope
"""
from __future__ import annotations

import asyncio

import pytest

from onelo.monitor._scope import (
    apply_scopes_to_event,
    get_current_scope,
    get_global_scope,
    get_isolation_scope,
    isolation_scope,
    new_scope,
    reset_for_tests,
)
from onelo.monitor._types import Breadcrumb, MonitorEvent


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_for_tests()
    yield
    reset_for_tests()


def _new_event(name: str = "test") -> MonitorEvent:
    return MonitorEvent(feature_name=name, ok=False)


def test_global_scope_is_singleton() -> None:
    assert get_global_scope() is get_global_scope()


def test_isolation_scope_lazy_inits_from_global() -> None:
    g = get_global_scope()
    g.set_tag("env", "prod")
    iso = get_isolation_scope()
    assert iso is not g
    assert iso.tags == {"env": "prod"}


def test_isolation_scope_block_resets_on_exit() -> None:
    parent = get_isolation_scope()
    parent.set_user({"id": "outer"})

    with isolation_scope() as forked:
        forked.set_user({"id": "inner"})
        assert get_isolation_scope().user == {"id": "inner"}

    # Must restore parent — outer code never sees "inner".
    assert get_isolation_scope().user == {"id": "outer"}


def test_isolation_scope_does_not_leak_breadcrumbs() -> None:
    """Critical property: forked scope starts with empty breadcrumbs even
    though it inherits user / tags from parent."""
    parent = get_isolation_scope()
    parent.add_breadcrumb(Breadcrumb.info("from parent"))

    with isolation_scope() as forked:
        # Forked scope should NOT see parent's breadcrumbs.
        assert forked.breadcrumbs.snapshot() == []
        forked.add_breadcrumb(Breadcrumb.info("from child"))

    # Parent's breadcrumb survived (we didn't touch it).
    assert len(parent.breadcrumbs) == 1
    assert parent.breadcrumbs.snapshot()[0].message == "from parent"


def test_new_scope_layers_over_isolation() -> None:
    iso = get_isolation_scope()
    iso.set_tag("layer", "iso")

    with new_scope() as cur:
        cur.set_tag("layer", "cur")
        assert get_current_scope().tags["layer"] == "cur"

    # Outside the block, current falls back to isolation.
    assert get_current_scope() is iso
    assert iso.tags["layer"] == "iso"


def test_apply_scopes_merges_in_correct_order() -> None:
    g = get_global_scope()
    g.set_tag("source", "global")
    g.set_extra("global_key", 1)

    with isolation_scope() as iso:
        iso.set_tag("source", "isolation")  # overrides global
        iso.set_user({"id": "u123"})
        iso.add_breadcrumb(Breadcrumb.info("iso crumb"))

        with new_scope() as cur:
            cur.set_tag("source", "current")  # overrides isolation
            cur.add_breadcrumb(Breadcrumb.info("cur crumb"))

            event = _new_event()
            apply_scopes_to_event(event)

    assert event.user_id == "u123"
    assert event.meta["tags"]["source"] == "current"
    assert event.meta["extra"]["global_key"] == 1
    # Breadcrumbs concatenate from both layers.
    crumbs = event.meta["breadcrumbs"]
    messages = [c["message"] for c in crumbs]
    assert "iso crumb" in messages
    assert "cur crumb" in messages


def test_apply_scopes_does_not_overwrite_explicit_user() -> None:
    """If integration code sets event.user_id explicitly, scope shouldn't
    overwrite it. Useful when an offline/background event already carries
    the right user."""
    iso = get_isolation_scope()
    iso.set_user({"id": "scope-user"})

    event = MonitorEvent(feature_name="x", ok=False, user_id="explicit-user")
    apply_scopes_to_event(event)
    assert event.user_id == "explicit-user"


@pytest.mark.asyncio
async def test_concurrent_asyncio_tasks_have_independent_isolation_scopes() -> None:
    """The whole point of ContextVar — one isolation scope per task."""
    results: dict[str, str | None] = {}

    async def task(name: str, user: str) -> None:
        with isolation_scope() as scope:
            scope.set_user({"id": user})
            await asyncio.sleep(0.01)  # yield to scheduler
            assert get_isolation_scope().user is not None
            assert get_isolation_scope().user["id"] == user
            results[name] = get_isolation_scope().user["id"]

    await asyncio.gather(
        task("a", "user-1"),
        task("b", "user-2"),
        task("c", "user-3"),
    )
    assert results == {"a": "user-1", "b": "user-2", "c": "user-3"}


@pytest.mark.asyncio
async def test_isolation_scope_cleans_up_after_exception() -> None:
    """Even if the with block raises, the parent scope must be restored."""
    parent_user = {"id": "parent"}
    iso = get_isolation_scope()
    iso.set_user(parent_user)

    with pytest.raises(RuntimeError):
        with isolation_scope() as scope:
            scope.set_user({"id": "child"})
            raise RuntimeError("boom")

    assert get_isolation_scope().user == parent_user


def test_clone_does_not_carry_breadcrumbs() -> None:
    parent = get_global_scope()
    parent.add_breadcrumb(Breadcrumb.info("parent crumb"))
    parent.set_user({"id": "u"})
    parent.set_tag("k", "v")

    clone = parent.clone()
    assert clone.user == {"id": "u"}
    assert clone.tags == {"k": "v"}
    assert clone.breadcrumbs.snapshot() == []
