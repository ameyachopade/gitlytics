"""
tests/conftest.py
Shared pytest configuration and fixtures for the gitlytics test suite.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OFFLINE tests  (pytest, no token needed, instant)
   python -m pytest tests/ -v

   test_core.py        unit tests for core.py
   test_automation.py  unit tests for automation.py
   test_cli.py         unit tests for cli.py
   test_process.py     unit tests for process.py
   test_api.py         unit tests for api.py
   test_username.py    unit tests for username-mode (get_public_user/repos + /api/username)

 ONLINE / LIVE tests  (scripts, real GitHub API, writes to data/)
   python tests/live_module.py       → Python public API
   python tests/live_cli.py          → CLI commands via subprocess
   python tests/live_automation.py   → sync engine / CSV writing
   python tests/live_username.py     → username mode (no token needed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This conftest.py applies only to the offline pytest suite.
It loads .env and provides shared fixtures.
"""
import os
import pytest
from pathlib import Path

# ── Load .env before any test runs ───────────────────────────────────────────
# Walk up two levels from tests/ to find the project root where .env lives
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # gitlytics/
_ENV_FILE = _PROJECT_ROOT / ".env"

try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        # override=False means we won't overwrite a token that's already in the shell env
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass  # python-dotenv not installed — rely on env vars that are already set

# ── Fixtures ──────────────────────────────────────────────────────────────────

# Persistent data dir for integration tests. Falls back to a per-process temp
# directory so the suite doesn't write into the user's project tree.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "_data"


@pytest.fixture(autouse=True)
def _clear_api_caches():
    """Keep module-level caches from leaking state between tests.

    ``api`` keeps a short-TTL live-traffic cache and a CSV read cache. Their
    keys are derived from the token / file path, so two tests that reuse the
    same fake token would otherwise see each other's data.
    """
    try:
        from gitlytics import api as _api
    except Exception:
        yield
        return

    def _clear():
        for cache_name, lock_name in (
            ("_TRAFFIC_CACHE", "_TRAFFIC_CACHE_LOCK"),
            ("_DEEP_STATS_CACHE", "_DEEP_STATS_CACHE_LOCK"),
            ("_CSV_CACHE", "_CSV_CACHE_LOCK"),
            ("_auth_cache", "_auth_cache_lock"),
        ):
            cache = getattr(_api, cache_name, None)
            lock = getattr(_api, lock_name, None)
            if cache is None:
                continue
            if lock is None:
                cache.clear()
            else:
                with lock:
                    cache.clear()
        try:
            from gitlytics import core as _core
            rcache = getattr(_core, "_stargazer_restricted_cache", None)
            rlock = getattr(_core, "_stargazer_restricted_lock", None)
            if rcache is not None:
                if rlock is not None:
                    with rlock:
                        rcache.clear()
                else:
                    rcache.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory) -> Path:
    """Per-session persistent data directory using pytest's temp dir."""
    return tmp_path_factory.mktemp("data")


@pytest.fixture(scope="session")
def github_token() -> str:
    """
    Returns the GitHub token from the environment (.env or system env).
    Automatically skips any test that uses this fixture when no token is available,
    so unit tests that use mocks are never blocked.
    """
    token = os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        pytest.skip("No GITHUB_TOKEN / GITLYTICS_TOKEN found in .env or environment")
    return token
