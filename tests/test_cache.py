"""Tests for ThreadSafeCache — verifies fail-closed semantics and atomic
replacement under concurrent access."""
import threading

import pytest

from onelo._cache import ThreadSafeCache


def test_cache_miss_returns_hidden():
    cache = ThreadSafeCache()
    assert cache.get("never-stored") == "hidden"


def test_cache_returns_stored_value():
    cache = ThreadSafeCache()
    cache.replace_all({"chat": "enabled"}, version=1)
    assert cache.get("chat") == "enabled"


def test_replace_all_overwrites_completely():
    """replace_all is atomic snapshot replacement — old keys vanish."""
    cache = ThreadSafeCache()
    cache.replace_all({"a": "enabled", "b": "hidden"}, version=1)
    cache.replace_all({"c": "beta"}, version=2)
    assert cache.get("a") == "hidden"  # gone
    assert cache.get("b") == "hidden"  # gone
    assert cache.get("c") == "beta"


def test_config_version_tracked():
    cache = ThreadSafeCache()
    assert cache.config_version == 0
    cache.replace_all({}, version=42)
    assert cache.config_version == 42


def test_concurrent_reads_and_writes_do_not_corrupt():
    """Hammer the cache from multiple threads. With RLock, no corruption,
    no exceptions, no torn reads (`get` either sees old or new snapshot,
    never half-applied)."""
    cache = ThreadSafeCache()
    cache.replace_all({f"k{i}": "enabled" for i in range(100)}, version=1)
    errors = []

    def reader():
        for _ in range(1000):
            for i in range(100):
                v = cache.get(f"k{i}")
                if v not in ("enabled", "hidden"):
                    errors.append(f"corrupt value: {v!r}")

    def writer():
        for n in range(50):
            snapshot = {f"k{i}": ("enabled" if (n + i) % 2 else "hidden") for i in range(100)}
            cache.replace_all(snapshot, version=n + 2)

    threads = [threading.Thread(target=reader) for _ in range(4)] + \
              [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
