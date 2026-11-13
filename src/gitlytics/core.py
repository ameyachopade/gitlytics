# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
# Licensed under Apache-2.0 with Commons Clause License Condition v1.0.
# See LICENSE for full terms.
"""
gitlytics/core.py
Handles fetching traffic and deep metadata from the GitHub API.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import sys
import threading
import time
from collections import OrderedDict

import requests
from requests.adapters import HTTPAdapter
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BASE = "https://api.github.com"
GITHUB_USERNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$", re.IGNORECASE)
GITHUB_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]{1,100}$")


def validate_owner_repo(owner: str, repo: str) -> tuple[str, str]:
    clean_owner = (owner or "").strip()
    clean_repo = (repo or "").strip()
    if (
        not clean_owner
        or not clean_repo
        or "/" in clean_owner
        or "/" in clean_repo
        or "\\" in clean_owner
        or "\\" in clean_repo
        or ".." in clean_owner
        or ".." in clean_repo
    ):
        raise ValueError(
            f"fetch_star_history requires owner and repo (no '/' in repo); got {owner!r}/{repo!r}."
        )
    if (
        not GITHUB_USERNAME_RE.match(clean_owner)
        or not GITHUB_REPO_RE.match(clean_repo)
        or clean_repo in (".", "..")
    ):
        raise ValueError(f"Invalid GitHub repository {f'{owner}/{repo}'!r}.")
    return clean_owner, clean_repo
# Single source of truth for the GitHub media-type + API version headers.
_GITHUB_BASE_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_PUBLIC_HEADERS = dict(_GITHUB_BASE_HEADERS)
_PER_PAGE = 100              # page size for paginated endpoints

_SESSION_LOCAL = threading.local()


def _get_session() -> requests.Session:
    sess = getattr(_SESSION_LOCAL, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _SESSION_LOCAL.session = sess
    return sess


def _http_get(url: str, **kwargs):
    """GET through the thread-local pooled session (keep-alive + bounded pool).

    All GitHub calls funnel through here so connection reuse actually happens.
    Tests patch this symbol instead of ``requests.get``.
    """
    return _get_session().get(url, **kwargs)


class GitHubRateLimitError(Exception):
    """Raised when GitHub responds with 403/429 (rate limit or abuse guard)."""


class GitHubAPIError(RuntimeError):
    """Raised when GitHub returns an unexpected upstream failure (5xx / bad payload).

    Distinct from :class:`TokenExpiredError` (401/403 auth) and
    :class:`GitHubRateLimitError` (429 / secondary limits). The CLI maps this
    to exit code 1 with a clean message instead of a raw traceback
    (AF2-LIB-03).

    Subclasses ``RuntimeError`` so callers that already guard against a
    generic upstream failure keep working.
    """


class StarHistoryFetchError(Exception):
    """Raised for non-rate-limit star-history failures (network, parse, validation)."""


class StargazersRestrictedError(Exception):
    """Raised when GitHub returns 404/403 on the /stargazers endpoint.

    As of June 30, 2026, GitHub restricted this endpoint to repository
    admins and collaborators only. It is distinct from a repo-not-found
    404 which is raised earlier via StarHistoryFetchError.
    """


class TokenExpiredError(Exception):
    """Raised when the GitHub token is invalid, expired, or revoked (HTTP 401/403)."""


# GitHub restricted the stargazers endpoint for public repositories the
# caller does not own. Remember restricted (owner, repo, token) triples for a
# while so repeated chart requests do not re-issue doomed page requests. This
# is a negative cache only; successful star histories are never cached here.
#
# The token fingerprint is part of the key (AF2-LIB-05): GitHub restricts the
# endpoint per *caller*, so one user's 404 must not poison the repository for
# every other user of the same process.
_STARGAZER_RESTRICTED_TTL = 6 * 60 * 60
_STARGAZER_RESTRICTED_MAXSIZE = 1024
_stargazer_restricted_cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_stargazer_restricted_lock = threading.Lock()


def _token_fingerprint(token: str | None) -> str:
    """Return a stable, non-reversible fingerprint for a GitHub token.

    The raw token must never end up in a cache key or a log line, but the
    negative stargazer cache has to distinguish callers. An empty string is
    used for anonymous (token-less) requests.
    """
    if not token:
        return ""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def _stargazer_cache_key(owner: str, repo: str, token: str | None = None) -> tuple[str, str, str]:
    return (owner.strip().lower(), repo.strip().lower(), _token_fingerprint(token))


def _is_stargazers_restricted(owner: str, repo: str, token: str | None = None) -> bool:
    key = _stargazer_cache_key(owner, repo, token)
    now_mono = time.monotonic()
    with _stargazer_restricted_lock:
        marked_at = _stargazer_restricted_cache.get(key)
        if marked_at is None:
            return False
        if now_mono - marked_at > _STARGAZER_RESTRICTED_TTL:
            _stargazer_restricted_cache.pop(key, None)
            return False
        return True


def _mark_stargazers_restricted(owner: str, repo: str, token: str | None = None) -> None:
    key = _stargazer_cache_key(owner, repo, token)
    with _stargazer_restricted_lock:
        _stargazer_restricted_cache[key] = time.monotonic()
        _stargazer_restricted_cache.move_to_end(key)
        while len(_stargazer_restricted_cache) > _STARGAZER_RESTRICTED_MAXSIZE:
            _stargazer_restricted_cache.popitem(last=False)


def _approximate_star_history(
    meta: dict, total_stars: int, today_str: str, now: datetime
) -> list[dict]:
    """Return a documented two-point approximation for restricted repos.

    GitHub no longer exposes the stargazer list for repositories the caller
    does not own, so an exact dated timeline is impossible. Drawing a straight
    line from repository creation (0 stars) to today (live count) preserves the
    chart shape and the correct right edge without pretending to know exact
    star dates.
    """
    created_at_raw = meta.get("created_at") if isinstance(meta, dict) else None
    start_date = str(created_at_raw)[:10] if created_at_raw else ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date) or start_date >= today_str:
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    if start_date == today_str:
        return [{"date": today_str, "total": total_stars}]
    return [
        {"date": start_date, "total": 0},
        {"date": today_str, "total": total_stars},
    ]


def format_auth_header(token: str | None) -> str | None:
    if not token:
        return None
    tok = str(token).strip()
    if not tok:
        return None
    if tok.startswith("Bearer ") or tok.startswith("token "):
        return tok
    return f"Bearer {tok}"


def make_headers(token: str) -> dict:
    # Auth headers for authenticated API calls
    auth = format_auth_header(token)
    h = {**_GITHUB_BASE_HEADERS}
    if auth:
        h["Authorization"] = auth
    return h


def validate_token(token: str) -> tuple[bool, str]:
    # Try to reach the GitHub /user endpoint with the given token
    try:
        r = _http_get(f"{BASE}/user", headers=make_headers(token), timeout=10)
        if r.status_code == 200:
            data = r.json()
            if not isinstance(data, dict):
                return False, "Invalid response from GitHub."
            return True, data.get("login", "")
    except requests.exceptions.ConnectionError:
        return False, "No internet connection."
    except Exception as e:
        logger.warning("Token validation error: %s", e)
        return False, "Authentication failed."

    if r.status_code == 401:
        return False, "Invalid token — authentication failed (401 Unauthorized)."
    if r.status_code == 403:
        remaining = r.headers.get("x-ratelimit-remaining", "")
        text_lower = r.text.lower() if r.text else ""
        if remaining == "0" or "rate limit" in text_lower or "abuse" in text_lower:
            return False, "Rate limit exceeded (HTTP 403). Retrying next cycle."
        return False, "Token has insufficient permissions (403 Forbidden)."
    if r.status_code == 429:
        return False, "Rate limit exceeded (HTTP 429). Retrying next cycle."
    return False, f"GitHub returned HTTP {r.status_code}."


def get_user_profile(token: str) -> dict:
    """Returns the authenticated user's full profile."""
    try:
        r = _http_get(f"{BASE}/user", headers=make_headers(token), timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "login": data.get("login", ""),
                "name": data.get("name") or data.get("login", ""),
                "avatar_url": data.get("avatar_url", ""),
                "bio": data.get("bio"),
                "location": data.get("location"),
                "followers": data.get("followers", 0),
                "following": data.get("following", 0),
            }
    except Exception as exc:
        logger.warning(f"Could not fetch user profile: {exc}")
    return {"login": "", "name": "", "avatar_url": "", "followers": 0, "following": 0}


