# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Public API facade for the gitlytics package."""

import importlib
import logging

from ._version import __version__
from .exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    GitlyticsError,
    NoTrafficDataError,
    StargazersRestrictedError,
    StarHistoryFetchError,
    TokenExpiredError,
)

# Attach a silent NullHandler so library logs never surprise caller applications.
logging.getLogger(__name__).addHandler(logging.NullHandler())

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "fetch_traffic": (".sdk", "fetch_traffic"),
    "sync": (".sdk", "sync"),
    "serve_dashboard": (".sdk", "serve_dashboard"),
    "_coerce_metrics": (".sdk", "_coerce_metrics"),
    "fetch_star_history": (".core.stars", "fetch_star_history"),
    "_write_csv_safely": (".io.atomic", "_write_csv_safely"),
    "_write_json_safely": (".io.atomic", "_write_json_safely"),
}

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


def __getattr__(name: str):
    """Lazily resolve heavy SDK, star-history, and I/O helpers on first access."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_path, attr_name = target
    module = importlib.import_module(module_path, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose both eager and lazily resolved package exports to dir()."""
    return sorted(set(globals()) | set(__all__) | set(_LAZY_EXPORTS))
