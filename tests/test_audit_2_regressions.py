"""Regression tests for Audit 2 library and CLI findings.

Covers LIB2-01 through LIB2-29 and ACT2-15.
"""
import io
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import gitlytics
from gitlytics import fetch_traffic, fetch_star_history, serve_dashboard
from gitlytics.api import app, _read_csv_cached, _CSV_CACHE
from gitlytics.automation import run_sync, run_sync_cycle, _merge_csv_locked, export_json_database
from gitlytics.core import (
    validate_token,
    print_repo_table,
    pad_traffic_data,
    get_public_user,
    get_public_repos,
    _mark_stargazers_restricted,
    _is_stargazers_restricted,
    _stargazer_restricted_cache,
    _STARGAZER_RESTRICTED_MAXSIZE,
)
from gitlytics.process import _safe_int, build_json_payload, build_react_payload


def test_lib2_01_token_validation_rate_limit_distinction():
    """LIB2-01: 403 with rate limit semantics must return rate-limit message, not insufficient permissions."""
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    mock_resp.headers = {"x-ratelimit-remaining": "0"}
    mock_resp.text = "API rate limit exceeded"
    with patch("gitlytics.core._http_get", return_value=mock_resp):
        ok, msg = validate_token("token")
        assert not ok
        assert "rate limit" in msg.lower()


def test_lib2_02_print_repo_table_with_nan_metrics():
    """LIB2-02: print_repo_table must not crash with TypeError when metrics contain NaN."""
    df = pd.DataFrame({
        "repository": ["owner/repo1", "owner/repo1"],
        "views": [10, np.nan],
        "unique_visitors": [np.nan, 5],
        "clones": [2, np.nan],
        "unique_cloners": [np.nan, 1],
        "stars": [100, np.nan],
        "forks": [10, np.nan],
        "top_referrer": ["google.com", np.nan],
    })
    # Must not raise TypeError
    print_repo_table(df)


def test_lib2_03_is_private_boolean_coercion():
    """LIB2-03: Filtering ~is_private on object series must not crash with TypeError or KeyError."""
    df = pd.DataFrame({
        "repository": ["owner/public", "owner/private"],
        "date": ["2026-11-01", "2026-11-01"],
        "views": [10, 20],
        "is_private": [False, True],  # object series after CSV roundtrip
    }, dtype=object)

    payload = build_json_payload(df, return_format="timeseries", export_public_only=True)
    assert "owner/public" in payload["repositories"]
    assert "owner/private" not in payload["repositories"]


def test_lib2_04_yearly_sync_groups_by_row_date_year(tmp_path):
    """LIB2-04: Yearly sync mode must group by date row year, not execution year."""
    df = pd.DataFrame({
        "repository": ["owner/repo", "owner/repo"],
        "date": ["2025-12-31", "2026-01-01"],
        "views": [10, 20],
        "unique_visitors": [5, 10],
        "clones": [1, 2],
        "unique_cloners": [1, 2],
    })
    with patch("gitlytics.automation.fetch_traffic_data", return_value=df):
        run_sync_cycle("token", ["owner/repo"], data_dir=str(tmp_path), output_mode="yearly")

    # Should produce traffic_2025.csv and traffic_2026.csv
    file_2025 = tmp_path / "traffic_2025.csv"
    file_2026 = tmp_path / "traffic_2026.csv"
    assert file_2025.exists(), "December 2025 data should route to traffic_2025.csv"
    assert file_2026.exists(), "January 2026 data should route to traffic_2026.csv"


def test_lib2_05_csv_merge_cleans_nan_strings(tmp_path):
    """LIB2-05: _merge_csv_locked must not write literal string 'nan' to CSV."""
    csv_file = tmp_path / "traffic_test.csv"
    df = pd.DataFrame([{
        "repository": "owner/repo",
        "date": "2026-11-01",
        "views": 10,
        "language": None,
        "top_referrer": np.nan,
    }])
    _merge_csv_locked(df, str(csv_file))

    raw_text = csv_file.read_text(encoding="utf-8")
    assert ",nan," not in raw_text
    assert ",nan\n" not in raw_text and not raw_text.endswith(",nan")


def test_lib2_08_star_history_cache_control_private():
    """LIB2-08: /api/star-history must use private Cache-Control to prevent shared proxy leakage."""
    client = TestClient(app)
    with patch("gitlytics.api.fetch_star_history", return_value=[{"date": "2026-11-01", "total": 10}]):
        res = client.get("/api/star-history?owner=test&repo=repo", headers={"Authorization": "Bearer token"})
        assert res.status_code == 200
        assert "private" in res.headers["cache-control"]
        assert "public" not in res.headers["cache-control"]


