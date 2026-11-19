# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Validation helpers for GitHub usernames and owner/repo identifiers."""
from __future__ import annotations

import re

GITHUB_USERNAME_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$",
    re.IGNORECASE,
)
GITHUB_REPO_RE = re.compile(r"^[a-zA-Z0-9._-]{1,100}$")


def validate_owner_repo(owner: str, repo: str) -> tuple[str, str]:
    """Validate and clean a GitHub owner/repo pair, rejecting path traversal."""
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


def validate_github_username(username: str) -> str:
    """Validate and strip a GitHub username."""
    cleaned = (username or "").strip()
    if not cleaned or not GITHUB_USERNAME_RE.match(cleaned):
        raise ValueError("Invalid GitHub username.")
    return cleaned
