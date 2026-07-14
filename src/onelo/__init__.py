"""Onelo SDK for Python — backend feature gating with real-time updates."""
from onelo._client import Onelo
from onelo._features import Feature
from onelo._version import __version__
from onelo.auth import (
    OneloAuthError,
    OneloAuthForbidden,
    OneloAuthInvalidToken,
    OneloAuthMissingToken,
    OneloAuthRateLimited,
    OneloAuthUnavailable,
    OneloUser,
    verify_token,
    verify_token_sync,
)

__all__ = [
    "Onelo",
    "Feature",
    "__version__",
    "OneloUser",
    "OneloAuthError",
    "OneloAuthMissingToken",
    "OneloAuthInvalidToken",
    "OneloAuthForbidden",
    "OneloAuthUnavailable",
    "OneloAuthRateLimited",
    "verify_token",
    "verify_token_sync",
]
