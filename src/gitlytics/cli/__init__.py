# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Command-line interface subpackage for Gitlytics."""

from .commands import _cmd_dashboard, _cmd_fetch, _cmd_stars, _cmd_sync, main
from .parser import _err, _result_is_empty, build_parser, parse_repo_names

__all__ = [
    "build_parser",
    "main",
    "parse_repo_names",
]
