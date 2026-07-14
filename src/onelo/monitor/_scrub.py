"""PII / secret scrubbing — mirrors the backend Python denylist (sdk_monitor.py)
and the Swift MonitorScrubber. Every event passes through this on the way out
so that secrets in URLs, headers, error strings, and meta payloads never leave
the host.

The denylist + regex set are duplicated here intentionally for now. The plan
(see decision D4) is to lift them into ``packages/onelo-shared/scrubbers.json``
once a second SDK lands, so all platforms read one file.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


REDACTED = "[REDACTED]"
"""Single replacement value used everywhere — easy to grep in dashboards."""

_MAX_DEPTH = 10
"""Hard cap on recursion depth in nested meta dicts. Anything deeper is
collapsed to a ``_onelo_depth_exceeded`` marker so secrets buried under a
deliberately-deep payload cannot slip past the scrubber.
"""


# ─── Headers ────────────────────────────────────────────────────────────────
SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-onelo-secret",
    "www-authenticate",
})

# ─── Query params ───────────────────────────────────────────────────────────
SENSITIVE_QUERY_KEYS: frozenset[str] = frozenset({
    "token", "access_token", "refresh_token", "id_token",
    "key", "api_key", "apikey",
    "secret", "client_secret",
    "password", "passwd",
    "auth", "authorization",
    "code",      # OAuth authorization code
    "session",
})

# ─── Substring match against arbitrary keys (case-insensitive) ──────────────
PII_KEY_SUBSTRINGS: tuple[str, ...] = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "cookie", "credential", "private_key",
    "client_secret", "cvv", "ssn",
)

# ─── Regex patterns matched against any string value ────────────────────────
#
# Boundary anchors: Python's ``\b`` uses Unicode word characters, which means
# letters like Cyrillic ``а``, ZWJ-joined glyphs, and full-width ``ｓ`` count
# as word chars too. That lets an attacker pad a secret with non-ASCII to
# escape the boundary (``"prefix𝐬sk_live_…"`` would not match a ``\b``-anchored
# Stripe regex). We replace ``\b`` with explicit ASCII-only lookarounds so
# only ``[A-Za-z0-9_]`` blocks the match — exactly the alphabet of the
# secrets we care about. Non-ASCII characters become "non-word" for the
# purpose of these regexes, and the patterns latch onto the secret cleanly.
# Underscore is intentionally NOT in the lookbehind/lookahead set: it's the
# canonical env-var separator (``ENV_sk_live_…`` / ``API_KEY_sk_…``) and
# leaving it as a "boundary" character means we still latch on when the
# secret is sandwiched between underscores.
_LB = r"(?<![A-Za-z0-9])"
_LA = r"(?![A-Za-z0-9])"

# Email regex (RFC 5322 simplified) — only used when ``strict_email=True``.
# Off by default because devs frequently log emails for support / debugging.
# Opt-in via ``monitor.init(strict_email_scrub=True)``.
_EMAIL_PATTERN = re.compile(_LB + r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}" + _LA)

_strict_email_scrub: bool = False


def set_strict_email_scrub(enabled: bool) -> None:
    """Toggle automatic email redaction. Off by default. Used by
    ``monitor.init(strict_email_scrub=True)`` for GDPR-strict deployments
    that don't want emails on the dashboard."""
    global _strict_email_scrub
    _strict_email_scrub = enabled


