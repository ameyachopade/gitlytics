# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Cumulative star-history sampling and restricted-endpoint fallback."""

from __future__ import annotations

import contextvars
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from gitlytics.exceptions import (
    GitHubRateLimitError,
    StargazersRestrictedError,
    StarHistoryFetchError,
)

from . import client
from .validation import validate_owner_repo

logger = logging.getLogger(__name__)

# Negative cache for repos where GitHub hides /stargazers for this caller.
_STARGAZER_RESTRICTED_TTL = 6 * 60 * 60
_STARGAZER_RESTRICTED_MAXSIZE = 1024
_stargazer_restricted_cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()
_stargazer_restricted_lock = threading.Lock()


def _token_fingerprint(token: str | None) -> str:
    """Hash a token into a short fingerprint so raw tokens never enter cache keys."""
    if not token:
        return ""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def _stargazer_cache_key(
    owner: str, repo: str, token: str | None = None
) -> tuple[str, str, str]:
    """Build a caller-scoped cache key for restricted stargazer lookups."""
    return (owner.strip().lower(), repo.strip().lower(), _token_fingerprint(token))


def _is_stargazers_restricted(owner: str, repo: str, token: str | None = None) -> bool:
    """Return True if this caller recently saw /stargazers restricted for the repo."""
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


def _mark_stargazers_restricted(
    owner: str, repo: str, token: str | None = None
) -> None:
    """Record that /stargazers is restricted for this (owner, repo, caller) tuple."""
    key = _stargazer_cache_key(owner, repo, token)
    with _stargazer_restricted_lock:
        _stargazer_restricted_cache[key] = time.monotonic()
        _stargazer_restricted_cache.move_to_end(key)
        while len(_stargazer_restricted_cache) > _STARGAZER_RESTRICTED_MAXSIZE:
            _stargazer_restricted_cache.popitem(last=False)


def _approximate_star_history(
    meta: dict,
    total_stars: int,
    today_str: str,
    now: datetime,
) -> list[dict]:
    """Build a two-point fallback curve from repo creation (0) to today's count."""
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


def _star_headers(token: str | None) -> dict:
    """Build headers requesting stargazer timestamps via GitHub's star+json media type."""
    headers = {
        **client._GITHUB_BASE_HEADERS,
        "Accept": "application/vnd.github.star+json",
    }
    auth = client.format_auth_header(token)
    if auth:
        headers["Authorization"] = auth
    return headers


def _fetch_stargazer_page(
    owner: str,
    repo: str,
    page: int,
    per_page: int,
    headers: dict,
    *,
    is_small: bool,
) -> tuple[str, int, list[dict]]:
    """Fetch and parse one stargazers page, classifying auth, rate-limit, and restriction states."""
    try:
        r = client._http_get(
            f"{client.BASE}/repos/{owner}/{repo}/stargazers",
            headers=headers,
            params={"page": page, "per_page": per_page},
            timeout=10,
        )
    except Exception as exc:
        logger.warning(
            f"Stargazers fetch failed for {owner}/{repo} page {page}: {exc}"
        )
        return "error", -1, []

    if r.status_code == 404:
        return "restricted", 404, []
    if r.status_code == 401:
        raise StarHistoryFetchError(
            "GitHub token is invalid or expired while reading stargazers (HTTP 401)."
        )
    if r.status_code == 403 and not client._is_rate_limited_response(r):
        return "restricted", 403, []
    if r.status_code in (403, 429):
        raise GitHubRateLimitError("GitHub rate limit reached, try again later")
    if r.status_code != 200:
        logger.warning(f"Stargazers page {page} returned HTTP {r.status_code}")
        return "skip", r.status_code, []

    items = r.json()
    if not isinstance(items, list) or not items:
        return "ok", 200, []

    page_points: list[dict] = []
    if is_small:
        for offset, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            starred_at = item.get("starred_at")
            if not starred_at:
                continue
            star_position = (page - 1) * per_page + offset + 1
            if star_position >= 1:
                page_points.append({"date": str(starred_at)[:10], "total": star_position})
    else:
        last = items[-1]
        if isinstance(last, dict):
            starred_at = last.get("starred_at")
            if starred_at:
                star_position = (page - 1) * per_page + len(items)
                if star_position >= 1:
                    page_points.append(
                        {"date": str(starred_at)[:10], "total": star_position}
                    )
    return "ok", 200, page_points


