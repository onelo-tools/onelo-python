"""Tests for the per-scope breadcrumb ring buffer."""
import pytest

from onelo.monitor._breadcrumbs import BreadcrumbBuffer, DEFAULT_CAPACITY
from onelo.monitor._types import Breadcrumb


def test_default_capacity_is_100() -> None:
    buf = BreadcrumbBuffer()
    assert buf.capacity == DEFAULT_CAPACITY == 100


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BreadcrumbBuffer(capacity=0)


def test_add_and_snapshot_chronological() -> None:
    buf = BreadcrumbBuffer(capacity=5)
    for i in range(3):
        buf.add(Breadcrumb.info(f"msg {i}"))
    snap = buf.snapshot()
    assert [c.message for c in snap] == ["msg 0", "msg 1", "msg 2"]


def test_ring_eviction_keeps_most_recent() -> None:
    buf = BreadcrumbBuffer(capacity=3)
    for i in range(10):
        buf.add(Breadcrumb.info(f"msg {i}"))
    snap = buf.snapshot()
    assert len(snap) == 3
    assert [c.message for c in snap] == ["msg 7", "msg 8", "msg 9"]


def test_clear_empties_buffer() -> None:
    buf = BreadcrumbBuffer(capacity=5)
    buf.add(Breadcrumb.info("a"))
    buf.add(Breadcrumb.info("b"))
    buf.clear()
    assert buf.snapshot() == []
    assert len(buf) == 0


def test_to_event_data_emits_wire_shape() -> None:
    buf = BreadcrumbBuffer()
    buf.add(Breadcrumb.http("POST", "https://x.com/y", status=201, duration_ms=42))
    buf.add(Breadcrumb.info("loaded"))
    out = buf.to_event_data()
    assert out[0]["category"] == "http"
    assert out[0]["data"]["status"] == 201
    assert out[0]["data"]["method"] == "POST"
    assert out[1]["category"] == "info"
    # Info breadcrumbs without data should not include an empty data key.
    assert "data" not in out[1]


def test_extend_appends_in_order() -> None:
    buf = BreadcrumbBuffer(capacity=5)
    buf.extend([Breadcrumb.info("a"), Breadcrumb.info("b")])
    assert [c.message for c in buf] == ["a", "b"]


def test_breadcrumb_factory_helpers() -> None:
    nav = Breadcrumb(category="navigation", message="HomeScreen", timestamp=0)
    assert nav.category == "navigation"

    http = Breadcrumb.http("GET", "https://api", status=200, duration_ms=12)
    assert http.data is not None
    assert http.data["url"] == "https://api"

    feature = Breadcrumb.feature("checkout-v2", "enabled")
    assert feature.data == {"value": "enabled"}
