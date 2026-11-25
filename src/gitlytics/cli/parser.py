# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Argument parser and small CLI formatting helpers for Gitlytics."""

from __future__ import annotations

import argparse
import sys

from gitlytics._version import __version__


def _err(message: str) -> None:
    """Write diagnostics to stderr so stdout stays safe for piped payloads."""
    print(message, file=sys.stderr)


def _result_is_empty(result) -> bool:
    """Return True when a fetch yielded no rows for an explicit repo list."""
    try:
        import pandas as pd

        if isinstance(result, pd.DataFrame):
            return result.empty
    except Exception:
        pass
    if isinstance(result, dict):
        return not result.get("repositories")
    return False


def parse_repo_names(repo_arg: str):
    """Parse a comma-separated 'owner/repo' string into validated repo names."""
    if not repo_arg:
        return None
    from gitlytics.core.validation import validate_owner_repo

    names = [r.strip() for r in repo_arg.split(",")]
    cleaned = []
    for name in names:
        if "/" not in name or name.count("/") != 1:
            raise argparse.ArgumentTypeError(
                f"Invalid repo name {name!r}: expected 'owner/repo' format."
            )
        owner, _, repo = name.partition("/")
        try:
            clean_owner, clean_repo = validate_owner_repo(owner, repo)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
        cleaned.append(f"{clean_owner}/{clean_repo}")
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level CLI parser with fetch, sync, dashboard, and stars commands."""
    # Pin prog so `python -m gitlytics --help` prints "gitlytics" instead of "__main__.py".
    parser = argparse.ArgumentParser(
        prog="gitlytics",
        description="Gitlytics - Monitor and Automate your GitHub Traffic",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"gitlytics {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Fetch subcommand for ad-hoc traffic queries.
    fetch_parser = subparsers.add_parser(
        "fetch", help="Fetch traffic for one or all repositories."
    )
    fetch_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")
    fetch_parser.add_argument(
        "--repo-name", help="Specific repository or comma-separated list."
    )
    fetch_parser.add_argument(
        "--print-table", action="store_true", help="Print the ASCII traffic table."
    )
    fetch_parser.add_argument(
        "--return-format",
        choices=["dataframe", "timeseries", "summary"],
        default="dataframe",
        help="Data shape to return.",
    )
    fetch_parser.add_argument(
        "--save-file", help="Path to save the output (.csv or .json)."
    )
    fetch_parser.add_argument(
        "-m",
        "--metrics",
        nargs="*",
        default=None,
        help="Specific metrics to fetch (e.g. views clones).",
    )

    # Sync subcommand for incremental CSV archival and optional cron scheduling.
    sync_parser = subparsers.add_parser(
        "sync", help="Append traffic data to a local CSV database."
    )
    sync_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")
    sync_parser.add_argument(
        "--repo-name", help="Specific repository or comma-separated list."
    )
    sync_parser.add_argument(
        "--data-dir", default="./data", help="Directory to store CSVs."
    )
    sync_parser.add_argument(
        "--output-mode",
        choices=["monthly", "yearly"],
        default="monthly",
        help="Chunking strategy for CSV files.",
    )
    sync_parser.add_argument(
        "--schedule-cron", help="Cron expression for background daemon mode."
    )
    sync_parser.add_argument(
        "--export-json", help="Path to export the merged historical database as JSON."
    )
    # Private repos are stripped from exported JSON by default unless explicitly opted in.
    sync_parser.add_argument(
        "--no-export-public-only",
        dest="export_public_only",
        action="store_false",
        default=True,
        help=(
            "Include private repositories in the exported JSON. "
            "By default they are stripped for security."
        ),
    )
    sync_parser.add_argument(
        "-m",
        "--metrics",
        nargs="*",
        default=None,
        help="Specific metrics to sync (e.g. views clones).",
    )
    sync_parser.add_argument(
        "--public-only",
        action="store_true",
        default=False,
        help=(
            "When syncing all repositories, skip private ones so their names "
            "and traffic never enter the CSV vault."
        ),
    )

    # Dashboard subcommand for serving the bundled local React UI.
    dash_parser = subparsers.add_parser(
        "dashboard", help="Serve the local React dashboard."
    )
    dash_parser.add_argument(
        "--host", default="127.0.0.1", help="Host IP (default 127.0.0.1)."
    )
    dash_parser.add_argument(
        "--public-bind",
        action="store_true",
        help="Allow binding to 0.0.0.0 (insecure without reverse-proxy auth).",
    )
    dash_parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    dash_parser.add_argument(
        "-t", "--token", help="GitHub token for headless TV display."
    )
    dash_parser.add_argument("--data-dir", help="Inject historical CSV database.")

    # Stars subcommand for sampled cumulative star history.
    stars_parser = subparsers.add_parser(
        "stars",
        help="Fetch sampled star history for a repo (Rarely used/Deprecated by GitHub API).",
    )
    stars_parser.add_argument(
        "repo", help="Repository in 'owner/repo' form (e.g. ameyachopade/gitlytics)."
    )
    stars_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")

    # Local stdio MCP server subcommand (privacy-first offline CSV vault + SDK).
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Start the local Gitlytics MCP server over stdio.",
    )
    mcp_parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory containing local traffic_*.csv archives (default ./data).",
    )
    mcp_parser.add_argument(
        "--api-base",
        default="https://api.gitlytics.dev",
        help="Hosted Gitlytics API base URL for widget embed generation.",
    )

    return parser
