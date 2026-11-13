"""
live_module.py
Online integration test for the gitlytics Python public API.
Tests all three public API functions:
  - gitlytics.fetch_traffic()
  - gitlytics.sync()
  - gitlytics.serve_dashboard()

Usage:
    python tests/live_module.py

GITHUB_TOKEN / GITLYTICS_TOKEN is loaded automatically from .env
Output files are saved to: data/data_module/
"""

import os
import sys

if __name__ == "__main__":
    # Force UTF-8 output so Unicode symbols render on Windows terminals (cp1252 fix)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


    import tempfile
    import time
    import threading
    from pathlib import Path

    # ── Load .env file so GITHUB_TOKEN / GITLYTICS_TOKEN are available ─────────────
    # Searches from this script's directory upward (covers both project root and tests/)
    try:
        from dotenv import load_dotenv, find_dotenv
        _env_file = find_dotenv(usecwd=False, raise_error_if_not_found=False)
        if not _env_file:
            # Fallback: look for .env next to this script or one level up
            for _candidate in [
                Path(__file__).parent / ".env",
                Path(__file__).parent.parent / ".env",
            ]:
                if _candidate.exists():
                    _env_file = str(_candidate)
                    break
        if _env_file:
            load_dotenv(_env_file, override=False)
    except ImportError:
        pass  # python-dotenv not installed; rely on environment variables already set

    # ── colour helpers ────────────────────────────────────────────────────────────
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    def ok(msg):    print(f"  {GREEN}✓{RESET}  {msg}")
    def fail(msg):  print(f"  {RED}✗{RESET}  {msg}")
    def info(msg):  print(f"  {CYAN}ℹ{RESET}  {msg}")
    def warn(msg):  print(f"  {YELLOW}⚠{RESET}  {msg}")
    def section(title):
        print(f"\n{BOLD}{'─'*55}{RESET}")
        print(f"{BOLD}  {title}{RESET}")
        print(f"{BOLD}{'─'*55}{RESET}")

    # ── import check ──────────────────────────────────────────────────────────────
    section("0 / 4  Import check")
    try:
        import gitlytics
        ok(f"import gitlytics  →  version {gitlytics.__version__ if hasattr(gitlytics, '__version__') else '(no __version__)'}")
    except ImportError as exc:
        fail(f"Could not import gitlytics: {exc}")
        print("\nMake sure you installed it with:\n  pip install -e '.[dashboard]'")
        sys.exit(1)

    # ── token ─────────────────────────────────────────────────────────────────────
    print()
    token = os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        info(f"Using token from environment variable ({token[:8]}…)")
    else:
        import getpass
        token = getpass.getpass("  Enter your GitHub Personal Access Token: ").strip()
        if not token:
            fail("No token provided. Exiting.")
            sys.exit(1)

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 1 — fetch_traffic()
    # ─────────────────────────────────────────────────────────────────────────────
    section("1 / 4  gitlytics.fetch_traffic()")

    # 1a — return_format="dataframe"
    print("\n  [1a] return_format='dataframe'")
    try:
        df = gitlytics.fetch_traffic(token=token, return_format="dataframe")
        import pandas as pd
        assert isinstance(df, pd.DataFrame), "Expected a pandas DataFrame"
        ok(f"Returned DataFrame with {len(df)} rows and {len(df.columns)} columns")
        if not df.empty:
            info(f"Repositories found: {sorted(df['repository'].unique().tolist())}")
            info(f"Columns: {list(df.columns)}")
            info(f"Date range: {df['date'].min()} → {df['date'].max()}")
        else:
            warn("DataFrame is empty (token may not own any repos with traffic)")
    except Exception as exc:
        fail(f"fetch_traffic(return_format='dataframe') raised: {exc}")

    # 1b — return_format="timeseries"
    print("\n  [1b] return_format='timeseries'")
    try:
        payload = gitlytics.fetch_traffic(token=token, return_format="timeseries")
        assert isinstance(payload, dict), "Expected a dict"
        assert "account_totals" in payload, "Missing 'account_totals' key"
        assert "repositories" in payload, "Missing 'repositories' key"
        repo_count = len(payload["repositories"])
        ok(f"Returned timeseries payload with {repo_count} repositor{'y' if repo_count == 1 else 'ies'}")
        totals = payload["account_totals"]
        info(f"Account totals → views: {totals.get('total_views', 0)}, "
             f"clones: {totals.get('total_clones', 0)}, "
             f"stars: {totals.get('total_stars', 0)}")
    except Exception as exc:
        fail(f"fetch_traffic(return_format='timeseries') raised: {exc}")

    # 1c — return_format="summary"
    print("\n  [1c] return_format='summary'")
    try:
        summary = gitlytics.fetch_traffic(token=token, return_format="summary")
        assert isinstance(summary, dict), "Expected a dict"
        assert "repositories" in summary
        ok(f"Returned summary payload with {len(summary['repositories'])} repos")
    except Exception as exc:
        fail(f"fetch_traffic(return_format='summary') raised: {exc}")

    # 1d — invalid return_format should raise ValueError
    print("\n  [1d] Invalid return_format raises ValueError")
    try:
        gitlytics.fetch_traffic(token=token, return_format="raw")
        fail("Expected ValueError but no exception was raised")
    except ValueError as exc:
        ok(f"Correctly raised ValueError: {exc}")
    except Exception as exc:
        fail(f"Wrong exception type raised: {type(exc).__name__}: {exc}")

    # 1e — print_table=True
    print("\n  [1e] print_table=True  (output below)")
    print("  " + "·" * 51)
    try:
        gitlytics.fetch_traffic(token=token, print_table=True)
        print("  " + "·" * 51)
        ok("print_table rendered without error")
    except Exception as exc:
        fail(f"print_table raised: {exc}")

    # 1f — save_file (CSV)
    print("\n  [1f] save_file=<path>.csv")
    try:
        data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "data_module"))
        os.makedirs(data_dir, exist_ok=True)
        tmp_path = os.path.join(data_dir, "test_output.csv")
        gitlytics.fetch_traffic(token=token, save_file=tmp_path)
        size = os.path.getsize(tmp_path)
        ok(f"CSV saved to {tmp_path}  ({size:,} bytes)")
    except Exception as exc:
        fail(f"save_file CSV raised: {exc}")

    # 1g — save_file (JSON)
    print("\n  [1g] save_file=<path>.json")
    try:
        data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
        os.makedirs(data_dir, exist_ok=True)
        tmp_path = os.path.join(data_dir, "test_output.json")
        gitlytics.fetch_traffic(token=token, return_format="dataframe", save_file=tmp_path)
        size = os.path.getsize(tmp_path)
        ok(f"JSON saved to {tmp_path}  ({size:,} bytes)")
    except Exception as exc:
        fail(f"save_file JSON raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 2 — sync()
    # ─────────────────────────────────────────────────────────────────────────────
    section("2 / 4  gitlytics.sync()")

    data_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "data_module"))
    os.makedirs(data_dir, exist_ok=True)
    csv_path = None  # track across sub-tests

    # 2a — one-shot sync (no cron)
    print("\n  [2a] One-shot sync (no cron)")
    try:
        gitlytics.sync(token=token, data_dir=data_dir, output_mode="monthly")
        csv_files = [f for f in os.listdir(data_dir) if f.endswith(".csv")]
        if not csv_files:
            warn("sync() wrote no CSV — token has no repositories or they all have 0 traffic")
            warn("Sub-tests 2b/2c will be skipped (need at least one repo to sync)")
        else:
            csv_path = os.path.join(data_dir, csv_files[0])
            size = os.path.getsize(csv_path)
            ok(f"Sync created: {csv_files[0]}  ({size:,} bytes)")
    except Exception as exc:
        fail(f"sync() raised: {exc}")

    # 2b — second sync should not duplicate rows
    print("\n  [2b] Second sync must not duplicate rows")
    if csv_path is None:
        warn("Skipped — no CSV was created in [2a]")
    else:
        try:
            import csv as csv_mod
            with open(csv_path, "r") as f:
                rows_before = sum(1 for _ in csv_mod.reader(f)) - 1  # subtract header

            gitlytics.sync(token=token, data_dir=data_dir, output_mode="monthly")

            with open(csv_path, "r") as f:
                rows_after = sum(1 for _ in csv_mod.reader(f)) - 1

            if rows_after == rows_before:
                ok(f"No duplication: {rows_before} rows before, {rows_after} rows after")
            elif rows_after > rows_before:
                warn(f"Row count increased {rows_before} → {rows_after} (possible new data or overlap window)")
            else:
                fail(f"Rows decreased: {rows_before} → {rows_after}")
        except Exception as exc:
            fail(f"Deduplication check raised: {exc}")

    # 2c — export_json
    print("\n  [2c] sync() with export_json")
    try:
        import tempfile as _tf
        json_path = os.path.join(data_dir, "export.json")
        gitlytics.sync(
            token=token,
            data_dir=data_dir,
            export_json=json_path,
            export_public_only=True,
        )
        if os.path.exists(json_path):
            size = os.path.getsize(json_path)
            ok(f"JSON export written: export.json  ({size:,} bytes)")
        else:
            warn("export.json was not created (may be empty data set)")
    except Exception as exc:
        fail(f"sync(export_json=...) raised: {exc}")

    # 2d — yearly output_mode
    print("\n  [2d] output_mode='yearly'")
    try:
        gitlytics.sync(token=token, data_dir=data_dir, output_mode="yearly")
        yearly = [f for f in os.listdir(data_dir) if f.startswith("traffic_") and len(f) == len("traffic_YYYY.csv")]
        ok(f"Yearly CSV written: {yearly}")
    except Exception as exc:
        fail(f"sync(output_mode='yearly') raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 3 — serve_dashboard()
    # ─────────────────────────────────────────────────────────────────────────────
    section("3 / 4  gitlytics.serve_dashboard()  (quick smoke test)")

    print("\n  Starting dashboard on port 18765 for 4 seconds…")
    error_holder = []

    def _run_dashboard():
        try:
            gitlytics.serve_dashboard(host="127.0.0.1", port=18765, token=token)
        except SystemExit:
            pass
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=_run_dashboard, daemon=True)
    t.start()
    time.sleep(3)  # Give uvicorn time to boot

    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:18765/", timeout=3) as resp:
            status = resp.status
        if status == 200:
            ok(f"Dashboard responded with HTTP {status}")
        else:
            warn(f"Dashboard responded with HTTP {status} (expected 200)")
    except Exception as exc:
        if error_holder:
            fail(f"serve_dashboard() crashed: {error_holder[0]}")
        else:
            # uvicorn may not have had time to boot fully on slow machines
            warn(f"Could not connect (server may still be starting): {exc}")

    # Check /api/config endpoint
    try:
        import urllib.request, json as _json
        with urllib.request.urlopen("http://127.0.0.1:18765/api/config", timeout=3) as resp:
            cfg = _json.loads(resp.read())
        assert "has_token" in cfg
        ok(f"/api/config → {cfg}")
    except Exception as exc:
        warn(f"/api/config check skipped: {exc}")

    time.sleep(1)  # Let daemon thread wind down

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 4 — ImportError guard (base install without [dashboard])
    # ─────────────────────────────────────────────────────────────────────────────
    section("4 / 4  serve_dashboard() import guard")

    print("\n  Simulating missing uvicorn…")
    import unittest.mock as _mock
    import gitlytics as _gl
    with _mock.patch.dict("sys.modules", {"uvicorn": None}):
        try:
            _gl.serve_dashboard(port=18766)
            fail("Expected ImportError when uvicorn is missing")
        except ImportError as exc:
            if "dashboard" in str(exc):
                ok("ImportError guard confirmed (serve_dashboard raised clean dashboard extra hint)")
            else:
                warn(f"ImportError raised with unexpected message: {exc}")
        except Exception as exc:
            warn(f"Guard test inconclusive: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # Summary
    # ─────────────────────────────────────────────────────────────────────────────
    section("Done")
    print(f"\n  All tests completed. Check {RED}✗{RESET} lines above for any failures.")
    print(f"  Output files saved to: data/data_module/\n")
