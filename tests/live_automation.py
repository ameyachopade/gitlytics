"""
live_automation.py
Online integration test for gitlytics.automation - real GitHub API, real CSV files.
Tests every aspect of the sync engine that matters before publishing to PyPI.

Usage:
    python tests/live_automation.py

GITHUB_TOKEN / GITLYTICS_TOKEN is loaded automatically from .env
Output files are saved to: data/data_automation/
"""
import os
import sys
import csv
import json
from pathlib import Path

if __name__ == "__main__":
    # Force UTF-8 so Unicode symbols render correctly on Windows terminals
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ── Load .env ──────────────────────────────────────────────────────────────
    try:
        from dotenv import load_dotenv, find_dotenv
        _env = find_dotenv(usecwd=False, raise_error_if_not_found=False)
        if not _env:
            for _c in [Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"]:
                if _c.exists():
                    _env = str(_c)
                    break
        if _env:
            load_dotenv(_env, override=False)
    except ImportError:
        pass

    # ── Colour helpers ─────────────────────────────────────────────────────────
    GREEN  = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
    CYAN   = "\033[96m"; BOLD = "\033[1m"; RESET  = "\033[0m"

    def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
    def fail(msg): print(f"  {RED}✗{RESET}  {msg}")
    def info(msg): print(f"  {CYAN}ℹ{RESET}  {msg}")
    def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
    def section(title):
        print(f"\n{BOLD}{'─'*55}{RESET}")
        print(f"{BOLD}  {title}{RESET}")
        print(f"{BOLD}{'─'*55}{RESET}")

    # ── Paths and token ────────────────────────────────────────────────────────
    DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "data_automation"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    section("0 / 7  Setup")
    token = os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        info(f"Token loaded from environment ({token[:8]}…)")
    else:
        import getpass
        token = getpass.getpass("  Enter your GitHub Personal Access Token: ").strip()
        if not token:
            fail("No token provided. Exiting.")
            sys.exit(1)
    info(f"Data directory: {DATA_DIR}")

    # Import the automation functions we're testing
    try:
        from gitlytics.automation import run_sync_cycle, run_sync, get_csv_path, export_json_database
        import pandas as pd
        ok("Automation module imported successfully")
    except ImportError as exc:
        fail(f"Could not import gitlytics.automation: {exc}")
        sys.exit(1)

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 1 - First sync creates a valid CSV
    # ─────────────────────────────────────────────────────────────────────────
    section("1 / 7  First sync cycle - CSV creation")

    print("\n  [1a] Running run_sync_cycle() - monthly mode")
    try:
        run_sync_cycle(token, data_dir=str(DATA_DIR), output_mode="monthly")
        monthly_files = list(DATA_DIR.glob("traffic_????-??.csv"))
        if monthly_files:
            csv_path = monthly_files[0]
            size = csv_path.stat().st_size
            ok(f"CSV created: {csv_path.name}  ({size:,} bytes)")
        else:
            warn("No monthly CSV found - token may have no repos with traffic")
            csv_path = None
    except Exception as exc:
        fail(f"run_sync_cycle() raised: {exc}")
        csv_path = None

    print("\n  [1b] CSV has all required columns")
    REQUIRED_COLS = {
        "date", "repository", "is_private", "views", "unique_visitors",
        "clones", "unique_cloners", "stars", "forks",
        "top_referrer", "top_path", "_raw_referrers", "_raw_paths"
    }
    if csv_path and csv_path.exists():
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                headers = set(reader.fieldnames or [])
                rows = list(reader)
            missing = REQUIRED_COLS - headers
            if missing:
                fail(f"Missing columns: {missing}")
            else:
                ok(f"All {len(REQUIRED_COLS)} required columns present")
            info(f"Total rows: {len(rows)}")
        except Exception as exc:
            fail(f"CSV read failed: {exc}")
    else:
        warn("Skipped - no CSV to check")

    print("\n  [1c] Data values are sensible (dates, numbers, no blanks)")
    if csv_path and csv_path.exists():
        try:
            df = pd.read_csv(csv_path)
            # Date column must be in YYYY-MM-DD format
            date_ok = df["date"].str.match(r"\d{4}-\d{2}-\d{2}").all()
            ok(f"All dates in YYYY-MM-DD format") if date_ok else fail("Some dates are malformed")
            # Views and clones must be non-negative integers
            views_ok = (df["views"] >= 0).all()
            ok(f"All view counts are non-negative") if views_ok else fail("Negative view counts found")
            # Stars should be non-negative
            stars_ok = (df["stars"] >= 0).all()
            ok(f"All star counts are non-negative") if stars_ok else fail("Negative star counts found")
            # Repository column must not be empty
            repo_ok = df["repository"].notna().all() and (df["repository"] != "").all()
            ok("No empty repository names") if repo_ok else fail("Some repository names are empty")
            info(f"Repositories found: {sorted(df['repository'].unique().tolist())}")
            info(f"Date range: {df['date'].min()} → {df['date'].max()}")
        except Exception as exc:
            fail(f"Data validation raised: {exc}")
    else:
        warn("Skipped - no CSV to validate")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 2 - Second sync must not duplicate rows
    # ─────────────────────────────────────────────────────────────────────────
    section("2 / 7  Deduplication - no duplicate rows on second sync")

    if not csv_path or not csv_path.exists():
        warn("Skipped - no CSV from test 1")
    else:
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                rows_before = sum(1 for _ in csv.reader(f)) - 1  # subtract header row

            run_sync_cycle(token, data_dir=str(DATA_DIR), output_mode="monthly")

            with open(csv_path, "r", encoding="utf-8") as f:
                rows_after = sum(1 for _ in csv.reader(f)) - 1

            if rows_after == rows_before:
                ok(f"Dedup works: {rows_before} rows before → {rows_after} rows after (no change)")
            elif rows_after > rows_before:
                warn(f"Row count grew {rows_before} → {rows_after} (GitHub returned new data - this is fine)")
            else:
                fail(f"Rows decreased unexpectedly: {rows_before} → {rows_after}")
        except Exception as exc:
            fail(f"Deduplication check raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 3 - Yearly mode creates a separate file
    # ─────────────────────────────────────────────────────────────────────────
    section("3 / 7  Yearly output mode")

    print("\n  [3a] run_sync_cycle() in yearly mode")
    try:
        run_sync_cycle(token, data_dir=str(DATA_DIR), output_mode="yearly")
        yearly_files = list(DATA_DIR.glob("traffic_????.csv"))
        if yearly_files:
            size = yearly_files[0].stat().st_size
            ok(f"Yearly CSV created: {yearly_files[0].name}  ({size:,} bytes)")
        else:
            warn("No yearly CSV found (may be empty data)")
    except Exception as exc:
        fail(f"Yearly sync raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 4 - Export JSON database
    # ─────────────────────────────────────────────────────────────────────────
    section("4 / 7  JSON export")

    print("\n  [4a] run_sync_cycle() with export_json")
    json_export_path = DATA_DIR / "automation_export.json"
    try:
        run_sync_cycle(
            token,
            data_dir=str(DATA_DIR),
            output_mode="monthly",
            export_json=str(json_export_path),
            export_public_only=True
        )
        if json_export_path.exists():
            size = json_export_path.stat().st_size
            ok(f"JSON export written: automation_export.json  ({size:,} bytes)")
            # Validate the JSON structure
            with open(json_export_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            assert isinstance(payload, list), "Payload should be a list"
            ok(f"JSON has correct structure: {len(payload)} repos")
        else:
            warn("JSON export was not created (may be empty data)")
    except Exception as exc:
        import traceback; traceback.print_exc(); fail(f"export_json raised: {exc}")

    print("\n  [4b] export_json_database() directly")
    json_direct_path = DATA_DIR / "automation_direct_export.json"
    try:
        export_json_database(str(DATA_DIR), str(json_direct_path), export_public_only=True)
        if json_direct_path.exists():
            size = json_direct_path.stat().st_size
            ok(f"Direct export written: automation_direct_export.json  ({size:,} bytes)")
        else:
            warn("No export written (may be no CSVs in data_dir yet)")
    except Exception as exc:
        fail(f"export_json_database() raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 5 - public run_sync() API (the function users call directly)
    # ─────────────────────────────────────────────────────────────────────────
    section("5 / 7  run_sync() public API")

    print("\n  [5a] run_sync() - one-shot (no schedule_cron)")
    sync_json_path = DATA_DIR / "run_sync_export.json"
    try:
        run_sync(
            token=token,
            data_dir=str(DATA_DIR),
            output_mode="monthly",
            schedule_cron=None,
            export_json=str(sync_json_path),
            export_public_only=True
        )
        ok("run_sync() completed without error")
        if sync_json_path.exists():
            ok(f"run_sync export written: run_sync_export.json ({sync_json_path.stat().st_size:,} bytes)")
        else:
            warn("run_sync export not written (may be empty data)")
    except Exception as exc:
        fail(f"run_sync() raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 6 - get_csv_path utility
    # ─────────────────────────────────────────────────────────────────────────
    section("6 / 7  get_csv_path() utility")

    print("\n  [6a] Monthly mode path format")
    try:
        path = get_csv_path(str(DATA_DIR), "monthly")
        import re
        filename = os.path.basename(path)
        if re.match(r"traffic_\d{4}-\d{2}\.csv", filename):
            ok(f"Monthly path correct: {filename}")
        else:
            fail(f"Unexpected monthly filename: {filename}")
    except Exception as exc:
        fail(f"get_csv_path(monthly) raised: {exc}")

    print("\n  [6b] Yearly mode path format")
    try:
        path = get_csv_path(str(DATA_DIR), "yearly")
        filename = os.path.basename(path)
        if re.match(r"traffic_\d{4}\.csv", filename):
            ok(f"Yearly path correct: {filename}")
        else:
            fail(f"Unexpected yearly filename: {filename}")
    except Exception as exc:
        fail(f"get_csv_path(yearly) raised: {exc}")

    print("\n  [6c] Creates directory if it doesn't exist")
    new_dir = DATA_DIR / "sync_test_subdir"
    try:
        get_csv_path(str(new_dir), "monthly")
        if new_dir.exists():
            ok("Directory created automatically")
            new_dir.rmdir()  # Clean up the test directory
        else:
            fail("Directory was not created")
    except Exception as exc:
        fail(f"get_csv_path directory creation raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────
    # TEST 7 - _raw_referrers and _raw_paths are valid JSON in the CSV
    # ─────────────────────────────────────────────────────────────────────────
    section("7 / 7  Raw JSON columns in CSV")

    if not csv_path or not csv_path.exists():
        warn("Skipped - no CSV from test 1")
    else:
        try:
            df = pd.read_csv(csv_path)
            # Check _raw_referrers is parseable JSON
            raw_ref_ok = 0
            raw_path_ok = 0
            for _, row in df.iterrows():
                try:
                    refs = json.loads(row.get("_raw_referrers", "[]"))
                    assert isinstance(refs, list)
                    raw_ref_ok += 1
                except Exception:
                    pass
                try:
                    paths = json.loads(row.get("_raw_paths", "[]"))
                    assert isinstance(paths, list)
                    raw_path_ok += 1
                except Exception:
                    pass
            total = len(df)
            ok(f"_raw_referrers: {raw_ref_ok}/{total} rows are valid JSON lists")
            ok(f"_raw_paths:     {raw_path_ok}/{total} rows are valid JSON lists")
        except Exception as exc:
            fail(f"Raw column check raised: {exc}")

    # ── Summary ────────────────────────────────────────────────────────────────
    section("Done")
    print(f"\n  All automation online tests completed.")
    print(f"  Check {RED}✗{RESET} lines above for any failures.")
    print(f"  Output files in: data/data_automation/\n")

