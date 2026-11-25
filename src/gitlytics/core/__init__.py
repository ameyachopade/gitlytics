# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Core GitHub API client and domain fetchers."""

from gitlytics.exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    StargazersRestrictedError,
    StarHistoryFetchError,
    TokenExpiredError,
)
from gitlytics.io.formatting import print_repo_table

from .client import (
    _BACKOFF_ANNOUNCE_THRESHOLD,
    _ETAG_CACHE,
    _ETAG_CACHE_LOCK,
    _ETAG_CACHE_MAXSIZE,
    _GITHUB_BASE_HEADERS,
    _PER_PAGE,
    _PUBLIC_HEADERS,
    _REQUEST_DEADLINE_SECONDS,
    _RETRY_BUDGET_SECONDS,
    _SESSION_LOCAL,
    BASE,
    _active_deadline,
    _announce_backoff,
    _cap_backoff,
    _clear_etag_cache,
    _count_via_link_header,
    _get_session,
    _http_get,
    _is_rate_limited_response,
    _operation_deadline,
    _safe_get,
    format_auth_header,
    make_headers,
)
from .repos import (
    fetch_deep_stats_for_top,
    get_all_repos,
    get_deep_repo_stats,
    get_single_repo,
)
from .stars import (
    _STARGAZER_RESTRICTED_MAXSIZE,
    _STARGAZER_RESTRICTED_TTL,
    _approximate_star_history,
    _is_stargazers_restricted,
    _mark_stargazers_restricted,
    _star_headers,
    _stargazer_cache_key,
    _stargazer_restricted_cache,
    _stargazer_restricted_lock,
    _token_fingerprint,
    fetch_star_history,
)
from .traffic import (
    build_tidy_rows,
    fetch_traffic_data,
    fetch_traffic_rows,
    get_repo_traffic,
    pad_traffic_data,
)
from .users import (
    _fetch_public_repos_by_stars,
    _fetch_public_repos_by_updated,
    _normalise_repo,
    get_public_repos,
    get_public_user,
    get_user_profile,
    validate_token,
)
from .validation import (
    GITHUB_REPO_RE,
    GITHUB_USERNAME_RE,
    validate_github_username,
    validate_owner_repo,
)

__all__ = [
    "BASE",
    "GITHUB_REPO_RE",
    "GITHUB_USERNAME_RE",
    "GitHubAPIError",
    "GitHubRateLimitError",
    "StarHistoryFetchError",
    "StargazersRestrictedError",
    "TokenExpiredError",
    "build_tidy_rows",
    "fetch_deep_stats_for_top",
    "fetch_star_history",
    "fetch_traffic_data",
    "fetch_traffic_rows",
    "format_auth_header",
    "get_all_repos",
    "get_deep_repo_stats",
    "get_public_repos",
    "get_public_user",
    "get_repo_traffic",
    "get_single_repo",
    "get_user_profile",
    "make_headers",
    "pad_traffic_data",
    "print_repo_table",
    "validate_github_username",
    "validate_owner_repo",
    "validate_token",
]
