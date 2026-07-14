"""Shared helpers for framework integrations.

Each web integration (ASGI, WSGI, Django, Flask, etc.) needs to:

  1. Build a "request context" dict from whatever request shape its
     framework gives it (URL, method, scrubbed headers, scrubbed query)
  2. Attach that dict to the active isolation scope so the next captured
     event includes it
  3. On exception, capture before the framework's error handler turns the
     exception into a 500 (so we still see the stack trace)
  4. On response, set a tag with the status code so dashboards can filter

The framework adapters live in ``onelo/monitor/integrations/`` — they call
into here so the actual scrubbing / serialisation logic exists in exactly
one place.
"""
from __future__ import annotations

from typing import Any

from onelo.monitor._capture import capture_exception
from onelo.monitor._scope import get_isolation_scope
from onelo.monitor._scrub import scrub_headers, scrub_url


# We never include these headers in the request context, even after scrubbing,
# because they bloat the payload without diagnostic value (cookies are
# already scrubbed, but they're also huge).
_DROP_HEADERS: frozenset[str] = frozenset({
    "cookie",
    "set-cookie",
    "authorization",  # already redacted by scrubber, but no point sending [REDACTED]
})


def build_request_context(
    *,
    method: str,
    url: str | None,
    headers: dict[str, str] | None,
) -> dict[str, Any]:
    """Build the ``contexts.request`` dict for an event.

    Mirrors the OTel ``http.*`` semantic conventions where possible so
    backends that already understand OTel can render Onelo events without
    custom logic.
    """
    ctx: dict[str, Any] = {"method": method.upper()}
    scrubbed_url = scrub_url(url)
    if scrubbed_url:
        ctx["url"] = scrubbed_url
    scrubbed = scrub_headers(headers)
    if scrubbed:
        ctx["headers"] = {
            k: v for k, v in scrubbed.items() if k.lower() not in _DROP_HEADERS
        }
    return ctx


def attach_request_to_scope(
    *,
    method: str,
    url: str | None,
    headers: dict[str, str] | None,
    user_id: str | None = None,
) -> None:
    """Populate the active isolation scope with request context + user.

    Call early in the middleware (before user code runs) so any captured
    event during the request automatically carries this data.
    """
    scope = get_isolation_scope()
    scope.set_context("request", build_request_context(
        method=method, url=url, headers=headers,
    ))
    if user_id is not None:
        scope.set_user({"id": user_id})


def attach_response_to_scope(*, status_code: int) -> None:
    """Tag the active isolation scope with the response status code.

    Called after the framework returns a response (success or error). For
    5xx we also flip ``ok=False`` on any subsequent capture by setting the
    scope level — though for the common case (exception → 500) the capture
    already happened before this is called.
    """
    scope = get_isolation_scope()
    scope.set_tag("http.status_code", str(status_code))
    if status_code >= 500:
        scope.set_level("error")
    elif status_code >= 400:
        scope.set_level("warning")


def capture_and_reraise(exc: BaseException, *, feature_name: str) -> None:
    """Capture an exception thrown out of a framework call, then re-raise.

    The middleware MUST re-raise so the framework's own error handler still
    runs (renders the 500 page, sends Sentry event, etc.). We just want to
    see the event before it gets translated to a response.
    """
    try:
        capture_exception(exc, feature_name=feature_name)
    except Exception:  # noqa: BLE001 — capture must never crash user code
        pass
    raise exc


def normalise_headers(raw: Any) -> dict[str, str]:
    """Convert various header containers (httpx-style list, dict, ASGI bytes
    pairs) into a plain ``{str: str}`` dict.

    ASGI scopes give us ``[(b"name", b"value"), ...]``. WSGI environ has
    headers as ``HTTP_NAME`` keys. Frameworks usually give us a dict-like.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            k, v = item
            if isinstance(k, bytes):
                k = k.decode("latin-1", errors="replace")
            if isinstance(v, bytes):
                v = v.decode("latin-1", errors="replace")
            out[str(k)] = str(v)
        return out
    return {}


def env_to_headers(environ: dict[str, Any]) -> dict[str, str]:
    """Extract HTTP headers from a WSGI environ dict.

    WSGI puts headers in ``HTTP_X_NAME`` (uppercase, hyphens to underscores).
    Two special cases live outside the prefix: ``CONTENT_TYPE`` and
    ``CONTENT_LENGTH``.
    """
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").lower()
            headers[name] = str(value)
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            headers[key.replace("_", "-").lower()] = str(value)
    return headers


def asgi_url(scope: dict[str, Any]) -> str | None:
    """Reconstruct a full URL from an ASGI scope.

    Includes scheme, host, port (if non-default), path, and query string.
    """
    if scope.get("type") != "http":
        return None
    scheme = scope.get("scheme", "http")
    server = scope.get("server")
    host: str = ""
    port: int | None = None
    if isinstance(server, (tuple, list)) and len(server) == 2:
        host, port = server[0] or "", server[1]
    headers = normalise_headers(scope.get("headers"))
    host_header = headers.get("host")
    if host_header:
        host = host_header
    elif port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    path = scope.get("path", "")
    raw_query: bytes | str = scope.get("query_string", b"")
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("latin-1", errors="replace")
    query = f"?{raw_query}" if raw_query else ""
    return f"{scheme}://{host}{path}{query}"


def wsgi_url(environ: dict[str, Any]) -> str | None:
    """Reconstruct a full URL from a WSGI environ dict (PEP 3333 §URL Reconstruction)."""
    scheme = str(environ.get("wsgi.url_scheme", "http"))
    host = str(environ.get("HTTP_HOST") or environ.get("SERVER_NAME") or "")
    if not environ.get("HTTP_HOST"):
        port = environ.get("SERVER_PORT")
        if port and not (
            (scheme == "http" and str(port) == "80")
            or (scheme == "https" and str(port) == "443")
        ):
            host = f"{host}:{port}"
    script = environ.get("SCRIPT_NAME", "")
    path = environ.get("PATH_INFO", "")
    query = environ.get("QUERY_STRING", "")
    full = f"{scheme}://{host}{script}{path}"
    if query:
        full = f"{full}?{query}"
    return full or None


__all__ = [
    "asgi_url",
    "attach_request_to_scope",
    "attach_response_to_scope",
    "build_request_context",
    "capture_and_reraise",
    "env_to_headers",
    "normalise_headers",
    "wsgi_url",
]
