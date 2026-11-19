# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Public API facade for the gitlytics package."""

import logging

from ._version import __version__
from .core.stars import fetch_star_history
from .exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    GitlyticsError,
    NoTrafficDataError,
    StargazersRestrictedError,
    StarHistoryFetchError,
    TokenExpiredError,
)
from .io.atomic import _write_csv_safely, _write_json_safely
from .sdk import _coerce_metrics, fetch_traffic, serve_dashboard, sync

# Attach a silent NullHandler so library logs never surprise caller applications.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__all__ = [
    "fetch_traffic",
    "fetch_star_history",
    "sync",
    "serve_dashboard",
    "__version__",
    "GitHubAPIError",
    "GitHubRateLimitError",
    "NoTrafficDataError",
    "StarHistoryFetchError",
    "StargazersRestrictedError",
    "TokenExpiredError",
]
