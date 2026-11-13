"""
gitlytics/cli.py
Command line interface for Gitlytics.
Achieves 100% feature parity with the Python API.
"""
import argparse
import sys
import os

# Ensure Unicode output works seamlessly on Windows consoles (cp1252 / cmd / PowerShell)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import the public functions that power every subcommand
from gitlytics import (
    fetch_traffic,
    sync,
    serve_dashboard,
    GitHubAPIError,
    NoTrafficDataError,
    TokenExpiredError,
    GitHubRateLimitError,
    __version__,
)
from gitlytics.core import (
    fetch_star_history,
    GitHubAPIError,
    StargazersRestrictedError,
    validate_owner_repo,
)


def _err(message: str) -> None:
    """Print a diagnostic to stderr.

    Diagnostics (errors and warnings) must never be interleaved with the
    machine-readable payload on stdout — a caller redirecting stdout to a
    file must still be able to see the failure (AF2-LIB-09).
    """
    print(message, file=sys.stderr)


def _result_is_empty(result) -> bool:
    """True when a fetch returned no rows for an explicit repository list."""
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
    # Turn "owner/repo1, owner/repo2" into ["owner/repo1", "owner/repo2"], or None if empty
    if not repo_arg:
        return None
    names = [r.strip() for r in repo_arg.split(",")]
    # Validate the format up front so the user gets a clear error instead of a 404.
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
    # prog is pinned so `python -m gitlytics --help` prints "gitlytics"
    # instead of "__main__.py" (AF2-LIB-10).
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

    # ── FETCH subcommand ──────────────────────────────────────────────────────
    fetch_parser = subparsers.add_parser("fetch", help="Fetch traffic for one or all repositories.")
    fetch_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")
    fetch_parser.add_argument("--repo-name", help="Specific repository or comma-separated list.")
    fetch_parser.add_argument("--print-table", action="store_true", help="Print the ASCII traffic table.")
    fetch_parser.add_argument(
        "--return-format",
        choices=["dataframe", "timeseries", "summary"],
        default="dataframe",
        help="Data shape to return.",
    )
    fetch_parser.add_argument("--save-file", help="Path to save the output (.csv or .json).")
    fetch_parser.add_argument("-m", "--metrics", nargs="*", default=None, help="Specific metrics to fetch (e.g. views clones).")

    # ── SYNC subcommand ───────────────────────────────────────────────────────
    sync_parser = subparsers.add_parser("sync", help="Append traffic data to a local CSV database.")
    sync_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")
    sync_parser.add_argument("--repo-name", help="Specific repository or comma-separated list.")
    sync_parser.add_argument("--data-dir", default="./data", help="Directory to store CSVs.")
    sync_parser.add_argument(
        "--output-mode",
        choices=["monthly", "yearly"],
        default="monthly",
        help="Chunking strategy for CSV files.",
    )
    sync_parser.add_argument("--schedule-cron", help="Cron expression for background daemon mode.")
    sync_parser.add_argument("--export-json", help="Path to export the merged historical database as JSON.")
    # store_false so the flag is --no-export-public-only (disables the default True)
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
    sync_parser.add_argument("-m", "--metrics", nargs="*", default=None, help="Specific metrics to sync (e.g. views clones).")
    sync_parser.add_argument(
        "--public-only",
        action="store_true",
        default=False,
        help=(
            "When syncing all repositories, skip private ones so their names "
            "and traffic never enter the CSV vault (AF2-ACT-08)."
        ),
    )

    # ── DASHBOARD subcommand ──────────────────────────────────────────────────
    dash_parser = subparsers.add_parser("dashboard", help="Serve the local React dashboard.")
    dash_parser.add_argument("--host", default="127.0.0.1", help="Host IP (default 127.0.0.1).")
    dash_parser.add_argument(
        "--public-bind",
        action="store_true",
        help="Allow binding to 0.0.0.0 (insecure without reverse-proxy auth).",
    )
    dash_parser.add_argument("--port", type=int, default=8000, help="Port to bind.")
    dash_parser.add_argument("-t", "--token", help="GitHub token for headless TV display.")
    dash_parser.add_argument("--data-dir", help="Inject historical CSV database.")

    # ── STARS subcommand ───────────────────────────────────────────────────────
    stars_parser = subparsers.add_parser("stars", help="Fetch sampled star history for a repo (Rarely used/Deprecated by GitHub API).")
    stars_parser.add_argument("repo", help="Repository in 'owner/repo' form (e.g. ameyachopade/gitlytics).")
    stars_parser.add_argument("-t", "--token", help="GitHub Personal Access Token.")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Print help and exit cleanly (code 0) if the user didn't give a subcommand.
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Resolve token: CLI flag wins, then environment variables
    raw_token = getattr(args, "token", None)
    token = (raw_token or os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip() or None

    # fetch and sync both need a token — bail early with a clear message
    if args.command in ["fetch", "sync"] and not token:
        _err("❌ Error: No GitHub token provided. Use --token or set GITLYTICS_TOKEN.")
        sys.exit(1)

    if args.command == "fetch":
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
                metrics=args.metrics
            )
        except (TokenExpiredError, GitHubRateLimitError, GitHubAPIError) as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        except ValueError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(2)
        # A pinned repo list that returns nothing means the token is dead,
        # under-scoped, or the repos vanished — never report that as success.
        if args.repo_name and _result_is_empty(result):
            _err("❌ Error: no traffic data returned for the requested repository list.")
            sys.exit(1)
        # Give the user a hint if they didn't ask for any output
        if not args.print_table and not args.save_file:
            try:
                import pandas as pd
                if isinstance(result, pd.DataFrame):
                    print(f"Fetch successful. {len(result)} row(s) across {result['repository'].nunique() if not result.empty else 0} repo(s).")
                    print("First 5 rows:")
                    print(result.head().to_string(index=False))
                else:
                    print(f"Fetch successful. Use --print-table or --save-file to see results.")
            except Exception:
                print("Fetch successful. Use --print-table or --save-file to see results.")

    elif args.command == "sync":
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
        except TokenExpiredError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        except GitHubRateLimitError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        except GitHubAPIError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        except NoTrafficDataError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        except ValueError as exc:
            _err(f"❌ Error: {exc}")
            sys.exit(2)
        # A sync cycle that fetched zero rows produced nothing at all. Report
        # it as a failure instead of a silent success (AF2-LIB-04).
        if isinstance(rows, int) and rows == 0:
            _err("❌ Error: no traffic data returned; nothing was synced.")
            sys.exit(1)
        if isinstance(rows, int):
            print(f"Sync complete. {rows} row(s) synced.")

    elif args.command == "dashboard":
        host = args.host
        if host == "0.0.0.0" and not args.public_bind:
            _err("❌ Error: Use --public-bind to listen on 0.0.0.0 (default is 127.0.0.1).")
            sys.exit(2)
        if args.public_bind:
            host = "0.0.0.0"
            _err("⚠️  Warning: dashboard bound to all interfaces — use only behind a trusted network or reverse proxy.")
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

    elif args.command == "stars":
        owner, _, repo = args.repo.partition("/")
        if not owner or not repo:
            _err("❌ Error: repo must be in 'owner/repo' form.")
            sys.exit(2)
        try:
            points = fetch_star_history(owner, repo, token)
        except ValueError as exc:
            # Bad input — non-zero exit so scripts can branch on it.
            _err(f"❌ Error: {exc}")
            sys.exit(2)
        except StargazersRestrictedError as exc:
            _err(f"❌ Restriction Error: {exc}")
            sys.exit(1)
        except Exception as exc:
            # Anything else (rate limit, network, etc.) — exit 1 so CI knows it failed.
            _err(f"❌ Error: {exc}")
            sys.exit(1)
        # Tabulate date + total stars.
        print(f"{'DATE':<12} {'TOTAL STARS':>12}")
        print("-" * 24)
        for p in points:
            print(f"{p['date']:<12} {p['total']:>12,}")
        print("-" * 24)
        print(f"{len(points)} sampled point(s)")


if __name__ == "__main__":
    main()
