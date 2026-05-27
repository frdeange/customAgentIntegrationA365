"""
token_cache.py
==============
Thread-safe, in-memory cache for Agent 365 observability bearer tokens.

The Microsoft OpenTelemetry Distro calls our `a365_token_resolver` callback
on **every** span-export batch. The resolver must be synchronous and fast, so
it must NOT mint a token on each call. This cache decouples token *minting*
(slow, ~600 ms, done once by the 3-hop FMI flow) from token *lookup* (sub-µs,
done on every export).

Design
------
* Key: ``f"{agent_id}:{tenant_id}"`` — supports running multiple agents in the
  same process (rare for an autonomous worker, but free).
* Value: ``(token, expires_at)``.
* Expiry buffer: tokens are evicted ``_EXPIRY_BUFFER`` before their nominal
  expiry to guarantee the exporter never sees a bearer that is about to die
  in flight.

Adapted from the Microsoft autonomous sample:
https://github.com/microsoft/Agent365-Samples/tree/main/python/autonomous/github-trending
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

_lock = threading.Lock()
_cache: dict[str, tuple[str, datetime]] = {}

# Treat tokens as expired this many minutes BEFORE their nominal expiry so the
# agent never sends a bearer that is about to expire in flight.
_EXPIRY_BUFFER = timedelta(minutes=5)


def cache_token(
    agent_id: str,
    tenant_id: str,
    token: str,
    expires_in: timedelta = timedelta(hours=1),
) -> None:
    """Store a freshly minted observability bearer for an ``(agent, tenant)`` pair."""
    key = f"{agent_id}:{tenant_id}"
    expires_at = datetime.now(timezone.utc) + expires_in
    with _lock:
        _cache[key] = (token, expires_at)


def get_cached_token(agent_id: str, tenant_id: str) -> str | None:
    """Return the cached bearer if still valid (with safety buffer), else ``None``.

    Returning ``None`` is the documented signal for the Microsoft OpenTelemetry
    exporter to log a warning and skip the current batch — graceful degradation.
    """
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
