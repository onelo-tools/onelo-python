"""``requests`` outgoing HTTP auto-instrumentation.

The ``requests`` library doesn't expose a clean transport hook (httpx-style),
so we wrap it via a custom HTTPAdapter:

    import requests
    from onelo.monitor.integrations.requests import OneloRequestsAdapter

    session = requests.Session()
    adapter = OneloRequestsAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    # Or use the convenience installer that mounts on both schemes:
    install_session(session)

We do NOT monkey-patch ``requests.get`` / ``requests.post`` globally —
top-level helpers create a new Session each call and would bypass our
adapter. Devs who use those helpers get HTTP breadcrumbs only via session
mounting, which is the idiomatic pattern anyway.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from onelo.monitor._capture import add_breadcrumb
from onelo.monitor._types import Breadcrumb


if TYPE_CHECKING:
    import requests
    from requests.adapters import HTTPAdapter as _HTTPAdapter
    from requests.models import PreparedRequest, Response

# Import lazily so the module can be loaded even without `requests` installed —
# users only get a clear error when they instantiate the adapter.
try:
    from requests.adapters import HTTPAdapter as _HTTPAdapterRuntime
except ImportError:  # pragma: no cover
    _HTTPAdapterRuntime = None  # type: ignore[assignment]


class OneloRequestsAdapter(_HTTPAdapterRuntime if _HTTPAdapterRuntime else object):  # type: ignore[misc,valid-type]
    """Drop-in replacement for ``requests.adapters.HTTPAdapter`` that emits
    a breadcrumb for every successful or failed request.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if _HTTPAdapterRuntime is None:
            raise ImportError(
                "OneloRequestsAdapter requires the `requests` package. "
                "Install with `pip install requests`."
            )
        super().__init__(*args, **kwargs)

    def send(  # type: ignore[override]
        self,
        request: "PreparedRequest",
        **kwargs: Any,
    ) -> "Response":
        start = time.monotonic()
        status: int | None = None
        try:
            response = super().send(request, **kwargs)
            status = response.status_code
            return response
        except Exception:
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            _record_breadcrumb(request, status, duration_ms)


def install_session(session: "requests.Session") -> "requests.Session":
    """Mount Onelo adapter on both http:// and https:// of an existing Session."""
    adapter = OneloRequestsAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ─── internals ─────────────────────────────────────────────────────────────


def _record_breadcrumb(
    request: "PreparedRequest",
    status: int | None,
    duration_ms: int,
) -> None:
    try:
        crumb = Breadcrumb.http(
            method=str(request.method or "GET"),
            url=str(request.url or ""),
            status=status,
            duration_ms=duration_ms,
        )
        add_breadcrumb(crumb)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["OneloRequestsAdapter", "install_session"]
