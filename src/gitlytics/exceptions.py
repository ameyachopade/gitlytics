# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Public exception hierarchy for gitlytics."""


class GitlyticsError(Exception):
    """Base exception for all gitlytics errors."""


class GitHubRateLimitError(GitlyticsError):
    """Raised when GitHub responds with a rate limit or abuse guard (HTTP 403/429)."""


class GitHubAPIError(GitlyticsError, RuntimeError):
    """Raised when GitHub returns an unexpected upstream failure or bad payload."""


class StarHistoryFetchError(GitlyticsError):
    """Raised for non-rate-limit star-history failures (network, parse, or bad repo)."""


class StargazersRestrictedError(GitlyticsError):
    """Raised when GitHub restricts the /stargazers endpoint for a repository."""


class TokenExpiredError(GitlyticsError):
    """Raised when the GitHub token is invalid, expired, revoked, or under-scoped."""


class NoTrafficDataError(GitlyticsError, RuntimeError):
    """Raised when a sync cycle returns zero traffic rows from GitHub."""


__all__ = [
    "GitlyticsError",
    "GitHubAPIError",
    "GitHubRateLimitError",
    "NoTrafficDataError",
    "StarHistoryFetchError",
    "StargazersRestrictedError",
    "TokenExpiredError",
]
