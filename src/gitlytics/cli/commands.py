# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Command handlers and entrypoint for the Gitlytics CLI."""

from __future__ import annotations

import argparse
import os
import sys

from gitlytics.core.stars import fetch_star_history
from gitlytics.exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    NoTrafficDataError,
    StargazersRestrictedError,
    TokenExpiredError,
)
from gitlytics.sdk import fetch_traffic, serve_dashboard, sync

from .parser import _err, _result_is_empty, build_parser, parse_repo_names

# Ensure Unicode symbols render cleanly on Windows consoles (cp1252 / cmd / PowerShell).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _cmd_fetch(args, token: str) -> None:
    """Execute the `gitlytics fetch` subcommand."""
    try:
        repos = parse_repo_names(args.repo_name)
    except argparse.ArgumentTypeError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(2)

    try:
        result = fetch_traffic(
            token=token,
            repo_name=repos,
            print_table=args.print_table,
            return_format=args.return_format,
            save_file=args.save_file,
            metrics=args.metrics,
        )
    except (TokenExpiredError, GitHubRateLimitError, GitHubAPIError) as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(2)

    # An explicit repo list returning no rows signals a bad token, scope, or repo name.
    if args.repo_name and _result_is_empty(result):
        _err("❌ Error: no traffic data returned for the requested repository list.")
        sys.exit(1)

    if not args.print_table and not args.save_file:
        try:
            import pandas as pd

            if isinstance(result, pd.DataFrame):
                repo_count = result["repository"].nunique() if not result.empty else 0
                print(
                    f"Fetch successful. {len(result)} row(s) across {repo_count} repo(s)."
                )
                print("First 5 rows:")
                print(result.head().to_string(index=False))
            else:
                print(
                    "Fetch successful. Use --print-table or --save-file to see results."
                )
        except Exception:
            print("Fetch successful. Use --print-table or --save-file to see results.")


def _cmd_sync(args, token: str) -> None:
    """Execute the `gitlytics sync` subcommand."""
    try:
        repos = parse_repo_names(args.repo_name)
    except argparse.ArgumentTypeError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(2)

    try:
        rows = sync(
            token=token,
            repo_name=repos,
            data_dir=args.data_dir,
            output_mode=args.output_mode,
            schedule_cron=args.schedule_cron,
            export_json=args.export_json,
            export_public_only=args.export_public_only,
            metrics=args.metrics,
            public_only=args.public_only,
        )
    except (
        TokenExpiredError,
        GitHubRateLimitError,
        GitHubAPIError,
        NoTrafficDataError,
    ) as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(1)
    except ValueError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(2)

    # Report zero synced rows as a failure so CI schedules never silently pass.
    if isinstance(rows, int) and rows == 0:
        _err("❌ Error: no traffic data returned; nothing was synced.")
        sys.exit(1)
    if isinstance(rows, int):
        print(f"Sync complete. {rows} row(s) synced.")


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _cmd_dashboard(args, token: str | None) -> None:
    """Execute the `gitlytics dashboard` subcommand."""
    host = args.host
    is_loopback = host in _LOOPBACK_HOSTS
    if not is_loopback and not args.public_bind:
        _err(
            "❌ Error: Use --public-bind to listen on a non-loopback address (default is 127.0.0.1)."
        )
        sys.exit(2)
    if args.public_bind:
        if host == "127.0.0.1":
            host = "0.0.0.0"
        _err(
            f"⚠️  Warning: dashboard bound to non-loopback address {host} - "
            "use only behind a trusted network or reverse proxy."
        )

    print(f"\n[Gitlytics] Starting Gitlytics Dashboard on http://{host}:{args.port}\n")
    try:
        serve_dashboard(
            host=host,
            port=args.port,
            token=token,
            data_dir=args.data_dir,
            stream=sys.stderr,
        )
    except ImportError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(1)


def _cmd_stars(args, token: str | None) -> None:
    """Execute the `gitlytics stars` subcommand."""
    owner, _, repo = args.repo.partition("/")
    if not owner or not repo:
        _err("❌ Error: repo must be in 'owner/repo' form.")
        sys.exit(2)

    try:
        points = fetch_star_history(owner, repo, token)
    except ValueError as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(2)
    except StargazersRestrictedError as exc:
        _err(f"❌ Restriction Error: {exc}")
        sys.exit(1)
    except Exception as exc:
        _err(f"❌ Error: {exc}")
        sys.exit(1)

    print(f"{'DATE':<12} {'TOTAL STARS':>12}")
    print("-" * 24)
    for point in points:
        print(f"{point['date']:<12} {point['total']:>12,}")
    print("-" * 24)
    print(f"{len(points)} sampled point(s)")


def main(argv=None) -> None:
    """Parse CLI arguments and dispatch to the matching subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Exit cleanly after showing help when invoked with no subcommand.
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # CLI --token flag takes priority over GITLYTICS_TOKEN and GITHUB_TOKEN.
    raw_token = getattr(args, "token", None)
    token = (
        raw_token
        or os.environ.get("GITLYTICS_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip() or None

    if args.command in ("fetch", "sync") and not token:
        _err("❌ Error: No GitHub token provided. Use --token or set GITLYTICS_TOKEN.")
        sys.exit(1)

    if args.command == "fetch":
        _cmd_fetch(args, token)
    elif args.command == "sync":
        _cmd_sync(args, token)
    elif args.command == "dashboard":
        # Pass only an explicit --token flag so ambient GITHUB_TOKEN is not silently promoted.
        explicit_token = (raw_token or "").strip() or None
        _cmd_dashboard(args, explicit_token)
    elif args.command == "stars":
        _cmd_stars(args, token)
