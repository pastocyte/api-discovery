"""
http_utils.py
-------------
Rate-limit-aware HTTP GET helper shared by GitHub and GitLab API clients.

Handles:
* GitHub  – ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset``
* GitLab  – ``RateLimit-Remaining`` / ``RateLimit-ResetTime``
* HTTP 429 / 503 with ``Retry-After`` header
* Exponential back-off for transient errors (up to *max_retries* attempts)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# When fewer than this many requests remain, pause until the reset window.
_RATE_LIMIT_BUFFER = 50
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds


def rate_limited_get(
    session: requests.Session,
    url: str,
    *,
    max_retries: int = _MAX_RETRIES,
    **kwargs: Any,
) -> requests.Response:
    """Perform a GET request with automatic rate-limit back-off and retry.

    Reads both GitHub-style (``X-RateLimit-*``) and GitLab-style
    (``RateLimit-*``) headers to decide whether to sleep before the next
    call.

    Args:
        session:     An authenticated :class:`requests.Session`.
        url:         The URL to GET.
        max_retries: Number of retries on 429 / 503 / network errors.
        **kwargs:    Extra keyword arguments forwarded to ``session.get()``.

    Returns:
        The successful :class:`requests.Response`.

    Raises:
        requests.HTTPError: On non-retriable HTTP errors (4xx other than 429).
        requests.RequestException: After all retries are exhausted.
    """
    for attempt in range(max_retries + 1):
        try:
            resp = session.get(url, **kwargs)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            wait = _BACKOFF_BASE ** attempt
            logger.warning("Network error (%s). Retrying in %ds…", exc, wait)
            time.sleep(wait)
            continue

        # --- Rate limit pre-check (applies to both GitHub and GitLab) --------
        _maybe_sleep_for_rate_limit(resp)

        # --- Retriable status codes ------------------------------------------
        if resp.status_code in (429, 503):
            retry_after = int(resp.headers.get("Retry-After", _BACKOFF_BASE ** attempt))
            if attempt == max_retries:
                resp.raise_for_status()
            logger.warning(
                "HTTP %d from %s. Waiting %ds before retry %d/%d…",
                resp.status_code, url, retry_after, attempt + 1, max_retries,
            )
            time.sleep(retry_after)
            continue

        # --- All other errors are non-retriable ------------------------------
        resp.raise_for_status()
        return resp

    # Should never reach here, but satisfies type checkers.
    raise RuntimeError(f"Exhausted retries for GET {url}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _maybe_sleep_for_rate_limit(resp: requests.Response) -> None:
    """Sleep if the rate-limit window is nearly exhausted."""
    headers = resp.headers

    # GitHub uses X-RateLimit-*; GitLab uses RateLimit-*
    remaining_str = headers.get("X-RateLimit-Remaining") or headers.get("RateLimit-Remaining")
    reset_str = headers.get("X-RateLimit-Reset") or headers.get("RateLimit-ResetTime")

    if remaining_str is None:
        return

    try:
        remaining = int(remaining_str)
    except ValueError:
        return

    if remaining < _RATE_LIMIT_BUFFER and reset_str:
        try:
            reset_at = int(reset_str)
            sleep_secs = max(0, reset_at - int(time.time())) + 1
            logger.warning(
                "Rate limit nearly exhausted (%d remaining). Sleeping %ds until reset…",
                remaining, sleep_secs,
            )
            time.sleep(sleep_secs)
        except ValueError:
            pass
