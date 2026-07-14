"""Shared pytest fixtures.

`mock_backend_transport` returns an httpx MockTransport that emulates the
Onelo SDK API endpoints used by the SDK: SSE stream, polling, batch-ping.
"""
import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest


@dataclass
class MockBackendState:
    """Mutable state shared between test code and the mock transport."""
    config_version: int = 1
    features: dict[str, str] = field(default_factory=dict)  # name → status
    declared: list[str] = field(default_factory=list)
    sse_event_queue: asyncio.Queue | None = None  # set in fixture


@pytest.fixture
def mock_backend() -> MockBackendState:
    # Note: brief allowed dropping `event_loop` param if deprecated in pytest-asyncio.
    # asyncio.Queue() does not require a running loop in modern Python.
    state = MockBackendState()
    state.sse_event_queue = asyncio.Queue()
    return state


@pytest.fixture
def mock_backend_transport(mock_backend) -> httpx.MockTransport:
    state = mock_backend

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/api/sdk/features/batch-ping":
            payload = json.loads(request.content)
            for name in payload["features"]:
                state.declared.append(name)
                state.features.setdefault(name, "hidden")
            return httpx.Response(204)

        if path == "/api/sdk/features/poll":
            since = int(request.url.params.get("since_version", "0"))
            if since >= state.config_version:
                return httpx.Response(304)
            return httpx.Response(200, json={
                "config_version": state.config_version,
                "features": {n: {"status": s} for n, s in state.features.items()},
            })

        if path == "/api/sdk/features/stream":
            # Build a single response containing whatever events are queued
            events: list[tuple[str, str]] = []
            # Initial connected event with current state
            events.append((
                "connected",
                json.dumps({
                    "config_version": state.config_version,
                    "features": {n: {"status": s} for n, s in state.features.items()},
                }),
            ))
            # Drain any queued updates
            while not state.sse_event_queue.empty():
                events.append(state.sse_event_queue.get_nowait())
            body = "".join(f"event: {e}\ndata: {d}\n\n" for e, d in events)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=body,
            )

        return httpx.Response(404, json={"error": f"unmocked path {path}"})

    return httpx.MockTransport(handler)
