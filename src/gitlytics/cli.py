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

# Import the three public functions that power every subcommand
from gitlytics import fetch_traffic, sync, serve_dashboard
from gitlytics.core import fetch_star_history, StargazersRestrictedError


def parse_repo_names(repo_arg: str):
    # Turn "owner/repo1, owner/repo2" into ["owner/repo1", "owner/repo2"], or None if empty
    if not repo_arg:
        return None
    names = [r.strip() for r in repo_arg.split(",")]
    # Validate the format up front so the user gets a clear error instead of a 404.
    for name in names:
        if "/" not in name:
            raise argparse.ArgumentTypeError(
                f"Invalid repo name {name!r}: expected 'owner/repo' format."
            )
    return names


def main():
    parser = argparse.ArgumentParser(description="Gitlytics - Monitor and Automate your GitHub Traffic")
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
    fetch_parser.add_argument("-m", "--metrics", nargs="+", help="Specific metrics to fetch (e.g. views clones).")

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
    sync_parser.add_argument("-m", "--metrics", nargs="+", help="Specific metrics to sync (e.g. views clones).")

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

    args = parser.parse_args()

    # Print help and exit cleanly (code 0) if the user didn't give a subcommand.
    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Resolve token: CLI flag wins, then environment variables
    raw_token = getattr(args, "token", None)
    token = (raw_token or os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip() or None

    # fetch and sync both need a token — bail early with a clear message
    if args.command in ["fetch", "sync"] and not token:
        print("❌ Error: No GitHub token provided. Use --token or set GITLYTICS_TOKEN.")
        sys.exit(1)

    if args.command == "fetch":
        try:
            repos = parse_repo_names(args.repo_name)
        except argparse.ArgumentTypeError as exc:
            print(f"❌ Error: {exc}")
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
        except ValueError as exc:
            print(f"❌ Error: {exc}")
            sys.exit(2)
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
            print(f"❌ Error: {exc}")
            sys.exit(2)
        try:
            sync(
                token=token,
                repo_name=repos,
                data_dir=args.data_dir,
                output_mode=args.output_mode,
                schedule_cron=args.schedule_cron,
                export_json=args.export_json,
                export_public_only=args.export_public_only,
                metrics=args.metrics
            )
        except ValueError as exc:
            print(f"❌ Error: {exc}")
            sys.exit(2)

    elif args.command == "dashboard":
        host = args.host
        if host == "0.0.0.0" and not args.public_bind:
            print("❌ Error: Use --public-bind to listen on 0.0.0.0 (default is 127.0.0.1).")
            sys.exit(2)
        if args.public_bind:
            host = "0.0.0.0"
            print("⚠️  Warning: dashboard bound to all interfaces — use only behind a trusted network or reverse proxy.")
        print(f"\n[Gitlytics] Starting Gitlytics Dashboard on http://{host}:{args.port}\n")
        serve_dashboard(
            host=host,
            port=args.port,
            token=token,
            data_dir=args.data_dir
        )

    elif args.command == "stars":
        owner, _, repo = args.repo.partition("/")
        if not owner or not repo:
            print("❌ Error: repo must be in 'owner/repo' form.")
            sys.exit(2)
        try:
            points = fetch_star_history(owner, repo, token)
        except ValueError as exc:
            # Bad input — non-zero exit so scripts can branch on it.
            print(f"❌ Error: {exc}")
            sys.exit(2)
        except StargazersRestrictedError as exc:
            print(f"❌ Restriction Error: {exc}")
            sys.exit(1)
        except Exception as exc:
            # Anything else (rate limit, network, etc.) — exit 1 so CI knows it failed.
            print(f"❌ Error: {exc}")
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
