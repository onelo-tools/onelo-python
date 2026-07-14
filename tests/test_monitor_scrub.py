"""Tests for the PII scrubber — mirrors backend/Swift scrubber behaviour.

These tests are the security gate: every regression here is potentially a
GDPR incident, so they're worth failing the build over.
"""
import pytest

from onelo.monitor._scrub import (
    REDACTED,
    is_pii_key,
    scrub_headers,
    scrub_meta,
    scrub_text,
    scrub_url,
)


# ── is_pii_key ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "password", "Password", "user_password",
    "api_key", "apiKey", "X-API-KEY",
    "Authorization", "set-cookie", "client_secret",
    "credentials", "private_key", "ssn", "cvv",
])
def test_is_pii_key_matches_denylist(key: str) -> None:
    assert is_pii_key(key) is True


@pytest.mark.parametrize("key", [
    "plan", "region", "user_id", "email",
    "feature_name", "request_id", "tenant",
])
def test_is_pii_key_safe_keys_pass(key: str) -> None:
    assert is_pii_key(key) is False


# ── scrub_text ─────────────────────────────────────────────────────────────

def test_scrub_text_redacts_bearer_token() -> None:
    out = scrub_text("Auth failed: Bearer abcDEF123_token")
    assert out == f"Auth failed: {REDACTED}"


def test_scrub_text_redacts_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature_part"
    out = scrub_text(f"token={jwt} more")
    assert REDACTED in out
    assert jwt not in out


def test_scrub_text_redacts_stripe_key() -> None:
    out = scrub_text("Using sk_live_abcdefghij1234567890")
    assert REDACTED in out
    assert "sk_live_" not in out


def test_scrub_text_redacts_onelo_keys() -> None:
    out = scrub_text("Got onelo_pk_live_CL2FmTgopEuY2QPqZms4scVR from cache")
    assert REDACTED in out
    assert "onelo_pk_live_" not in out


def test_scrub_text_redacts_cc_separated() -> None:
    out = scrub_text("Card 4111-1111-1111-1111 declined")
    assert REDACTED in out


def test_scrub_text_redacts_cc_unseparated_visa() -> None:
    """Regression: pre-fix regex required separators between groups, so a
    raw 16-digit card slipped past the scrubber."""
    out = scrub_text("Card 4111111111111111 declined")
    assert REDACTED in out
    assert "4111111111111111" not in out


def test_scrub_text_redacts_cc_unseparated_amex() -> None:
    out = scrub_text("Charge 378282246310005 OK")
    assert REDACTED in out


def test_scrub_text_passes_benign_text() -> None:
    out = scrub_text("Plan upgraded to pro")
    assert out == "Plan upgraded to pro"


def test_scrub_text_handles_none_and_empty() -> None:
    assert scrub_text(None) is None
    assert scrub_text("") == ""


# ── scrub_meta ─────────────────────────────────────────────────────────────

def test_scrub_meta_redacts_top_level_pii_keys() -> None:
    meta = {"plan": "pro", "password": "hunter2"}
    out = scrub_meta(meta)
    assert out is not None
    assert out["plan"] == "pro"
    assert out["password"] == REDACTED
    assert "password" in out["_onelo_redacted"]


def test_scrub_meta_redacts_nested_pii_keys() -> None:
    meta = {"outer": "ok", "nested": {"secret": "x", "ok_field": "yes"}}
    out = scrub_meta(meta)
    assert out is not None
    assert out["nested"]["secret"] == REDACTED
    assert out["nested"]["ok_field"] == "yes"


def test_scrub_meta_redacts_secret_pattern_in_string_value() -> None:
    out = scrub_meta({"log": "Bearer xyz123 happened"})
    assert out is not None
    assert REDACTED in out["log"]


def test_scrub_meta_returns_none_for_none() -> None:
    assert scrub_meta(None) is None


def test_scrub_meta_no_redacted_marker_when_clean() -> None:
    out = scrub_meta({"plan": "pro", "region": "eu"})
    assert out is not None
    assert "_onelo_redacted" not in out


def test_scrub_meta_collapses_deep_subtree() -> None:
    """Regression: pre-fix the scrubber returned a deep sub-tree untouched
    once the depth limit was hit, hiding any secrets below."""
    deep: dict = {"password": "leaked"}
    for _ in range(15):
        deep = {"nested": deep}

    out = scrub_meta(deep)
    assert out is not None

    # Walk down looking for the marker; the deep "password" must not survive.
    node = out
    found_marker = False
    for _ in range(20):
        if not isinstance(node, dict):
            break
        if node.get("_onelo_depth_exceeded") is True:
            found_marker = True
            break
        next_val = node.get("nested")
        if next_val is None:
            break
        node = next_val
    assert found_marker


def test_scrub_meta_preserves_list_structure() -> None:
    out = scrub_meta({"items": [{"token": "x"}, {"plan": "pro"}]})
    assert out is not None
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["plan"] == "pro"


# ── scrub_headers ──────────────────────────────────────────────────────────

def test_scrub_headers_redacts_authorization_and_cookie() -> None:
    h = {"Authorization": "Bearer xyz", "X-Plan": "pro", "cookie": "session=abc"}
    out = scrub_headers(h)
    assert out is not None
    assert out["Authorization"] == REDACTED
    assert out["cookie"] == REDACTED
    assert out["X-Plan"] == "pro"


def test_scrub_headers_handles_none() -> None:
    assert scrub_headers(None) is None
    assert scrub_headers({}) == {}


# ── scrub_url ──────────────────────────────────────────────────────────────

def test_scrub_url_redacts_sensitive_query_params() -> None:
    out = scrub_url("https://api.example.com/x?token=secret&user=alice")
    assert out is not None
    assert "token=" + REDACTED in out or "token=%5BREDACTED%5D" in out
    assert "user=alice" in out


def test_scrub_url_passes_benign() -> None:
    out = scrub_url("https://api.example.com/users?limit=10")
    assert out is not None
    assert "limit=10" in out


def test_scrub_url_redacts_token_pattern_in_path() -> None:
    """A Bearer-style token embedded in the path is caught by scrub_text
    on the path component (rare, but happens with sloppy redirect URLs)."""
    out = scrub_url("https://api.example.com/oauth/eyJabc.def.ghi/callback")
    assert out is not None
    assert "eyJabc.def.ghi" not in out


def test_scrub_url_keeps_email_in_path_by_default() -> None:
    """Email is intentionally NOT auto-redacted (consistent with backend
    Python and Swift SDK). Devs often log emails for support / debugging.
    Strict mode would be opt-in if we add it later."""
    out = scrub_url("https://api.example.com/users/x@y.com/orders")
    assert out is not None
    assert "x@y.com" in out


def test_scrub_url_handles_none_and_empty() -> None:
    assert scrub_url(None) is None
    assert scrub_url("") == ""
