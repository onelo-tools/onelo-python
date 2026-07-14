"""Per-request breadcrumb buffer.

Backed by ``collections.deque(maxlen=N)`` — gives us a ring buffer for free
with O(1) append and automatic eviction of the oldest entry when full. Sentry
SDK uses the same primitive. Default capacity 100; configurable per-init.

Thread safety: the buffer is meant to live on a Scope which itself sits on a
ContextVar (see ``_scope.py``). ContextVars are per-task / per-thread, so the
buffer never needs an explicit lock — there is exactly one writer per scope.
The only cross-thread risk is if a developer manually shares a Scope across
threads, which we document as undefined behaviour.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from typing import Any

from onelo.monitor._types import Breadcrumb


DEFAULT_CAPACITY = 100


class BreadcrumbBuffer:
    """Bounded FIFO of recent breadcrumbs attached to the next captured event."""

    __slots__ = ("_buffer",)

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._buffer: deque[Breadcrumb] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        # `deque.maxlen` is `int | None`; we set it explicitly above so it's int.
        return self._buffer.maxlen  # type: ignore[return-value]

    def add(self, crumb: Breadcrumb) -> None:
        self._buffer.append(crumb)

    def extend(self, crumbs: Iterable[Breadcrumb]) -> None:
        self._buffer.extend(crumbs)

    def snapshot(self) -> list[Breadcrumb]:
        """Copy the buffer in chronological order (oldest first).

        Returning a list (not the deque itself) keeps the buffer mutable
        without surprising callers if they iterate after a later ``add``.
        """
        return list(self._buffer)

    def clear(self) -> None:
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)

    def __iter__(self) -> Iterator[Breadcrumb]:
        return iter(self._buffer)

    def to_event_data(self) -> list[dict[str, Any]]:
        """Serialise to the wire shape expected by ``MonitorEvent.meta['breadcrumbs']``."""
        return [
            {
                "category": c.category,
                "message": c.message,
                "ts": c.timestamp,
                "level": c.level,
                **({"data": c.data} if c.data else {}),
            }
            for c in self._buffer
        ]


__all__ = ["BreadcrumbBuffer", "DEFAULT_CAPACITY"]