def fetch_star_history(owner: str, repo: str, token: str | None = None) -> list[dict]:
    """Fetch a cumulative daily star timeline ({date, total}) for a repository."""
    owner, repo = validate_owner_repo(owner, repo)

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    meta, status = client._safe_get(
        f"{client.BASE}/repos/{owner}/{repo}", _star_headers(token)
    )
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

    # Skip doomed page calls when this caller already hit a stargazer restriction.
    if _is_stargazers_restricted(owner, repo, token):
        return _approximate_star_history(meta, total_stars, today_str, now)

    headers = _star_headers(token)
    per_page = client._PER_PAGE
    points: list[dict] = []
    restricted = False

    try:
        # Walk every page for small repos; sample 10 evenly spaced pages for large ones.
        small_threshold = 200
        is_small = total_stars <= small_threshold
        if is_small:
            page_size = 30
            total_pages = (total_stars + page_size - 1) // page_size
            pages_to_fetch = list(range(1, total_pages + 1))
        else:
            page_size = per_page
            max_pages = 400
            total_pages = min((total_stars + page_size - 1) // page_size, max_pages)
            sample_count = 10
            if total_pages <= sample_count:
                pages_to_fetch = list(range(1, total_pages + 1))
            else:
                pages_to_fetch = sorted(
                    {
                        1 + round(i * (total_pages - 1) / (sample_count - 1))
                        for i in range(sample_count)
                    }
                )

        if pages_to_fetch:
            # Probe page 1 synchronously so 404/403 restrictions abort in a single call.
            first_page = pages_to_fetch[0]
            first_kind, first_code, first_points = _fetch_stargazer_page(
                owner, repo, first_page, page_size, headers, is_small=is_small
            )
            if first_kind == "restricted":
                restricted = True
                _mark_stargazers_restricted(owner, repo, token)
                logger.warning(
                    "Stargazers endpoint restricted (HTTP %s) for %s/%s; "
                    "returning an approximate timeline.",
                    first_code,
                    owner,
                    repo,
                )
            else:
                points.extend(first_points)
                remaining_pages = pages_to_fetch[1:]
                if remaining_pages:
                    page_results: dict[int, tuple[str, int, list[dict]]] = {}
                    with ThreadPoolExecutor(
                        max_workers=min(5, len(remaining_pages))
                    ) as pool:
                        future_map = {
                            pool.submit(
                                contextvars.copy_context().run,
                                _fetch_stargazer_page,
                                owner,
                                repo,
                                page_num,
                                page_size,
                                headers,
                                is_small=is_small,
                            ): page_num
                            for page_num in remaining_pages
                        }
                        for fut in as_completed(future_map):
                            page_num = future_map[fut]
                            page_results[page_num] = fut.result()

                    for page_num in remaining_pages:
                        kind, code, page_points = page_results[page_num]
                        if kind == "restricted":
                            restricted = True
                            _mark_stargazers_restricted(owner, repo, token)
                            logger.warning(
                                "Stargazers endpoint restricted (HTTP %s) for %s/%s; "
                                "returning an approximate timeline.",
                                code,
                                owner,
                                repo,
                            )
                            break
                        points.extend(page_points)
    except (GitHubRateLimitError, StargazersRestrictedError, StarHistoryFetchError):
        raise
    except Exception as exc:
        logger.warning(
            "Stargazers history fetch exception for %s/%s: %s", owner, repo, exc
        )

    if restricted and not points:
        return _approximate_star_history(meta, total_stars, today_str, now)

    # Fall back to recent WatchEvents when stargazer pages returned no dated points.
    if not points:
        try:
            r_ev = client._http_get(
                f"{client.BASE}/repos/{owner}/{repo}/events",
                headers=headers,
                timeout=10,
            )
            if r_ev.status_code == 200 and isinstance(r_ev.json(), list):
                watch_dates = sorted(
                    [
                        str(e.get("created_at"))[:10]
                        for e in r_ev.json()
                        if isinstance(e, dict)
                        and e.get("type") == "WatchEvent"
                        and e.get("created_at")
                    ]
                )
                if watch_dates:
                    first_total = max(1, total_stars - len(watch_dates) + 1)
                    for i, d in enumerate(watch_dates):
                        points.append({"date": d, "total": first_total + i})
        except Exception as ev_exc:
            logger.warning(f"Events fallback failed for {owner}/{repo}: {ev_exc}")

    if not points:
        return [{"date": today_str, "total": total_stars}]

    # Anchor start at repo creation (or 30 days back) and keep totals monotonically increasing.
    per_day_max: dict[str, int] = {}
    for p in points:
        d = p["date"]
        per_day_max[d] = max(per_day_max.get(d, 0), int(p["total"]))
    if not per_day_max or max(per_day_max.values()) < total_stars:
        per_day_max[today_str] = total_stars

    created_at_raw = meta.get("created_at") if isinstance(meta, dict) else None
    candidate_start = str(created_at_raw)[:10] if created_at_raw else ""
    if (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate_start)
        and candidate_start < today_str
    ):
        start_date = candidate_start
    else:
        start_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    if start_date not in per_day_max:
        per_day_max[start_date] = 0

    cumulative = 0
    timeline: list[dict] = []
    for d in sorted(per_day_max.keys()):
        cumulative = max(cumulative, per_day_max[d])
        timeline.append({"date": d, "total": cumulative})
    if timeline and timeline[-1]["date"] != today_str:
        timeline.append({"date": today_str, "total": max(cumulative, total_stars)})
    if timeline and timeline[-1]["total"] < total_stars:
        timeline[-1] = {"date": today_str, "total": total_stars}

    return timeline
