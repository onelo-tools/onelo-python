"""Pluggable cache for verified user tokens.

The default ``InProcessAuthCache`` is a single-process, asyncio-safe
LRU-with-TTL dict. v0.3+ may add a Redis-backed cache without changing
the public ``RequireUser`` API — both implement the ``AuthCache``
protocol below.

Tokens are never cached in plaintext. Callers must hash the token (e.g.
sha256) before passing it as the cache key — see
``RequireUser.__call__`` in ``onelo.fastapi``.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from typing import Protocol, runtime_checkable

from onelo.auth import OneloUser


@runtime_checkable
class AuthCache(Protocol):
    """Pluggable cache contract for verified Onelo users."""

    async def get(self, token_hash: str) -> OneloUser | None: ...

    async def set(self, token_hash: str, user: OneloUser, ttl: float) -> None: ...

    async def invalidate(self, token_hash: str) -> None: ...


class InProcessAuthCache:
    """Default in-process LRU-with-TTL cache.

    * Bounded by ``max_size`` — oldest entry is evicted when full.
    * Each entry has its own absolute expiry (``now + ttl`` at write time).
    * All mutations are protected by an ``asyncio.Lock`` — safe for
      concurrent FastAPI request handlers.
    * Reads use ``time.monotonic()`` for TTL checks (immune to wall-clock
      jumps), and on hit the entry is moved to "most recently used".
    """

    def __init__(self, max_size: int = 10_000) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        # token_hash -> (user, expiry_monotonic)
        self._store: OrderedDict[str, tuple[OneloUser, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, token_hash: str) -> OneloUser | None:
        async with self._lock:
            entry = self._store.get(token_hash)
            if entry is None:
                return None
            user, expiry = entry
            if time.monotonic() >= expiry:
                # Expired — evict and miss.
                self._store.pop(token_hash, None)
                return None
            # Move to MRU end.
            self._store.move_to_end(token_hash)
            return user

    async def set(self, token_hash: str, user: OneloUser, ttl: float) -> None:
        if ttl <= 0:
            return  # do not cache
        expiry = time.monotonic() + ttl
        async with self._lock:
            # Replace + move-to-end semantics.
            if token_hash in self._store:
                self._store.move_to_end(token_hash)
            self._store[token_hash] = (user, expiry)
            # Evict oldest entries until under the size cap.
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    async def invalidate(self, token_hash: str) -> None:
        async with self._lock:
            self._store.pop(token_hash, None)


@runtime_checkable
class SyncAuthCache(Protocol):
    """Pluggable cache contract for verified Onelo users (sync flavour).

    Mirrors ``AuthCache`` but with synchronous methods, for use by Flask,
    Django, and WSGI adapters.
    """

    def get(self, token_hash: str) -> OneloUser | None: ...

    def set(self, token_hash: str, user: OneloUser, ttl: float) -> None: ...

    def invalidate(self, token_hash: str) -> None: ...


class InProcessSyncAuthCache:
    """Sync version of ``InProcessAuthCache``.

    Same LRU + TTL semantics, but uses ``threading.Lock`` instead of
    ``asyncio.Lock`` — required because Flask / Django run multi-threaded
    under gunicorn / uWSGI workers.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max_size = max_size
        self._store: OrderedDict[str, tuple[OneloUser, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, token_hash: str) -> OneloUser | None:
        with self._lock:
            entry = self._store.get(token_hash)
            if entry is None:
                return None
            user, expiry = entry
            if time.monotonic() >= expiry:
                self._store.pop(token_hash, None)
                return None
            self._store.move_to_end(token_hash)
            return user

    def set(self, token_hash: str, user: OneloUser, ttl: float) -> None:
        if ttl <= 0:
            return
        expiry = time.monotonic() + ttl
        with self._lock:
            if token_hash in self._store:
                self._store.move_to_end(token_hash)
            self._store[token_hash] = (user, expiry)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def invalidate(self, token_hash: str) -> None:
        with self._lock:
            self._store.pop(token_hash, None)


__all__ = [
    "AuthCache",
    "InProcessAuthCache",
    "SyncAuthCache",
    "InProcessSyncAuthCache",
]
