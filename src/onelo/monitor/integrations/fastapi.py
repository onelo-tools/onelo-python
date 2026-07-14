"""FastAPI integration.

FastAPI is built on Starlette so the ASGI middleware works directly. This
file exists as a discoverable import path and provides ``install_fastapi``
for one-line setup that also wires the active asyncio loop's exception
handler — useful since FastAPI typically runs under uvicorn which spawns
its own loop after ``monitor.init`` has already been called.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from onelo.monitor.integrations.asgi import OneloMonitorASGIMiddleware

if TYPE_CHECKING:
    from fastapi import FastAPI


def install_fastapi(app: "FastAPI", **kwargs: Any) -> None:
    """Add the Onelo monitor ASGI middleware to a FastAPI app + register
    an asyncio loop hook for unhandled task exceptions.

    Equivalent to:

        app.add_middleware(OneloMonitorASGIMiddleware)
        # plus loop.set_exception_handler on the running loop
    """
    app.add_middleware(OneloMonitorASGIMiddleware, **kwargs)

    # Register loop exception handler when the app starts up. FastAPI's
    # startup event runs inside the worker's loop, so this is the canonical
    # place to grab it.
    @app.on_event("startup")
    async def _onelo_register_loop_handler() -> None:  # pragma: no cover - exercised in integration tests
        from onelo.monitor._excepthook import install_for_loop

        try:
            loop = asyncio.get_running_loop()
            install_for_loop(loop)
        except RuntimeError:
            pass


__all__ = ["OneloMonitorASGIMiddleware", "install_fastapi"]