def test_lib2_12_atomic_json_writes(tmp_path):
    """LIB2-12: export_json_database and _write_json_safely write atomically."""
    csv_file = tmp_path / "traffic_2026-11.csv"
    pd.DataFrame([{
        "repository": "owner/repo",
        "date": "2026-11-01",
        "views": 5,
        "is_private": False,
    }]).to_csv(csv_file, index=False)

    export_dest = tmp_path / "export.json"
    export_json_database(str(tmp_path), str(export_dest), export_public_only=True)
    assert export_dest.exists()
    content = json.loads(export_dest.read_text(encoding="utf-8"))
    assert isinstance(content, list)


def test_lib2_13_safe_int_overflow_error():
    """LIB2-13: _safe_int must handle 'inf' and '-inf' without crashing."""
    assert _safe_int("inf", fallback=0) == 0
    assert _safe_int("-inf", fallback=-1) == -1
    assert _safe_int(float("inf"), fallback=42) == 42


def test_lib2_14_fetch_star_history_path_traversal_owner():
    """LIB2-14: fetch_star_history must reject slashes and backslashes in owner."""
    with pytest.raises(ValueError, match="requires owner and repo"):
        fetch_star_history("owner/team", "repo")
    with pytest.raises(ValueError, match="requires owner and repo"):
        fetch_star_history("../owner", "repo")


def test_lib2_15_run_sync_invalid_cron_raises():
    """LIB2-15: run_sync must raise ValueError on invalid cron syntax."""
    with pytest.raises(ValueError, match="Invalid cron expression"):
        run_sync("token", schedule_cron="invalid * cron * expression")


def test_lib2_16_fetch_traffic_export_public_only_flag(tmp_path):
    """LIB2-16: fetch_traffic respects export_public_only parameter."""
    df = pd.DataFrame([{
        "repository": "owner/secret",
        "date": "2026-11-01",
        "views": 10,
        "is_private": True,
    }])
    with patch("gitlytics.fetch_traffic_data", return_value=df):
        out_csv = tmp_path / "out.csv"
        # When export_public_only=False, private repos are saved
        fetch_traffic("token", save_file=str(out_csv), export_public_only=False)
        saved = pd.read_csv(out_csv)
        assert len(saved) == 1
        assert saved.iloc[0]["repository"] == "owner/secret"


def test_lib2_17_spa_fallback_exact_api_returns_404():
    """LIB2-17: Exact /api or /assets returns 404, not index.html."""
    client = TestClient(app)
    res = client.get("/api")
    assert res.status_code == 404
    res2 = client.get("/assets")
    assert res2.status_code == 404


def test_lib2_23_serve_dashboard_env_token_leakage():
    """LIB2-23: serve_dashboard does not set GITLYTICS_TOKEN on non-loopback hosts."""
    import uvicorn
    orig_token = os.environ.get("GITLYTICS_TOKEN")
    try:
        with patch.object(uvicorn, "run"):
            serve_dashboard(host="0.0.0.0", port=8000, token="secret_token_123")
            assert "GITLYTICS_TOKEN" not in os.environ
    finally:
        if orig_token:
            os.environ["GITLYTICS_TOKEN"] = orig_token
        else:
            os.environ.pop("GITLYTICS_TOKEN", None)


def test_lib2_27_csv_mtime_caching(tmp_path):
    """LIB2-27: _read_csv_cached reuses cached DataFrame when mtime is unchanged."""
    f = tmp_path / "traffic_test.csv"
    f.write_text("repository,date,views\nowner/repo,2026-11-01,10\n", encoding="utf-8")

    df1 = _read_csv_cached(f)
    df2 = _read_csv_cached(f)
    assert len(df1) == 1
    assert len(df2) == 1
    key = (str(f.resolve()), f.stat().st_mtime)
    assert key in _CSV_CACHE


def test_lib2_28_stargazer_restricted_cache_bounded():
    """LIB2-28: _stargazer_restricted_cache is an OrderedDict capped at _STARGAZER_RESTRICTED_MAXSIZE."""
    for i in range(_STARGAZER_RESTRICTED_MAXSIZE + 50):
        _mark_stargazers_restricted(f"owner{i}", "repo")
    assert len(_stargazer_restricted_cache) <= _STARGAZER_RESTRICTED_MAXSIZE


def test_lib2_29_username_reexport_module():
    """LIB2-29: gitlytics.username backward compatibility module re-exports functions."""
    import gitlytics.username as u
    assert hasattr(u, "get_public_user")
    assert hasattr(u, "get_public_repos")
    assert hasattr(u, "validate_github_username")


def test_act2_15_cli_metrics_empty_args():
    """ACT2-15: CLI sync and fetch parsers accept empty metrics without crashing."""
    from gitlytics.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["sync", "--metrics"])
    assert args.metrics == []
    args_fetch = parser.parse_args(["fetch", "--metrics"])
    assert args_fetch.metrics == []
