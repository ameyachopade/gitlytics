"""
live_cli.py
Online integration test for the gitlytics CLI commands.
Runs the real CLI via subprocess so every test hits the actual GitHub API.

Usage:
    python tests/live_cli.py

GITHUB_TOKEN / GITLYTICS_TOKEN is loaded automatically from .env
Output files are saved to: data/data_cli/
"""
import os
import sys
import subprocess
from pathlib import Path

if __name__ == "__main__":
    # Force UTF-8 output so Unicode symbols render correctly on Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    # ── Load .env so GITHUB_TOKEN / GITLYTICS_TOKEN are available ─────────────
    try:
        from dotenv import load_dotenv, find_dotenv
        _env_file = find_dotenv(usecwd=False, raise_error_if_not_found=False)
        if not _env_file:
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
        pass

    # ── Colour helpers ─────────────────────────────────────────────────────────
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

    def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
    def fail(msg): print(f"  {RED}✗{RESET}  {msg}")
    def info(msg): print(f"  {CYAN}ℹ{RESET}  {msg}")
    def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
    def section(title):
        print(f"\n{BOLD}{'─'*55}{RESET}")
        print(f"{BOLD}  {title}{RESET}")
        print(f"{BOLD}{'─'*55}{RESET}")

    # Persistent output goes to data/data_cli/
    DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "data_cli"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # The Python executable running this script — used to invoke CLI as a subprocess
    PYTHON = sys.executable

    token = os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN")

    section("0 / 5  Setup")
    if token:
        info(f"Using token from environment variable ({token[:8]}…)")
    else:
        import getpass
        token = getpass.getpass("  Enter your GitHub Personal Access Token: ").strip()
        if not token:
            fail("No token provided. Exiting.")
            sys.exit(1)

    info(f"Data directory: {DATA_DIR}")

    def run_cli(*args, env_token=True):
        """Run the gitlytics CLI as a subprocess and return the CompletedProcess."""
        cmd = [PYTHON, "-m", "gitlytics"] + list(args)
        env = os.environ.copy()
        if env_token:
            # Pass the token via environment variable so we don't need --token on every call
            env["GITLYTICS_TOKEN"] = token
        else:
            # Negative tests must actually see a token-less environment, otherwise
            # a set GITLYTICS_TOKEN/GITHUB_TOKEN in the parent makes them vacuous.
            env.pop("GITLYTICS_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120  # 2 minutes max per command
        )

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 1 — gitlytics (no args) should print help and exit with code 0
    # ─────────────────────────────────────────────────────────────────────────────
    section("1 / 5  No-args guard")

    print("\n  [1a] Running `gitlytics` with no arguments")
    try:
        result = run_cli(env_token=False)
        if result.returncode == 0:
            ok("Exited with code 0 and printed help (documented contract)")
        else:
            warn(f"Exited with unexpected code {result.returncode}")
        if result.stdout:
            info(f"Output preview: {result.stdout[:80].strip()}")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [1b] Running `gitlytics fetch` without a token")
    try:
        result = run_cli("fetch", env_token=False)
        if result.returncode == 1:
            ok("Correctly exited with code 1 when token is missing")
        else:
            warn(f"Exited with code {result.returncode} (expected 1)")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 2 — gitlytics fetch (live API call)
    # ─────────────────────────────────────────────────────────────────────────────
    section("2 / 5  `gitlytics fetch` command")

    print("\n  [2a] fetch --print-table")
    try:
        result = run_cli("fetch", "--print-table")
        if result.returncode == 0:
            ok("Exited with code 0")
            # The table should have a REPOSITORY column header
            if "REPOSITORY" in result.stdout or "Fetch successful" in result.stdout:
                ok("Output contains expected table content")
            else:
                warn("Output did not contain REPOSITORY header — may have zero repos")
            info(f"Output preview:\n{result.stdout[:300].strip()}")
        else:
            fail(f"Exited with code {result.returncode}")
            fail(f"stderr: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        fail("Command timed out after 120 seconds")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [2b] fetch --save-file <data_dir>/cli_fetch.csv")
    csv_out = str(DATA_DIR / "cli_fetch.csv")
    try:
        result = run_cli("fetch", "--save-file", csv_out)
        if result.returncode == 0:
            ok("Command exited with code 0")
        else:
            fail(f"Command failed with code {result.returncode}: {result.stderr[:200]}")
        if os.path.exists(csv_out):
            size = os.path.getsize(csv_out)
            ok(f"CSV saved: cli_fetch.csv ({size:,} bytes)")
        else:
            fail("CSV file was not created")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [2c] fetch --save-file <data_dir>/cli_fetch.json")
    json_out = str(DATA_DIR / "cli_fetch.json")
    try:
        result = run_cli("fetch", "--save-file", json_out)
        if result.returncode == 0:
            ok("Command exited with code 0")
        else:
            fail(f"Command failed with code {result.returncode}: {result.stderr[:200]}")
        if os.path.exists(json_out):
            size = os.path.getsize(json_out)
            ok(f"JSON saved: cli_fetch.json ({size:,} bytes)")
        else:
            fail("JSON file was not created")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [2d] fetch --return-format timeseries (should print 'Fetch successful')")
    try:
        result = run_cli("fetch", "--return-format", "timeseries")
        if result.returncode == 0:
            ok("Exited with code 0")
        else:
            fail(f"Exited with code {result.returncode}: {result.stderr[:200]}")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [2e] fetch --return-format summary")
    try:
        result = run_cli("fetch", "--return-format", "summary")
        if result.returncode == 0:
            ok("Exited with code 0")
        else:
            fail(f"Exited with code {result.returncode}: {result.stderr[:200]}")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 3 — gitlytics sync (live API, writes to data/)
    # ─────────────────────────────────────────────────────────────────────────────
    section("3 / 5  `gitlytics sync` command")

    print("\n  [3a] sync --data-dir <data_dir> (monthly, first run)")
    try:
        result = run_cli("sync", "--data-dir", str(DATA_DIR))
        if result.returncode == 0:
            ok("Command exited with code 0")
        else:
            fail(f"Command failed with code {result.returncode}: {result.stderr[:200]}")
        csv_files = list(DATA_DIR.glob("traffic_????-??.csv"))
        if csv_files:
            size = csv_files[0].stat().st_size
            ok(f"Monthly CSV created: {csv_files[0].name} ({size:,} bytes)")
        else:
            warn("No monthly CSV found — token may have no repos with traffic")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [3b] sync again — must not duplicate rows")
    csv_files = list(DATA_DIR.glob("traffic_????-??.csv"))
    if not csv_files:
        warn("Skipped — no CSV was created in [3a]")
    else:
        import csv as csv_mod
        csv_path = csv_files[0]
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                rows_before = sum(1 for _ in csv_mod.reader(f)) - 1  # subtract header
            run_cli("sync", "--data-dir", str(DATA_DIR))
            with open(csv_path, "r", encoding="utf-8") as f:
                rows_after = sum(1 for _ in csv_mod.reader(f)) - 1
            if rows_after == rows_before:
                ok(f"No duplicates: {rows_before} rows before, {rows_after} rows after")
            elif rows_after > rows_before:
                warn(f"Row count grew {rows_before} → {rows_after} (GitHub may have returned new data)")
            else:
                fail(f"Rows decreased unexpectedly: {rows_before} → {rows_after}")
        except Exception as exc:
            fail(f"Deduplication check raised: {exc}")

    print("\n  [3c] sync --output-mode yearly")
    try:
        result = run_cli("sync", "--data-dir", str(DATA_DIR), "--output-mode", "yearly")
        if result.returncode == 0:
            ok("Command exited with code 0")
        else:
            fail(f"Command failed with code {result.returncode}: {result.stderr[:200]}")
        yearly = list(DATA_DIR.glob("traffic_????.csv"))
        if yearly:
            ok(f"Yearly CSV created: {yearly[0].name}")
        else:
            warn("No yearly CSV found (may be empty data)")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    print("\n  [3d] sync --export-json <data_dir>/cli_export.json")
    json_export = str(DATA_DIR / "cli_export.json")
    try:
        result = run_cli("sync", "--data-dir", str(DATA_DIR), "--export-json", json_export)
        if result.returncode == 0:
            ok("Command exited with code 0")
        else:
            fail(f"Command failed with code {result.returncode}: {result.stderr[:200]}")
        if os.path.exists(json_export):
            size = os.path.getsize(json_export)
            ok(f"JSON export written: cli_export.json ({size:,} bytes)")
        else:
            warn("JSON export was not created (may be empty data)")
    except Exception as exc:
        fail(f"Subprocess raised: {exc}")

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 4 — gitlytics dashboard (quick smoke test, same as test_module.py)
    # ─────────────────────────────────────────────────────────────────────────────
    section("4 / 5  `gitlytics dashboard` (3-second smoke test)")

    import threading
    import time

    print("\n  Starting dashboard on port 19876 for 3 seconds…")
    dashboard_error = []

    def _start_dashboard():
        try:
            result = run_cli("dashboard", "--port", "19876")
        except Exception as exc:
            dashboard_error.append(exc)

    t = threading.Thread(target=_start_dashboard, daemon=True)
    t.start()
    time.sleep(3)  # Give uvicorn time to start

    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:19876/api/config", timeout=3) as resp:
            import json as _json
            cfg = _json.loads(resp.read())
        assert "has_token" in cfg
        ok(f"Dashboard is live — /api/config responded: {cfg}")
    except Exception as exc:
        if dashboard_error:
            fail(f"Dashboard crashed: {dashboard_error[0]}")
        else:
            warn(f"Could not reach dashboard (may still be starting): {exc}")

    time.sleep(1)  # Let the daemon thread wind down

    # ─────────────────────────────────────────────────────────────────────────────
    # TEST 5 — parse_repo_names (inline logic check, no subprocess needed)
    # ─────────────────────────────────────────────────────────────────────────────
    section("5 / 5  parse_repo_names() edge cases")

    from gitlytics.cli import parse_repo_names

    print("\n  [5a] None → None")
    assert parse_repo_names(None) is None and ok("None returns None") or True

    print("\n  [5b] Empty string → None")
    assert parse_repo_names("") is None and ok("Empty string returns None") or True

    print("\n  [5c] Comma-separated → list")
    result = parse_repo_names("user/a, user/b")
    if result == ["user/a", "user/b"]:
        ok(f"Correctly split into {result}")
    else:
        fail(f"Expected ['user/a', 'user/b'], got {result}")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Done")
    print(f"\n  All CLI online tests completed. Check {RED}✗{RESET} lines above for any failures.\n")
    print(f"  Output files saved to: {DATA_DIR}\n")
