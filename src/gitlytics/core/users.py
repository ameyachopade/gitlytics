# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""GitHub user authentication checks and public profile/repository lookups."""

from __future__ import annotations

import logging

import requests

from . import client
from .validation import validate_github_username

logger = logging.getLogger(__name__)


def validate_token(token: str) -> tuple[bool, str]:
    """Check whether a GitHub token is valid and return (ok, login_or_reason)."""
    try:
        r = client._http_get(
            f"{client.BASE}/user", headers=client.make_headers(token), timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if not isinstance(data, dict):
                return False, "Invalid response from GitHub."
            return True, data.get("login", "")
    except requests.exceptions.ConnectionError:
        return False, "No internet connection."
    except requests.exceptions.Timeout:
        return False, "GitHub request timed out. Retrying next cycle."
    except Exception as exc:
        logger.warning("Token validation error: %s", exc)
        return False, "Authentication failed."

    if r.status_code == 401:
        return False, "Invalid token - authentication failed (401 Unauthorized)."
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
    """Return the authenticated GitHub user's profile fields."""
    try:
        r = client._http_get(
            f"{client.BASE}/user", headers=client.make_headers(token), timeout=10
        )
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


def get_public_user(username: str, token: str | None = None) -> dict:
    """Fetch a public GitHub user profile with optional token authentication."""
    username = validate_github_username(username)
    headers = client.make_headers(token) if token else client._PUBLIC_HEADERS
    try:
        r = client._http_get(
            f"{client.BASE}/users/{username}", headers=headers, timeout=10
        )
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
    except (ValueError, RuntimeError):
        raise
    except Exception as exc:
        logger.warning(f"Could not fetch public user profile: {exc}")
        raise RuntimeError("GitHub is unreachable.") from exc


def _normalise_repo(repo: dict) -> dict:
    """Extract only the repository fields the dashboard needs."""
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


def _fetch_public_repos_by_updated(
    username: str, per_page: int, token: str | None = None
) -> list:
    """Fetch a user's most recently updated public repositories."""
    headers = client.make_headers(token) if token else client._PUBLIC_HEADERS
    try:
        r = client._http_get(
            f"{client.BASE}/users/{username}/repos",
            headers=headers,
            params={
                "per_page": per_page,
                "page": 1,
                "sort": "updated",
                "type": "public",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        batch = r.json()
        return (
            [_normalise_repo(repo) for repo in batch if not repo.get("private", False)]
            if isinstance(batch, list)
            else []
        )
    except Exception as exc:
        logger.warning(f"Could not fetch recent repos for {username}: {exc}")
        return []


def _fetch_public_repos_by_stars(
    username: str, per_page: int, token: str | None = None
) -> list:
    """Fetch a user's top-starred public repositories via the Search API."""
    headers = client.make_headers(token) if token else client._PUBLIC_HEADERS
    try:
        r = client._http_get(
            f"{client.BASE}/search/repositories",
            headers=headers,
            params={
                "q": f"user:{username} is:public",
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, dict) or "items" not in data:
            return []
        return [
            _normalise_repo(repo)
            for repo in data["items"]
            if not repo.get("private", False)
        ]
    except Exception as exc:
        logger.warning(f"Could not fetch starred repos for {username}: {exc}")
        return []


def get_public_repos(
    username: str, max_repos: int = 50, token: str | None = None
) -> list:
    """Merge a user's recently updated and most-starred public repos, sorted by stars."""
    username = validate_github_username(username)
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
