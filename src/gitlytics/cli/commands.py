# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Command handlers and entrypoint for the Gitlytics CLI."""

from __future__ import annotations

import argparse
import importlib
import os
import sys

from gitlytics.exceptions import (
    GitHubAPIError,
    GitHubRateLimitError,
    NoTrafficDataError,
    StargazersRestrictedError,
    TokenExpiredError,
)

from .parser import _err, _result_is_empty, build_parser, parse_repo_names

_LAZY_HANDLERS: dict[str, tuple[str, str]] = {
    "fetch_traffic": ("gitlytics.sdk", "fetch_traffic"),
    "sync": ("gitlytics.sdk", "sync"),
    "serve_dashboard": ("gitlytics.sdk", "serve_dashboard"),
    "fetch_star_history": ("gitlytics.core.stars", "fetch_star_history"),
}


def __getattr__(name: str):
    """Lazily resolve SDK/core entrypoints for monkeypatching without eager startup imports."""
    target = _LAZY_HANDLERS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _resolve_handler(name: str):
    """Return a test-patched handler if present in module globals, else import lazily."""
    patched = globals().get(name)
    if patched is not None:
        return patched
    module_name, attr_name = _LAZY_HANDLERS[name]
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)

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
        fetch_fn = _resolve_handler("fetch_traffic")
        result = fetch_fn(
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
        sync_fn = _resolve_handler("sync")
        rows = sync_fn(
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
        serve_fn = _resolve_handler("serve_dashboard")
        serve_fn(
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
        stars_fn = _resolve_handler("fetch_star_history")
        points = stars_fn(owner, repo, token)
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


def _cmd_analyze(args) -> None:
    """Execute the `gitlytics analyze [path]` offline pure-Git analyzer."""
    import json

    from gitlytics.core.local_git import analyze_local_repo

    try:
        report = analyze_local_repo(args.path, days=args.days)
    except (ValueError, RuntimeError) as exc:
        _err(f"Error: {exc}")
        sys.exit(2)

    if getattr(args, "as_json", False):
        print(json.dumps(report, indent=2))
        return

    print(f"\nGitlytics Local Git Intelligence - {report['repo_name']} ({report['branch']})")
    print(
        f"Window: {report['days']}d | Commits: {report['total_commits']} "
        f"({report['commits_per_week']}/wk) | Authors: {report['active_authors']} | Bus Factor: {report['bus_factor']}"
    )
    if report["top_authors"]:
        print("\nTop Contributors:")
        for a in report["top_authors"][:5]:
            print(f"  - {a['author']}: {a['commits']} commits ({a['share_pct']}%)")
    if report["hotspots"]:
        print("\nTop Churn Hotspots:")
        for h in report["hotspots"][:8]:
            print(f"  - {h['file']}: {h['touches']} touches")
    print(f"\nAgent Prompt:\n{report['agent_prompt']}\n")


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
    elif args.command == "analyze":
        _cmd_analyze(args)
    elif args.command == "mcp":
        from gitlytics.mcp.server import run_stdio_server

        try:
            run_stdio_server(data_dir=args.data_dir, api_base=args.api_base)
        except ValueError as exc:
            _err(f"Error: {exc}")
            sys.exit(2)
