"""Shared HTTP session for api.congress.gov.

Originally a circuit breaker — it tripped a process-wide 15-minute cooldown
on any failure. In practice a single dropped network connection locked
everyone out long after the network had recovered. Removed.

What remains: a shared `requests.Session` for connection pooling, and a
CongressOutageError exception type so existing callers don't need changes.
Each call now fails independently — a transient failure does not poison
subsequent requests.

Usage:

    from sources.congress_breaker import congress_get, CongressOutageError

    try:
        r = congress_get(url, params={"api_key": KEY, ...}, timeout=10)
    except CongressOutageError:
        return None
"""
import requests
from requests.adapters import HTTPAdapter


class CongressOutageError(Exception):
    """Raised when an api.congress.gov call fails (network error or 5xx)."""


# Shared session so every Congress.gov call enjoys HTTP keep-alive.
_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


# Kept for backwards compatibility with any module that still imports these.
# They're no-ops now — there's no breaker state to manage.
def is_tripped() -> bool:
    return False


def trip() -> None:
    pass


def clear() -> None:
    pass


def cooldown_remaining_seconds() -> int:
    return 0


def congress_get(url, params=None, timeout=10, **kwargs):
    """GET against api.congress.gov using the shared session.

    Raises CongressOutageError when the call fails (network error or 5xx).
    Returns the requests.Response on success.
    """
    try:
        r = _session.get(url, params=params, timeout=timeout, **kwargs)
    except Exception as e:
        raise CongressOutageError(f"live call failed: {type(e).__name__}") from e
    if r.status_code in (500, 502, 503, 504):
        raise CongressOutageError(f"upstream {r.status_code}")
    return r
