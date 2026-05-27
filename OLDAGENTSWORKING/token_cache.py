"""Simple in-memory token cache for Agent 365 observability tokens.

Adapted (lightly) from the Microsoft autonomous sample:
https://github.com/microsoft/Agent365-Samples/tree/main/python/autonomous/github-trending
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_cache: dict[str, tuple[str, datetime]] = {}

# Tokens are treated as expired this many minutes BEFORE their nominal expiry,
# so the agent never sends a stale or about-to-die bearer.
_EXPIRY_BUFFER = timedelta(minutes=5)


def cache_token(
    agent_id: str,
    tenant_id: str,
    token: str,
    expires_in: timedelta = timedelta(hours=1),
) -> None:
    """Store a token for a given (agent, tenant) pair."""
    key = f"{agent_id}:{tenant_id}"
    expires_at = datetime.now(timezone.utc) + expires_in
    with _lock:
        _cache[key] = (token, expires_at)


def get_cached_token(agent_id: str, tenant_id: str) -> str | None:
    """Return the cached token if still valid (with safety buffer)."""
    key = f"{agent_id}:{tenant_id}"
    with _lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        token, expires_at = entry
        if datetime.now(timezone.utc) + _EXPIRY_BUFFER >= expires_at:
            del _cache[key]
            return None
        return token
