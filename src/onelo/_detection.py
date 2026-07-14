"""Strategy resolution.

In v1, "auto" maps to "sse" — SSE-per-worker is the default for indie scale.
This function exists so we can add detection logic later (multi-worker
heuristics, Redis pub/sub coordinator) without changing the public API.
"""

VALID_STRATEGIES = frozenset({"auto", "sse", "polling"})

# Strategies a future v1.x release may add. Reject explicitly so users get a
# clear error rather than silently falling back to something else.
DEFERRED_STRATEGIES = frozenset({"redis"})


def resolve_strategy(strategy: str) -> str:
    """Resolve "auto" to a concrete strategy. Validate input.

    Returns one of: "sse", "polling".
    Raises ValueError on invalid or deferred-but-not-yet-supported values.
    """
    if strategy in DEFERRED_STRATEGIES:
        raise ValueError(
            f"strategy={strategy!r} is not supported in v1. "
            "Use 'sse' or 'polling'. Redis pub/sub coordinator may land in v1.x."
        )
    if strategy not in VALID_STRATEGIES:
        raise ValueError(
            f"strategy must be one of {sorted(VALID_STRATEGIES)}, got {strategy!r}"
        )
    if strategy == "auto":
        return "sse"
    return strategy