_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer / Basic tokens
    re.compile(_LB + r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
    # JWT — three base64url segments separated by dots
    re.compile(_LB + r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+" + _LA),
    # Stripe-style API keys (sk/pk/rk)_(live|test)_…
    re.compile(_LB + r"(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}" + _LA),
    # Onelo SDK keys: onelo_pk_live_… / onelo_sk_live_…
    re.compile(_LB + r"onelo_(?:pk|sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}" + _LA),
    # Credit-card numbers — separated form
    re.compile(
        _LB
        + r"(?:4[0-9]{3}|5[1-5][0-9]{2}|3[47][0-9]{2}|6(?:011|5[0-9]{2}))"
        r"[\s-][0-9]{4}[\s-][0-9]{4}[\s-][0-9]{4}"
        + _LA
    ),
    # Credit-card numbers — unseparated greedy form (Visa/MC/Discover).
    # ``[0-9]{12,}`` lets the regex consume 12+ trailing digits in one go,
    # then ``(?![0-9])`` anchors at the right side of the digit run. This
    # catches PANs glued to extra digits (``4111111111111111111`` — 19
    # digits) instead of failing the boundary and leaking the secret.
    re.compile(
        _LB + r"(?:4|5[1-5]|6(?:011|5))[0-9]{14,}(?![0-9])"
    ),
    # Credit-card numbers — unseparated greedy Amex
    re.compile(_LB + r"3[47][0-9]{13,}(?![0-9])"),
)


# App-supplied extra sensitive header/key names (via monitor.init(sensitive_headers=...)).
# Substring-matched, separator-normalised — lets an app redact oddly-named secret
# headers the built-in patterns don't recognise.
_EXTRA_SENSITIVE_KEYS: set[str] = set()


def configure_sensitive_keys(names: "Iterable[str] | None") -> None:
    """Register extra header/meta key names to redact (replaces the set).
    Names are lower-cased and dash→underscore normalised; matched as substrings."""
    _EXTRA_SENSITIVE_KEYS.clear()
    if names:
        _EXTRA_SENSITIVE_KEYS.update(n.lower().replace("-", "_") for n in names)


def is_pii_key(key: str) -> bool:
    """True if the key looks like it carries a secret. Case-insensitive
    substring match against the denylist; dashes are normalised to
    underscores so ``X-API-KEY`` matches ``api_key`` as expected.
    """
    normalised = key.lower().replace("-", "_")
    if any(needle in normalised for needle in PII_KEY_SUBSTRINGS):
        return True
    if any(needle in normalised for needle in _EXTRA_SENSITIVE_KEYS):
        return True
    # Segment "key" catches custom secret headers like x-anthropic-key /
    # x-groq-key / x-elevenlabs-key without false-positives like "monkey".
    return "key" in normalised.split("_")


def scrub_text(text: str | None) -> str | None:
    """Replace any sensitive substring inside an arbitrary text payload
    (e.g. an exception message that may include a Bearer token).

    When ``set_strict_email_scrub(True)`` was called (typically via
    ``monitor.init(strict_email_scrub=True)``), email addresses are also
    redacted. Off by default — most teams want emails for debugging.
    """
    if not text:
        return text
    out = text
    for pattern in _VALUE_PATTERNS:
        out = pattern.sub(REDACTED, out)
    if _strict_email_scrub:
        out = _EMAIL_PATTERN.sub(REDACTED, out)
    return out


def scrub_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Filter a header dict, redacting sensitive values."""
    if not headers:
        return headers
    return {
        k: REDACTED if (k.lower() in SENSITIVE_HEADERS or is_pii_key(k)) else v
        for k, v in headers.items()
    }


def scrub_url(url: str | None) -> str | None:
    """Strip query-param values for sensitive keys; preserve the rest of
    the URL untouched. Path segments containing emails / UUIDs are also
    matched against value regexes. Fragment is scrubbed too — OAuth
    implicit-grant flows return tokens in ``#access_token=…`` so a
    captured redirect URL would otherwise leak the token verbatim.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub_text(url)
    if parts.query:
        params = parse_qsl(parts.query, keep_blank_values=True)
        scrubbed = [
            (k, REDACTED if k.lower() in SENSITIVE_QUERY_KEYS else v)
            for k, v in params
        ]
        new_query = urlencode(scrubbed)
    else:
        new_query = parts.query
    # Path may contain emails / tokens — run text scrubber on it.
    new_path = scrub_text(parts.path) or parts.path
    # Fragment uses the same key=value format as query strings in OAuth
    # implicit-grant responses (``#access_token=…&id_token=eyJ…``). Apply
    # the query-key denylist there too, then run the value-regex scrubber
    # for any leftover token-shaped strings.
    new_fragment = parts.fragment
    if parts.fragment:
        if "=" in parts.fragment:
            frag_params = parse_qsl(parts.fragment, keep_blank_values=True)
            scrubbed_frag = [
                (k, REDACTED if k.lower() in SENSITIVE_QUERY_KEYS else v)
                for k, v in frag_params
            ]
            new_fragment = urlencode(scrubbed_frag)
        new_fragment = scrub_text(new_fragment) or new_fragment
    return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, new_fragment))