def validate_github_username(username: str) -> str:
    u = (username or "").strip()
    if not u or not GITHUB_USERNAME_RE.match(u):
        raise ValueError("Invalid GitHub username.")
    return u


def get_public_user(username: str, token: str | None = None) -> dict:
    """Fetches a public GitHub user profile — no PAT required, but uses token if provided."""
    username = validate_github_username(username)
    headers = make_headers(token) if token else _PUBLIC_HEADERS
    try:
        r = _http_get(f"{BASE}/users/{username}", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return {
                "login": data.get("login", ""),
                "name": data.get("name") or data.get("login", ""),
                "avatar_url": data.get("avatar_url", ""),
                "bio": data.get("bio"),
                "location": data.get("location"),
                "blog": data.get("blog"),
                "twitter_username": data.get("twitter_username"),
                "html_url": data.get("html_url", ""),
                "followers": data.get("followers", 0),
                "following": data.get("following", 0),
                "public_repos": data.get("public_repos", 0),
                "created_at": data.get("created_at", ""),
            }
        if r.status_code == 404:
            raise ValueError(f"User '{username}' not found.")
        logger.warning(f"Failed to fetch user {username}: HTTP {r.status_code}")
        raise RuntimeError(f"GitHub returned HTTP {r.status_code}.")
    except ValueError:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning(f"Could not fetch public user profile: {exc}")
        raise RuntimeError("GitHub is unreachable.") from exc


def _normalise_repo(repo: dict) -> dict:
    # Extract only the fields the dashboard needs from a raw GitHub repo object
    return {
        "name": repo.get("name", ""),
        "full_name": repo.get("full_name", ""),
        "description": repo.get("description"),
        "html_url": repo.get("html_url", ""),
        "fork": repo.get("fork", False),
        "stargazers_count": repo.get("stargazers_count", 0),
        "forks_count": repo.get("forks_count", 0),
        "watchers_count": repo.get("watchers_count", 0),
        "language": repo.get("language"),
        "open_issues_count": repo.get("open_issues_count", 0),
        "topics": repo.get("topics", []),
        "pushed_at": repo.get("pushed_at", ""),
        "created_at": repo.get("created_at", ""),
        "default_branch": repo.get("default_branch", "main"),
    }


def _fetch_public_repos_by_updated(username: str, per_page: int, token: str | None = None) -> list:
    # Call 1 of 2: most-recently-updated repos (catches active new projects)
    headers = make_headers(token) if token else _PUBLIC_HEADERS
    try:
        r = _http_get(
            f"{BASE}/users/{username}/repos",
            headers=headers,
            params={"per_page": per_page, "page": 1, "sort": "updated", "type": "public"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        batch = r.json()
        return [_normalise_repo(repo) for repo in batch] if isinstance(batch, list) else []
    except Exception as exc:
        logger.warning(f"Could not fetch recent repos for {username}: {exc}")
        return []


def _fetch_public_repos_by_stars(username: str, per_page: int, token: str | None = None) -> list:
    # Call 2 of 2: top-starred repos via Search API (catches famous older projects)
    headers = make_headers(token) if token else _PUBLIC_HEADERS
    try:
        r = _http_get(
            f"{BASE}/search/repositories",
            headers=headers,
            params={"q": f"user:{username}", "sort": "stars", "order": "desc", "per_page": per_page},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, dict) or "items" not in data:
            return []
        return [_normalise_repo(repo) for repo in data["items"]]
    except Exception as exc:
        logger.warning(f"Could not fetch starred repos for {username}: {exc}")
        return []


def get_public_repos(username: str, max_repos: int = 50, token: str | None = None) -> list:
    """
    Fetches up to max_repos best public repos for a username using two calls:
      1. Most recently updated (catches active projects)
      2. Most starred via Search API (catches famous older projects)
    The two lists are merged, deduplicated, sorted by stars, and capped at max_repos.
    """
    username = validate_github_username(username)
    # Don't request more than the caller actually wants.
    per_page = min(50, max_repos) if max_repos > 0 else 50
    recent = _fetch_public_repos_by_updated(username, per_page=per_page, token=token)
    starred = _fetch_public_repos_by_stars(username, per_page=per_page, token=token)

    seen: set = set()
    merged = []
    for repo in recent + starred:
        key = repo.get("full_name", "")
        if key and key not in seen:
            seen.add(key)
            merged.append(repo)

    merged.sort(key=lambda r: r.get("stargazers_count") or 0, reverse=True)
    return merged[:max_repos]


# ── Rate-limit backoff budget (AF2-LIB-14) ───────────────────────────────────
# GitHub's secondary-rate-limit responses can carry `Retry-After: 60`. Two
# retries of that length blocked a single request for ~120 seconds with no
# output at all. These bounds keep the worst case short and observable:
#   * _RETRY_BUDGET_SECONDS caps the *total* time spent sleeping between
#     retries for one logical request.
#   * _REQUEST_DEADLINE_SECONDS is the overall wall-clock deadline for one
#     logical request (sleeping included).
_RETRY_BUDGET_SECONDS = 30.0
_REQUEST_DEADLINE_SECONDS = 120.0
# Only announce waits of at least this many seconds - sub-second backoffs are
# noise, multi-second ones are what a user needs to know about.
_BACKOFF_ANNOUNCE_THRESHOLD = 1.0


def _cap_backoff(delay: float, slept: float, deadline: float) -> tuple[float, bool]:
    """Clamp a backoff delay to the remaining retry budget and deadline.

    Returns ``(delay_to_sleep, exhausted)``. ``exhausted`` is True when no
    further waiting is allowed, so the caller must give up immediately.
    """
    remaining_budget = _RETRY_BUDGET_SECONDS - slept
    remaining_time = deadline - time.monotonic()
    allowed = min(remaining_budget, remaining_time)
    if allowed <= 0:
        return 0.0, True
    if delay > allowed:
        delay = allowed
    return delay, False


def _announce_backoff(delay: float, rate_limited: bool) -> None:
    """Tell the user *why* the command is hanging before we sleep."""
    if delay < _BACKOFF_ANNOUNCE_THRESHOLD:
        return
    reason = "rate limited" if rate_limited else "GitHub API error"
    try:
        print(f"⚠️  {reason} - waiting {delay:.0f}s before retrying...", file=sys.stderr)
    except Exception:  # pragma: no cover - stderr must never break a fetch
        pass


_deadline_lock = threading.Lock()
_current_deadline: float | None = None


@contextlib.contextmanager
def _operation_deadline(seconds: float | None = None):
    """Publish a shared deadline for the duration of one logical operation.

    The deadline is stored in a module-level slot (not a keyword argument) so
    the worker threads spawned by ``fetch_traffic_data`` share it without
    changing the public signature of ``get_repo_traffic``.

    Nested calls reuse the deadline of the outermost operation, so the whole
    fetch shares one budget instead of getting a fresh one per request.
    """
    global _current_deadline
    with _deadline_lock:
        previous = _current_deadline
        if previous is None:
            limit = _REQUEST_DEADLINE_SECONDS if seconds is None else seconds
            _current_deadline = time.monotonic() + limit
    try:
        yield _current_deadline
    finally:
        with _deadline_lock:
            _current_deadline = previous


def _active_deadline() -> float:
    """Return the deadline of the current operation (or a fresh default)."""
    with _deadline_lock:
        deadline = _current_deadline
    if deadline is None:
        return time.monotonic() + _REQUEST_DEADLINE_SECONDS
    return deadline


def _safe_get(url: str, headers: dict, params: dict = None) -> tuple:
    """Wraps requests.get with error handling, retry/backoff, and returns (data, status_code).

    Rate-limit backoff is bounded (AF2-LIB-14):
      * the total time spent sleeping between retries for a single logical
        request never exceeds ``_RETRY_BUDGET_SECONDS``;
      * every request has an overall deadline (``_REQUEST_DEADLINE_SECONDS``)
        so a request can never block indefinitely;
      * every wait is announced on stderr ("rate limited - waiting Ns") so a
        multi-minute stall is never silent.

    The deadline comes from the enclosing operation (see
    :func:`_operation_deadline`); the public signature is unchanged.
    """
    max_attempts = 3
    deadline = _active_deadline()
    slept = 0.0
    for attempt in range(max_attempts):
        try:
            r = _http_get(url, headers=headers, params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                # GitHub occasionally returns 200 with a soft-error body. Any dict that
                # contains a "message" field is treated as a soft error (caller decides
                # what to do with status 200 + empty body).
                if isinstance(data, dict) and data.get("message"):
                    logger.warning(f"GitHub soft-error for {url}: {data['message']}")
                    return {}, r.status_code
                return data, 200

            response_headers = getattr(r, "headers", None) or {}
            text_lower = r.text.lower() if r.text else ""
            is_secondary_limit = (
                r.status_code == 403
                and (
                    response_headers.get("Retry-After")
                    or response_headers.get("x-ratelimit-remaining") == "0"
                    or "rate limit" in text_lower
                    or "abuse" in text_lower
                )
            )

            if (r.status_code in (429, 500, 502, 503, 504) or is_secondary_limit) and attempt < max_attempts - 1:
                retry_after = 0.0
                try:
                    retry_after = float(response_headers.get("Retry-After", 0) or 0)
                except (TypeError, ValueError):
                    retry_after = 0.0
                if retry_after <= 0 and response_headers.get("X-RateLimit-Reset"):
                    try:
                        reset_ts = float(response_headers["X-RateLimit-Reset"])
                        wait_for = reset_ts - time.time()
                        if 0 < wait_for <= 60:
                            retry_after = wait_for
                    except (TypeError, ValueError):
                        pass
                delay = min(60.0, retry_after if retry_after > 0 else 0.25 * (2 ** attempt))
                delay, exhausted = _cap_backoff(delay, slept, deadline)
                if exhausted:
                    logger.warning(
                        "Giving up on %s after %d attempt(s) - backoff budget exhausted.",
                        url, attempt + 1,
                    )
                    if r.status_code == 429 or is_secondary_limit:
                        return {}, 429
                    return {}, r.status_code
                _announce_backoff(delay, is_secondary_limit or r.status_code == 429)
                time.sleep(delay)
                slept += delay
                continue
            if r.status_code == 429 or is_secondary_limit:
                # Secondary-limit 403s are rate limits in disguise. Normalise
                # them to 429 so every caller classifies them identically.
                logger.warning(f"GitHub API rate limit hit (HTTP {r.status_code}) for {url}.")
                return {}, 429
            if r.status_code == 403:
                logger.warning(f"Access denied (403) for {url}.")
            elif r.status_code != 404:
                logger.warning(f"Unexpected HTTP {r.status_code} for {url}.")
            return {}, r.status_code
        except requests.exceptions.Timeout:
            if attempt < max_attempts - 1:
                delay, exhausted = _cap_backoff(0.25 * (2 ** attempt), slept, deadline)
                if exhausted:
                    logger.warning("Request timed out for %s - backoff budget exhausted.", url)
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
    """Get item count in exactly 1 API call using the Link header pagination trick.

    Requests per_page=1 so GitHub includes a Link header with the last page
    number, which equals the total count. Falls back to len(response) when
    the repository has only one page of results (no Link header present).
    """
    p = dict(params or {})
    p["per_page"] = 1
    try:
        r = _http_get(url, headers=headers, params=p, timeout=10)
        if r.status_code != 200:
            return 0
        link = r.headers.get("Link", "")
        match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
        if match:
            return int(match.group(1))
        # No Link header = all results fit on one page
        data = r.json()
        return len(data) if isinstance(data, list) else 0
    except Exception as exc:
        logger.warning("Link-header count failed for %s: %s", url, exc)
        return 0


def get_deep_repo_stats(token: str, full_name: str) -> dict:
    """Fetch commit count, open PRs, releases, and community health in exactly 4 API calls."""
    h = make_headers(token)
    stats = {
        "total_commits": None,
        "open_prs": None,
        "total_releases": None,
        "last_release_at": None,
        "has_readme": None,
        "has_license": None,
        "has_contributing": None,
        "has_code_of_conduct": None,
    }

    # Call 1: Commit activity (52-week window). GitHub computes this asynchronously
    # and returns 202 while it's being prepared. When ready it gives us a weekly
    # breakdown which also powers the CommitActivity chart in the dashboard.
    ca_data, ca_status = _safe_get(f"{BASE}/repos/{full_name}/stats/commit_activity", h)
    if ca_status == 202:
        logger.info("Commit activity computing (202) for %s — will populate on next fetch.", full_name)
    if isinstance(ca_data, list) and ca_data:
        stats["total_commits"] = sum(week.get("total", 0) for week in ca_data)
    else:
        # Call 1b (fallback): commit_activity not ready — use Link header trick to
        # count commits in 1 request instead of walking up to 50 pages.
        try:
            stats["total_commits"] = _count_via_link_header(
                f"{BASE}/repos/{full_name}/commits", h
            )
        except Exception as exc:
            logger.warning("Could not fetch commit count for %s: %s", full_name, exc)

    # Call 2: Open PRs — Link header trick replaces up to 10 paginated calls.
    try:
        stats["open_prs"] = _count_via_link_header(
            f"{BASE}/repos/{full_name}/pulls", h, {"state": "open"}
        )
    except Exception as exc:
        logger.warning("Could not fetch open PRs for %s: %s", full_name, exc)

    # Call 3: Community health (README, license, contributing, CoC) — 1 call, unchanged.
    cp_data, _ = _safe_get(f"{BASE}/repos/{full_name}/community/profile", h)
    if isinstance(cp_data, dict) and "files" in cp_data:
        files = cp_data["files"]
        stats["has_readme"] = bool(files.get("readme"))
        stats["has_license"] = bool(files.get("license"))
        stats["has_contributing"] = bool(files.get("contributing"))
        stats["has_code_of_conduct"] = bool(files.get("code_of_conduct"))

    # Call 4: Releases — Link header trick for count + per_page=1 returns the
    # newest release (GitHub sorts newest-first) so we get last_release_at too.
    try:
        rel_url = f"{BASE}/repos/{full_name}/releases"
        r = _http_get(rel_url, headers=h, params={"per_page": 1}, timeout=10)
        if r.status_code == 200:
            link = r.headers.get("Link", "")
            match = re.search(r'[?&]page=(\d+)[^>]*>;\s*rel="last"', link)
            data = r.json()
            if match:
                stats["total_releases"] = int(match.group(1))
            else:
                stats["total_releases"] = len(data) if isinstance(data, list) else 0
            if isinstance(data, list) and data:
                stats["last_release_at"] = data[0].get("published_at") or data[0].get("created_at")
    except Exception as exc:
        logger.warning("Could not fetch releases for %s: %s", full_name, exc)

    return stats


def fetch_deep_stats_for_top(token: str, repos_with_views: list, top_n: int = 20) -> dict:
    """Runs get_deep_repo_stats concurrently for the top N repos by views."""
    # Sort by total views descending and take the top slice
    ranked = sorted(repos_with_views, key=lambda x: x.get("total_views", 0), reverse=True)
    top = ranked[:top_n]
    results = {}

    def fetch_one(full_name: str):
        return full_name, get_deep_repo_stats(token, full_name)

    # 5 workers keeps us well inside GitHub's abuse rate limit. The default
    # `get_deep_repo_stats` issues 3 requests per repo (commit activity, PRs,
    # releases) plus a community profile — so 5 workers = 20 concurrent calls.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_one, r["repository"]): r["repository"] for r in top}
        for future in as_completed(futures):
            try:
                name, stats = future.result()
                results[name] = stats
            except Exception as exc:
                logger.warning(f"Deep stats fetch failed for {futures[future]}: {exc}")

    return results


def get_all_repos(token: str) -> list[dict]:
    # Page through every repo the token can see
    headers = make_headers(token)
    repos, page = [], 1
    seen = set()
    while True:
        data, status = _safe_get(f"{BASE}/user/repos", headers, {"per_page": _PER_PAGE, "page": page, "type": "all"})
        if status == 429:
            raise GitHubRateLimitError(
                "GitHub API rate limit exceeded while listing repositories (HTTP 429)."
            )
        if status in (401, 403):
            raise TokenExpiredError(
                f"GitHub token is expired, revoked, or lacks repo permissions (HTTP {status})."
            )
        if not isinstance(data, list):
            if page == 1:
                raise RuntimeError(f"Failed to fetch user repositories (HTTP {status}).")
            logger.warning(f"Failed to fetch repositories page {page} (HTTP {status}); stopping pagination.")
            break
        if len(data) == 0:
            break
        for repo in data:
            fname = repo.get("full_name")
            if fname and fname not in seen:
                seen.add(fname)
                repos.append(repo)
        # Stop when the page is short — this is the natural end of pagination.
        if len(data) < _PER_PAGE:
            break
        page += 1
    return repos


def get_single_repo(token: str, full_name: str) -> dict:
    # Fetch metadata for one specific repo. Only a genuine 404 is recoverable
    # (the caller skips it); auth and rate-limit failures must abort so the
    # CLI/daemon never reports a false success with zero rows.
    owner, _, repo = (full_name or "").partition("/")
    owner, repo = validate_owner_repo(owner, repo)
    clean_name = f"{owner}/{repo}"
    headers = make_headers(token)
    data, status = _safe_get(f"{BASE}/repos/{clean_name}", headers)
    if status == 429:
        raise GitHubRateLimitError(
            f"GitHub API rate limit exceeded while fetching '{full_name}' (HTTP 429)."
        )
    if status in (401, 403):
        raise TokenExpiredError(
            f"GitHub token is expired, revoked, or lacks access to '{full_name}' "
            f"(HTTP {status})."
        )
    if status == 404:
        raise ValueError(f"Repository '{full_name}' not found (HTTP 404).")
    if not data or "name" not in data:
        raise GitHubAPIError(
            f"Failed to fetch repository '{full_name}' (HTTP {status or 'no response'})."
        )
    return data


def _star_headers(token: str | None) -> dict:
    h = {
        **_GITHUB_BASE_HEADERS,
        "Accept": "application/vnd.github.star+json",
    }
    auth = format_auth_header(token)
    if auth:
        h["Authorization"] = auth
    return h


def _is_rate_limited_response(r) -> bool:
    """True when a 403 is really a rate-limit/abuse response."""
    if getattr(r, "status_code", None) == 429:
        return True
    headers = getattr(r, "headers", None) or {}
    if headers.get("Retry-After") or headers.get("x-ratelimit-remaining") == "0":
        return True
    body = getattr(r, "text", "") or ""
    lowered = body.lower()
    return "rate limit" in lowered or "abuse" in lowered


def fetch_star_history(owner: str, repo: str, token: str | None = None) -> list[dict]:
    # Returns list of {date, total} cumulative points. No on-disk cache — the
    # library always reads live from GitHub; the browser (IndexedDB via
    # TanStack) and the Vercel CDN cache handle persistence upstream.
    owner, repo = validate_owner_repo(owner, repo)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Live fetch — pull total star count from the repo metadata endpoint.
    meta, status = _safe_get(f"{BASE}/repos/{owner}/{repo}", _star_headers(token))
    if status == 429:
        raise GitHubRateLimitError("GitHub rate limit reached, try again later")
    if status == 403:
        raise StarHistoryFetchError("Repository access forbidden (HTTP 403)")
    if status == 404:
        raise StarHistoryFetchError("user or repo does not exist")
    if status != 200 or not isinstance(meta, dict):
        raise StarHistoryFetchError(f"Failed to fetch repo metadata (HTTP {status})")
    total_stars = int(meta.get("stargazers_count", 0) or 0)
    if total_stars <= 0:
        return [{"date": today_str, "total": 0}]

    # A previous call already learned that GitHub hides this repository's
    # stargazer list. Skip the doomed page requests and return the documented
    # approximation directly (LIB-08).
    if _is_stargazers_restricted(owner, repo, token):
        return _approximate_star_history(meta, total_stars, today_str, now)

    # Note: the stargazers endpoint with `Accept: application/vnd.github.star+json`
    # returns entries oldest-first (page 1 index 0 is the earliest star).
    headers = _star_headers(token)
    per_page = _PER_PAGE
    points: list[dict] = []
    restricted = False

    try:
        # Choose strategy based on repo size so small repos get a real
        # datewise timeline (every individual star) while large repos use
        # sampling to stay under rate limits.
        SMALL_THRESHOLD = 200
        if total_stars <= SMALL_THRESHOLD:
            # Walk every page at smaller page size for finer granularity.
            small_per_page = 30
            total_pages = (total_stars + small_per_page - 1) // small_per_page
            for p in range(1, total_pages + 1):
                try:
                    r = _http_get(
                        f"{BASE}/repos/{owner}/{repo}/stargazers",
                        headers=headers,
                        params={"page": p, "per_page": small_per_page},
                        timeout=10,
                    )
                except Exception as exc:
                    logger.warning(f"Stargazers fetch failed for {owner}/{repo} page {p}: {exc}")
                    continue
                if r.status_code == 404:
                    # GitHub restricts the stargazer list for non-owned
                    # repositories. Fall back instead of failing the chart.
                    restricted = True
                    _mark_stargazers_restricted(owner, repo, token)
                    logger.warning(
                        "Stargazers endpoint restricted (HTTP 404) for %s/%s; "
                        "returning an approximate timeline.", owner, repo,
                    )
                    break
                if r.status_code == 401:
                    raise StarHistoryFetchError(
                        "GitHub token is invalid or expired while reading stargazers "
                        "(HTTP 401)."
                    )
                if r.status_code == 403 and not _is_rate_limited_response(r):
                    restricted = True
                    _mark_stargazers_restricted(owner, repo, token)
                    logger.warning(
                        "Stargazers endpoint restricted (HTTP 403) for %s/%s; "
                        "returning an approximate timeline.", owner, repo,
                    )
                    break
                if r.status_code in (403, 429):
                    raise GitHubRateLimitError("GitHub rate limit reached, try again later")
                if r.status_code != 200:
                    logger.warning(f"Stargazers page {p} returned HTTP {r.status_code}")
                    continue
                items = r.json()
                if not isinstance(items, list):
                    continue
                for offset in range(len(items)):
                    item = items[offset]
                    if not isinstance(item, dict):
                        continue
                    starred_at = item.get("starred_at")
                    if not starred_at:
                        continue
                    # GitHub returns oldest-first; map the index to the
                    # star's chronological position in the cumulative count.
                    star_position = (p - 1) * small_per_page + offset + 1
                    if star_position < 1:
                        continue
                    points.append({"date": str(starred_at)[:10], "total": star_position})
        else:
            # Pick 10 evenly-spaced pages across the available page range.
            max_pages = 400
            total_pages = min((total_stars + per_page - 1) // per_page, max_pages)
            sample_count = 10
            if total_pages <= sample_count:
                sampled_pages = list(range(1, total_pages + 1))
            else:
                sampled_pages = sorted({
                    1 + round(i * (total_pages - 1) / (sample_count - 1))
                    for i in range(sample_count)
                })
            for p in sampled_pages:
                try:
                    r = _http_get(
                        f"{BASE}/repos/{owner}/{repo}/stargazers",
                        headers=headers,
                        params={"page": p, "per_page": per_page},
                        timeout=10,
                    )
                except Exception as exc:
                    logger.warning(f"Stargazers fetch failed for {owner}/{repo} page {p}: {exc}")
                    continue
                if r.status_code == 404:
                    restricted = True
                    _mark_stargazers_restricted(owner, repo, token)
                    logger.warning(
                        "Stargazers endpoint restricted (HTTP 404) for %s/%s; "
                        "returning an approximate timeline.", owner, repo,
                    )
                    break
                if r.status_code == 401:
                    raise StarHistoryFetchError(
                        "GitHub token is invalid or expired while reading stargazers "
                        "(HTTP 401)."
                    )
                if r.status_code == 403 and not _is_rate_limited_response(r):
                    restricted = True
                    _mark_stargazers_restricted(owner, repo, token)
                    logger.warning(
                        "Stargazers endpoint restricted (HTTP 403) for %s/%s; "
                        "returning an approximate timeline.", owner, repo,
                    )
                    break
                if r.status_code in (403, 429):
                    raise GitHubRateLimitError("GitHub rate limit reached, try again later")
                if r.status_code != 200:
                    logger.warning(f"Stargazers page {p} returned HTTP {r.status_code}")
                    continue
                items = r.json()
                if not isinstance(items, list) or not items:
                    continue
                last = items[-1]
                if not isinstance(last, dict):
                    continue
                starred_at = last.get("starred_at")
                if not starred_at:
                    continue
                offset = len(items) - 1
                star_position = (p - 1) * per_page + offset + 1
                if star_position < 1:
                    continue
                points.append({"date": str(starred_at)[:10], "total": star_position})
    except (GitHubRateLimitError, StargazersRestrictedError, StarHistoryFetchError):
        raise
    except Exception as exc:
        logger.warning("Stargazers history fetch exception for %s/%s: %s", owner, repo, exc)

    if restricted and not points:
        # GitHub hides stargazer details for repositories the caller does not
        # own. Return the documented two-point approximation (LIB-08).
        return _approximate_star_history(meta, total_stars, today_str, now)

    if not points:
        # Fallback: Query repo /events endpoint for WatchEvent timestamps.
        # Only the 30 most recent events are available; anchor the observed
        # stars to the end of the live total so the series does not restart
        # at one star.
        try:
            r_ev = _http_get(f"{BASE}/repos/{owner}/{repo}/events", headers=headers, timeout=10)
            if r_ev.status_code == 200 and isinstance(r_ev.json(), list):
                events = r_ev.json()
                watch_dates = sorted([
                    str(e.get("created_at"))[:10]
                    for e in events
                    if isinstance(e, dict) and e.get("type") == "WatchEvent" and e.get("created_at")
                ])
                if watch_dates:
                    first_total = max(1, total_stars - len(watch_dates) + 1)
                    for i, d in enumerate(watch_dates):
                        points.append({"date": d, "total": first_total + i})
        except Exception as ev_exc:
            logger.warning(f"Events fallback failed for {owner}/{repo}: {ev_exc}")

    if not points:
        return [{"date": today_str, "total": total_stars}]

    # Build a per-day cumulative timeline. Each entry means "by end of <date>,
    # this repo had <total> stars". Today always equals the live stargazers
    # count so the chart's right edge is the current count.
    per_day_max: dict[str, int] = {}
    for p in points:
        d = p["date"]
        per_day_max[d] = max(per_day_max.get(d, 0), int(p["total"]))
    if not per_day_max or max(per_day_max.values()) < total_stars:
        per_day_max[today_str] = total_stars

    # Always ensure a start date (repo creation date or 30 days prior at 0 stars)
    # so the timeline renders a clean line graph instead of a single floating dot.
    created_at_raw = meta.get("created_at") if isinstance(meta, dict) else None
    candidate_start = str(created_at_raw)[:10] if created_at_raw else ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate_start) and candidate_start < today_str:
        start_date = candidate_start
    else:
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    if start_date not in per_day_max:
        per_day_max[start_date] = 0

    # Convert raw "max position on this day" into a running cumulative count.
    # Stars are monotone — total can never decrease across dates.
    sorted_dates = sorted(per_day_max.keys())
    cumulative = 0
    points: list[dict] = []
    for d in sorted_dates:
        cumulative = max(cumulative, per_day_max[d])
        points.append({"date": d, "total": cumulative})
    if points and points[-1]["date"] != today_str:
        points.append({"date": today_str, "total": max(cumulative, total_stars)})
    if points and points[-1]["total"] < total_stars:
        points[-1] = {"date": today_str, "total": total_stars}

    return points


def get_repo_traffic(token: str, full_name: str, metrics: list = None) -> dict:
    # Fetch only the traffic endpoints requested in metrics.
    # The overall deadline of the enclosing operation (AF2-LIB-14) caps the
    # total wait so a rate-limited run cannot stall indefinitely.
    h = make_headers(token)

    views, clones, refs, paths = {}, {}, [], []
    wants_views = metrics is None or "views" in metrics
    wants_clones = metrics is None or "clones" in metrics
    # Endpoints that were not requested are considered "ok" so they do not block
    # row generation; only a failed endpoint that was actually requested does.
    views_ok = True
    clones_ok = True
    releases_count = 0
    traffic_statuses: list[int] = []
    with _operation_deadline():
        if wants_views:
            views, v_status = _safe_get(f"{BASE}/repos/{full_name}/traffic/views", h)
            traffic_statuses.append(v_status)
            views_ok = v_status == 200 and isinstance(views, dict) and "views" in views
        if wants_clones:
            clones, c_status = _safe_get(f"{BASE}/repos/{full_name}/traffic/clones", h)
            traffic_statuses.append(c_status)
            clones_ok = c_status == 200 and isinstance(clones, dict) and "clones" in clones
        if metrics is None or "referrers" in metrics:
            refs, _ = _safe_get(f"{BASE}/repos/{full_name}/traffic/popular/referrers", h)
        if metrics is None or "paths" in metrics:
            paths, _ = _safe_get(f"{BASE}/repos/{full_name}/traffic/popular/paths", h)
    if metrics is None or "releases" in metrics:
        try:
            releases_count = _count_via_link_header(f"{BASE}/repos/{full_name}/releases", h)
        except Exception as exc:
            logger.warning("Could not fetch releases count for %s: %s", full_name, exc)

    # A cycle is usable only when at least one requested traffic endpoint
    # succeeded. When both fail we return no rows, so a transient outage can
    # never overwrite real history with fabricated zeros.
    fetch_ok = (
        (not wants_views and not wants_clones)
        or (wants_views and views_ok)
        or (wants_clones and clones_ok)
    )

    return {
        "views": views if isinstance(views, dict) else {},
        "clones": clones if isinstance(clones, dict) else {},
        "referrers": refs if isinstance(refs, list) else [],
        "paths": paths if isinstance(paths, list) else [],
        "releases": releases_count,
        "_fetch_ok": fetch_ok,
        "_views_ok": views_ok,
        "_clones_ok": clones_ok,
        "_traffic_statuses": traffic_statuses,
    }


def pad_traffic_data(traffic: dict) -> list[dict]:
    # GitHub only returns days with activity — fill gaps with zeros
    views_obj = traffic.get("views") if isinstance(traffic, dict) else {}
    clones_obj = traffic.get("clones") if isinstance(traffic, dict) else {}
    views_list = [
        item for item in ((views_obj.get("views") if isinstance(views_obj, dict) else None) or [])
        if isinstance(item, dict)
    ]
    clones_list = [
        item for item in ((clones_obj.get("clones") if isinstance(clones_obj, dict) else None) or [])
        if isinstance(item, dict)
    ]

    latest_date_str = None
    for item in views_list + clones_list:
        ts = item.get("timestamp", "")
        date_str = ts[:10] if isinstance(ts, str) else ""
        if not date_str:
            continue
        if latest_date_str is None or date_str > latest_date_str:
            latest_date_str = date_str

    now_utc = datetime.now(timezone.utc)
    if latest_date_str:
        try:
            parsed_date = datetime.strptime(latest_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if 0 <= (now_utc.date() - parsed_date.date()).days <= 14:
                end_date = now_utc
            else:
                end_date = parsed_date
        except ValueError:
            logger.warning(f"Could not parse traffic date {latest_date_str!r}; falling back to today.")
            end_date = now_utc
    else:
        end_date = now_utc

    dates = [(end_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14, -1, -1)]
    # Guard against rows missing a timestamp (rare but possible on partial responses).
    views_map = {v.get("timestamp", "")[:10]: v for v in views_list if isinstance(v, dict) and v.get("timestamp")}
    clones_map = {c.get("timestamp", "")[:10]: c for c in clones_list if isinstance(c, dict) and c.get("timestamp")}

    padded = []
    for d in dates:
        v = views_map.get(d, {}) or {}
        c = clones_map.get(d, {}) or {}
        padded.append({
            "date": d,
            "views": int(v.get("count", 0) or 0),
            "unique_visitors": int(v.get("uniques", 0) or 0),
            "clones": int(c.get("count", 0) or 0),
            "unique_cloners": int(c.get("uniques", 0) or 0)
        })
    return padded


def build_tidy_rows(repo: dict, traffic: dict, metrics: list = None) -> list[dict]:
    # One CSV row per calendar day — repo metadata repeated on every row
    if isinstance(traffic, dict) and traffic.get("_fetch_ok") is False:
        logger.warning(
            "Skipping tidy row generation for %s because traffic endpoints failed.",
            repo.get("full_name", "unknown"),
        )
        return []
    views_ok = traffic.get("_views_ok", True) if isinstance(traffic, dict) else True
    clones_ok = traffic.get("_clones_ok", True) if isinstance(traffic, dict) else True
    padded = pad_traffic_data(traffic)
    refs = traffic.get("referrers", [])
    paths = traffic.get("paths", [])

    top_ref = refs[0].get("referrer", "") if refs else ""
    top_ref_views = refs[0].get("count", 0) if refs else 0
    top_ref_uniques = refs[0].get("uniques", 0) if refs else 0
    top_path = paths[0].get("path", "") if paths else ""
    top_path_views = paths[0].get("count", 0) if paths else 0
    top_path_uniques = paths[0].get("uniques", 0) if paths else 0

    rows = []
    for day in padded:
        row = {
            "date": day["date"],
            "repository": repo["full_name"],
            "is_private": repo.get("private", False),
        }
        if (metrics is None or "views" in metrics) and views_ok:
            row["views"] = day["views"]
            row["unique_visitors"] = day["unique_visitors"]
        if (metrics is None or "clones" in metrics) and clones_ok:
            row["clones"] = day["clones"]
            row["unique_cloners"] = day["unique_cloners"]
        if metrics is None or "stars" in metrics:
            row["stars"] = repo.get("stargazers_count", 0)
        if metrics is None or "forks" in metrics:
            row["forks"] = repo.get("forks_count", 0)
        if metrics is None or "releases" in metrics:
            row["releases"] = traffic.get("releases", 0)

        # Repo metadata that the deep dashboard needs
        if metrics is None or "language" in metrics:
            row["language"] = repo.get("language")
        if metrics is None or "topics" in metrics:
            row["topics"] = json.dumps(repo.get("topics", []))
        if metrics is None or "watchers_count" in metrics:
            row["watchers_count"] = repo.get("watchers_count", 0)
        if metrics is None or "pushed_at" in metrics:
            row["pushed_at"] = repo.get("pushed_at", "")
        if metrics is None or "created_at" in metrics:
            row["created_at"] = repo.get("created_at", "")
        if metrics is None or "open_issues_count" in metrics:
            row["open_issues_count"] = repo.get("open_issues_count", 0)

        if metrics is None or "referrers" in metrics:
            row["top_referrer"] = top_ref
            row["top_referrer_views"] = top_ref_views
            row["top_referrer_uniques"] = top_ref_uniques
            row["_raw_referrers"] = json.dumps(refs)

        if metrics is None or "paths" in metrics:
            row["top_path"] = top_path
            row["top_path_views"] = top_path_views
            row["top_path_uniques"] = top_path_uniques
            row["_raw_paths"] = json.dumps(paths)

        rows.append(row)
    return rows


def fetch_traffic_data(token: str, repo_names=None, metrics: list = None, public_only: bool = False) -> pd.DataFrame:
    # Decide whether to fetch one repo, a custom list, or all repos
    if repo_names is not None:
        if isinstance(repo_names, str):
            repo_names = [repo_names]
        # Treat an explicit empty list as "fetch nothing" rather than falling through
        # to get_all_repos (which would fetch every accessible repo).
        if len(repo_names) == 0:
            return pd.DataFrame()
        repos = []
        for name in repo_names:
            try:
                repos.append(get_single_repo(token, name))
            except ValueError as exc:
                logger.warning("Skipping unknown repo '%s': %s", name, exc)
    else:
        repos = get_all_repos(token)
        # AF2-ACT-08: "all" mode used to archive private repository names and
        # their traffic into committed CSVs with no way to opt out. Filtering
        # happens here (not in the CSV writer) so private repos are never even
        # fetched. Explicit `repo_name` entries are still honoured.
        if public_only:
            repos = [r for r in repos if not r.get("private", False)]

    if not repos:
        return pd.DataFrame()

    all_rows = []
    traffic_results: list[dict] = []
    # One shared deadline for the whole fetch operation (AF2-LIB-14): even
    # with many repositories in flight, the run cannot stall forever. The
    # deadline is published in a module-level slot so the worker threads
    # share it without changing the public signature of get_repo_traffic.
    with _operation_deadline():
        if len(repos) <= 1:
            for repo in repos:
                traffic = get_repo_traffic(token, repo["full_name"], metrics)
                if isinstance(traffic, dict):
                    traffic_results.append(traffic)
                all_rows.extend(build_tidy_rows(repo, traffic, metrics))
        else:
            def _fetch_one(idx_repo):
                idx, repo = idx_repo
                traffic = get_repo_traffic(token, repo["full_name"], metrics)
                return idx, traffic, build_tidy_rows(repo, traffic, metrics)

            ordered_results = []
            with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
                futures = {pool.submit(_fetch_one, (i, r)): i for i, r in enumerate(repos)}
                for fut in as_completed(futures):
                    try:
                        ordered_results.append(fut.result())
                    except Exception as exc:
                        logger.warning("Repo traffic worker failed: %s", exc)
            for _, traffic, rows in sorted(ordered_results, key=lambda x: x[0]):
                if isinstance(traffic, dict):
                    traffic_results.append(traffic)
                all_rows.extend(rows)

    if not all_rows and traffic_results and all(t.get("_fetch_ok") is False for t in traffic_results):
        statuses = [s for t in traffic_results for s in (t.get("_traffic_statuses") or [])]
        if 429 in statuses:
            raise GitHubRateLimitError(
                "GitHub API rate limit exceeded while fetching traffic data (HTTP 429)."
            )
        if any(s in (401, 403) for s in statuses):
            raise TokenExpiredError(
                "GitHub token is invalid or lacks repository traffic permissions (HTTP 401/403)."
            )

    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()


def print_repo_table(df: pd.DataFrame):
    if df.empty:
        print("No data to display.")
        return

    # Replace NaN with empty string for clean printing (no literal "nan").
    # Cast to object first so pandas does not warn about filling float64
    # columns with an incompatible string value (LIB-09).
    safe_df = df.astype(object).where(df.notna(), "")

    repo_width = max(30, safe_df["repository"].astype(str).str.len().max() + 2)
    ref_width = max(15, safe_df["top_referrer"].astype(str).str.len().max() + 2) if "top_referrer" in safe_df.columns else 15
    header = (
        f"{'REPOSITORY':<{repo_width}} "
        f"{'VIEWS':<8} {'U.VIEWS':<8} {'CLONES':<8} {'U.CLONES':<8} "
        f"{'STARS':<8} {'FORKS':<8} {'TOP REFERRER':<{ref_width}}"
    )
    print(header)
    print("-" * len(header))

    total_account_views = 0
    total_account_uniques = 0
    total_account_clones = 0

    def _col_sum(frame: pd.DataFrame, col: str) -> int:
        if col not in frame.columns:
            return 0
        s = frame[col]
        if not pd.api.types.is_numeric_dtype(s):
            s = s.astype(str).str.replace(",", "", regex=False).str.strip()
        num = pd.to_numeric(s, errors="coerce").replace([float("inf"), float("-inf")], 0).fillna(0)
        return int(num.sum())

    def _col_last(frame: pd.DataFrame, col: str) -> int:
        if col not in frame.columns:
            return 0
        s = frame[col]
        if not pd.api.types.is_numeric_dtype(s):
            s = s.astype(str).str.replace(",", "", regex=False).str.strip()
        num = pd.to_numeric(s, errors="coerce").replace([float("inf"), float("-inf")], float("nan")).dropna()
        return int(num.iloc[-1]) if not num.empty else 0

    for repo in df["repository"].unique():
        orig_repo_df = df[df["repository"] == repo]
        views = _col_sum(orig_repo_df, "views")
        u_views = _col_sum(orig_repo_df, "unique_visitors")
        clones = _col_sum(orig_repo_df, "clones")
        u_clones = _col_sum(orig_repo_df, "unique_cloners")
        stars = _col_last(orig_repo_df, "stars")
        forks = _col_last(orig_repo_df, "forks")

        top_ref_series = orig_repo_df["top_referrer"].dropna().astype(str) if "top_referrer" in orig_repo_df.columns else pd.Series(dtype=str)
        top_ref = top_ref_series.iloc[-1] if not top_ref_series.empty and top_ref_series.iloc[-1] != "nan" else ""

        total_account_views += views
        total_account_uniques += u_views
        total_account_clones += clones

        print(
            f"{repo:<{repo_width}} "
            f"{views:<8} {u_views:<8} {clones:<8} {u_clones:<8} "
            f"{stars:<8} {forks:<8} {str(top_ref):<{ref_width}}"
        )

    print("-" * len(header))
    print(f"Total Account Views:  {total_account_views} (Unique: {total_account_uniques})")
    print(f"Total Account Clones: {total_account_clones}")
