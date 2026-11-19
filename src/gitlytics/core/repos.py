# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Authenticated repository listing, lookup, and deep metadata fetchers."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from gitlytics.exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    TokenExpiredError,
)
from . import client
from .validation import validate_owner_repo

logger = logging.getLogger(__name__)
_MAX_REPO_PAGES = 100


def _validate_full_name(full_name: str) -> str:
    """Validate and normalize an 'owner/repo' string before URL interpolation."""
    owner, sep, repo = (full_name or "").strip().partition("/")
    if not sep:
        raise ValueError(f"Invalid repository name {full_name!r}: expected 'owner/repo'.")
    owner, repo = validate_owner_repo(owner, repo)
    return f"{owner}/{repo}"


def get_deep_repo_stats(token: str, full_name: str) -> dict:
    """Fetch commits, open PRs, releases, and community health for one repository."""
    full_name = _validate_full_name(full_name)
    headers = client.make_headers(token)
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

    # Try weekly commit stats first; fall back to a 1-call Link-header count if computing (202).
    ca_data, ca_status = client._safe_get(f"{client.BASE}/repos/{full_name}/stats/commit_activity", headers)
    if ca_status == 202:
        logger.info("Commit activity computing (202) for %s - will populate on next fetch.", full_name)
    if isinstance(ca_data, list) and ca_data:
        stats["total_commits"] = sum(week.get("total", 0) for week in ca_data)
    else:
        try:
            stats["total_commits"] = client._count_via_link_header(
                f"{client.BASE}/repos/{full_name}/commits", headers
            )
        except Exception as exc:
            logger.warning("Could not fetch commit count for %s: %s", full_name, exc)

    try:
        stats["open_prs"] = client._count_via_link_header(
            f"{client.BASE}/repos/{full_name}/pulls", headers, {"state": "open"}
        )
    except Exception as exc:
        logger.warning("Could not fetch open PRs for %s: %s", full_name, exc)

    cp_data, _ = client._safe_get(f"{client.BASE}/repos/{full_name}/community/profile", headers)
    if isinstance(cp_data, dict) and "files" in cp_data:
        files = cp_data["files"]
        stats["has_readme"] = bool(files.get("readme"))
        stats["has_license"] = bool(files.get("license"))
        stats["has_contributing"] = bool(files.get("contributing"))
        stats["has_code_of_conduct"] = bool(files.get("code_of_conduct"))

    # Fetch release count and latest release timestamp in a single per_page=1 call.
    try:
        rel_url = f"{client.BASE}/repos/{full_name}/releases"
        r = client._http_get(rel_url, headers=headers, params={"per_page": 1}, timeout=10)
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
    """Run get_deep_repo_stats concurrently for the top N repositories by views."""
    ranked = sorted(repos_with_views, key=lambda x: x.get("total_views", 0), reverse=True)
    top = ranked[:top_n]
    results = {}

    def fetch_one(full_name: str):
        return full_name, get_deep_repo_stats(token, full_name)

    # Cap concurrency at 5 workers to stay safely under GitHub's abuse rate limit.
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
    """Paginate through every repository accessible to the authenticated token."""
    headers = client.make_headers(token)
    repos, page = [], 1
    seen = set()
    while page <= _MAX_REPO_PAGES:
        data, status = client._safe_get(
            f"{client.BASE}/user/repos",
            headers,
            {"per_page": client._PER_PAGE, "page": page, "type": "all"},
        )
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
        if len(data) < client._PER_PAGE:
            break
        page += 1
    return repos


def get_single_repo(token: str, full_name: str) -> dict:
    """Fetch metadata for one owner/repo, failing loudly on auth or rate limits."""
    clean_name = _validate_full_name(full_name)
    headers = client.make_headers(token)
    data, status = client._safe_get(f"{client.BASE}/repos/{clean_name}", headers)
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
