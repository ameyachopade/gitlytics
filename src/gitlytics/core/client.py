# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Pooled HTTP transport, auth headers, retry budgets, and deadline management."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import sys
import threading
import time

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

BASE = "https://api.github.com"

# Shared media-type and API version headers for every GitHub REST request.
_GITHUB_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_PUBLIC_HEADERS = dict(_GITHUB_BASE_HEADERS)
_PER_PAGE = 100

_SESSION_LOCAL = threading.local()

# Caps retry sleep and total wall-clock wait so rate limits never hang forever.
_RETRY_BUDGET_SECONDS = 30.0
_REQUEST_DEADLINE_SECONDS = 120.0
_BACKOFF_ANNOUNCE_THRESHOLD = 1.0

_current_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "_current_deadline", default=None
)


def _get_session() -> requests.Session:
    """Return a thread-local requests Session with a bounded 16-connection pool."""
    sess = getattr(_SESSION_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _SESSION_LOCAL.session = sess
    return sess


def _http_get(url: str, **kwargs):
    """Issue a GET request through the thread-local pooled session."""
    return _get_session().get(url, **kwargs)


def format_auth_header(token: str | None) -> str | None:
    """Format a raw token as a Bearer header while preserving pre-prefixed tokens."""
    if not token:
        return None
    tok = str(token).strip()
    if not tok:
        return None
    if tok.startswith("Bearer ") or tok.startswith("token "):
        return tok
    return f"Bearer {tok}"


def make_headers(token: str) -> dict:
    """Build standard GitHub API headers with optional Bearer authorization."""
    auth = format_auth_header(token)
    headers = {**_GITHUB_BASE_HEADERS}
    if auth:
        headers["Authorization"] = auth
    return headers


def _is_rate_limited_response(response) -> bool:
    """Return True when a 403 or 429 response indicates a GitHub rate or abuse limit."""
    if getattr(response, "status_code", None) == 429:
        return True
    headers = getattr(response, "headers", None) or {}
    if headers.get("Retry-After") or headers.get("x-ratelimit-remaining") == "0":
        return True
    body = (getattr(response, "text", "") or "").lower()
    return "rate limit" in body or "abuse" in body


def _cap_backoff(delay: float, slept: float, deadline: float) -> tuple[float, bool]:
    """Clamp backoff sleep to the remaining retry budget and operation deadline."""
    remaining_budget = _RETRY_BUDGET_SECONDS - slept
    remaining_time = deadline - time.monotonic()
    allowed = min(remaining_budget, remaining_time)
    if allowed <= 0:
        return 0.0, True
    if delay > allowed:
        delay = allowed
    return delay, False


def _announce_backoff(delay: float, rate_limited: bool) -> None:
    """Print multi-second retry waits to stderr so long pauses are visible."""
    if delay < _BACKOFF_ANNOUNCE_THRESHOLD:
        return
    reason = "rate limited" if rate_limited else "GitHub API error"
    try:
        print(f"⚠️  {reason} - waiting {delay:.0f}s before retrying...", file=sys.stderr)
    except Exception:
        pass


@contextlib.contextmanager
def _operation_deadline(seconds: float | None = None):
    """Bind a wall-clock deadline to the current execution context."""
    existing = _current_deadline.get()
    if existing is not None:
        yield existing
        return
    limit = _REQUEST_DEADLINE_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + limit
    token = _current_deadline.set(deadline)
    try:
        yield deadline
    finally:
        _current_deadline.reset(token)


def _active_deadline() -> float:
    """Return the active operation deadline, or start a fresh default window."""
    deadline = _current_deadline.get()
    if deadline is None:
        return time.monotonic() + _REQUEST_DEADLINE_SECONDS
    return deadline


def _safe_get(url: str, headers: dict, params: dict = None) -> tuple:
    """Fetch JSON from GitHub with bounded retry/backoff and return (data, status_code)."""
    max_attempts = 3
    deadline = _active_deadline()
    slept = 0.0
    for attempt in range(max_attempts):
        if time.monotonic() >= deadline:
            logger.warning(
                "Skipping %s because the operation deadline has expired.", url
            )
            return {}, -1
        try:
            r = _http_get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                # Treat HTTP 200 payloads containing an error "message" as soft errors.
                if isinstance(data, dict) and data.get("message"):
                    logger.warning(f"GitHub soft-error for {url}: {data['message']}")
                    return {}, r.status_code
                return data, 200

            response_headers = getattr(r, "headers", None) or {}
            text_lower = r.text.lower() if r.text else ""
            is_secondary_limit = r.status_code == 403 and (
                response_headers.get("Retry-After")
                or response_headers.get("x-ratelimit-remaining") == "0"
                or "rate limit" in text_lower
                or "abuse" in text_lower
            )

            if (
                r.status_code in (429, 500, 502, 503, 504) or is_secondary_limit
            ) and attempt < max_attempts - 1:
                retry_after = 0.0
                try:
                    retry_after = float(response_headers.get("Retry-After", 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                if retry_after <= 0 and response_headers.get("X-RateLimit-Reset"):
                    try:
                        wait_for = (
                            float(response_headers["X-RateLimit-Reset"]) - time.time()
                        )
                        if 0 < wait_for <= 60:
                            retry_after = wait_for
                    except (TypeError, ValueError):
                        pass
                delay = min(
                    60.0, retry_after if retry_after > 0 else 0.25 * (2**attempt)
                )
                delay, exhausted = _cap_backoff(delay, slept, deadline)
                if exhausted:
                    logger.warning(
                        "Giving up on %s after %d attempt(s) - backoff budget exhausted.",
                        url,
                        attempt + 1,
                    )
                    if r.status_code == 429 or is_secondary_limit:
                        return {}, 429
                    return {}, r.status_code
                _announce_backoff(delay, is_secondary_limit or r.status_code == 429)
                time.sleep(delay)
                slept += delay
                continue

            if r.status_code == 429 or is_secondary_limit:
                # Normalize secondary-limit 403s to 429 so callers classify them uniformly.
                logger.warning(
                    f"GitHub API rate limit hit (HTTP {r.status_code}) for {url}."
                )
                return {}, 429
            if r.status_code == 403:
                logger.warning(f"Access denied (403) for {url}.")
            elif r.status_code != 404:
                logger.warning(f"Unexpected HTTP {r.status_code} for {url}.")
            return {}, r.status_code
        except requests.exceptions.Timeout:
            if attempt < max_attempts - 1:
                delay, exhausted = _cap_backoff(0.25 * (2**attempt), slept, deadline)
                if exhausted:
                    logger.warning(
                        "Request timed out for %s - backoff budget exhausted.", url
                    )
                    return {}, -1
                time.sleep(delay)
                slept += delay
                continue
            logger.warning(f"Request timed out for {url}.")
            return {}, -1
        except Exception as exc:
            logger.warning(f"Request failed for {url}: {exc}")
            return {}, -1
    return {}, -1


def _count_via_link_header(url: str, headers: dict, params: dict = None) -> int:
    """Count total items in one request using per_page=1 and the Link last-page header."""
    query = dict(params or {})
    query["per_page"] = 1
    try:
        r = _http_get(url, headers=headers, params=query, timeout=10)
        if r.status_code != 200:
            return 0
        link = r.headers.get("Link", "")
        match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
        if match:
            return int(match.group(1))
        data = r.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as exc:
        logger.warning("Link-header count failed for %s: %s", url, exc)
        return 0