def scrub_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """Recursively scrub a JSON-shaped meta dict.

    Returns a new dict; the original is untouched. If anything was redacted,
    a ``_onelo_redacted`` list is appended so the developer can see which
    keys were cleaned.
    """
    if meta is None:
        return None
    redacted: set[str] = set()
    cleaned = _scrub_dict(meta, redacted, depth=0)
    if redacted and isinstance(cleaned, dict):
        cleaned["_onelo_redacted"] = sorted(redacted)
    return cleaned


# ─── internals ──────────────────────────────────────────────────────────────


def _scrub_dict(
    value: dict[str, Any],
    redacted: set[str],
    depth: int,
) -> dict[str, Any]:
    if depth >= _MAX_DEPTH:
        # Replace the whole sub-tree so a deliberately-deep payload cannot
        # hide secrets below the limit.
        redacted.add("_onelo_depth_exceeded")
        return {"_onelo_depth_exceeded": True}
    out: dict[str, Any] = {}
    for k, v in value.items():
        if k == "_onelo_redacted":
            out[k] = v
            continue
        if isinstance(k, str) and is_pii_key(k):
            redacted.add(k)
            out[k] = REDACTED
            continue
        out[k] = _scrub_value(v, redacted, depth + 1)
    return out


def _scrub_value(value: Any, redacted: set[str], depth: int) -> Any:
    # Strings always get scrubbed for embedded secret patterns regardless
    # of depth — they are leaf values, no recursion concern.
    if isinstance(value, str):
        return scrub_text(value)
    # Dicts always go through `_scrub_dict` which owns the depth check
    # (so deep sub-trees are collapsed to a marker, not leaked through).
    if isinstance(value, dict):
        return _scrub_dict(value, redacted, depth)
    if isinstance(value, (list, tuple)):
        if depth >= _MAX_DEPTH:
            redacted.add("_onelo_depth_exceeded")
            return [REDACTED]
        # Coerce to a plain ``list``/``tuple`` rather than ``type(value)(...)``.
        # Subclasses (namedtuple, ``UserList``, custom containers) often have
        # a custom ``__init__`` that doesn't accept a single iterable, so
        # ``type(value)(generator)`` raises ``TypeError`` and would propagate
        # all the way up to user code. A plain coercion keeps the data and
        # doesn't lie about the original type — the wire format is JSON
        # anyway, so subclass identity is meaningless downstream.
        scrubbed = [_scrub_value(v, redacted, depth + 1) for v in value]
        return tuple(scrubbed) if isinstance(value, tuple) and not _is_namedtuple(value) else scrubbed
    return value


def _is_namedtuple(value: Any) -> bool:
    """``namedtuple`` subclasses fail the simple ``tuple(iterable)`` test
    above (they require positional args). We collapse them to a plain list
    so the scrubber output is always JSON-safe.
    """
    return isinstance(value, tuple) and hasattr(value, "_fields")


__all__ = [
    "PII_KEY_SUBSTRINGS",
    "REDACTED",
    "SENSITIVE_HEADERS",
    "SENSITIVE_QUERY_KEYS",
    "is_pii_key",
    "scrub_headers",
    "scrub_meta",
    "scrub_text",
    "scrub_url",
]
