"""Regression tests for Audit 3 Python library findings.

Covers:
- LIB3-01: pad_traffic_data 15-day rolling window retaining the oldest day (range(14, -1, -1))
- LIB3-03: build_react_payload non-null referrer and path extraction via .dropna().iloc[-1]
- LIB3-04: Username endpoint Authorization Bearer token forwarding
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gitlytics.api import app, _fetch_username_data
from gitlytics.core import pad_traffic_data, build_tidy_rows, fetch_traffic_data
from gitlytics.process import build_react_payload

client = TestClient(app)


# ── LIB3-01: 15-day rolling window retaining oldest day ─────────────────────────

def test_lib3_01_pad_traffic_data_retains_15_days_including_oldest():
    """LIB3-01: pad_traffic_data must preserve day 14 (14 days ago, total 15 days)."""
    now_utc = datetime.now(timezone.utc)
    # Generate 15 consecutive days of traffic: day 14 ago up to today (day 0)
    views_items = []
    clones_items = []
    for days_ago in range(14, -1, -1):
        dt = (now_utc - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        views_items.append({"timestamp": f"{dt}T00:00:00Z", "count": 10 + days_ago, "uniques": 5 + days_ago})
        clones_items.append({"timestamp": f"{dt}T00:00:00Z", "count": 20 + days_ago, "uniques": 8 + days_ago})

    traffic = {
        "views": {"views": views_items},
        "clones": {"clones": clones_items},
    }

    padded = pad_traffic_data(traffic)
    assert len(padded) == 15

    # Check oldest day (14 days ago) is retained at index 0
    oldest_expected_date = (now_utc - timedelta(days=14)).strftime("%Y-%m-%d")
    assert padded[0]["date"] == oldest_expected_date
    assert padded[0]["views"] == 24  # 10 + 14
    assert padded[0]["clones"] == 34  # 20 + 14

    # Check today (day 0) is at index 14
    today_expected_date = now_utc.strftime("%Y-%m-%d")
    assert padded[-1]["date"] == today_expected_date
    assert padded[-1]["views"] == 10
    assert padded[-1]["clones"] == 20


def test_lib3_01_pad_traffic_data_empty_traffic_yields_15_days():
    """LIB3-01: Empty traffic input must yield a zero-filled 15-day window ending today."""
    traffic = {"views": {"views": []}, "clones": {"clones": []}}
    padded = pad_traffic_data(traffic)

    assert len(padded) == 15
    assert all(r["views"] == 0 for r in padded)
    assert all(r["clones"] == 0 for r in padded)
    assert all(r["unique_visitors"] == 0 for r in padded)
    assert all(r["unique_cloners"] == 0 for r in padded)

    # Dates must be 15 strictly consecutive days ending on today (UTC)
    now_utc = datetime.now(timezone.utc)
    expected_dates = [(now_utc - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14, -1, -1)]
    actual_dates = [r["date"] for r in padded]
    assert actual_dates == expected_dates


def test_lib3_01_build_tidy_rows_produces_15_rows():
    """LIB3-01: build_tidy_rows must produce 15 rows for every repository."""
    repo = {
        "full_name": "owner/repo",
        "private": False,
        "stargazers_count": 42,
        "forks_count": 7,
    }
    traffic = {"views": {"views": []}, "clones": {"clones": []}, "referrers": [], "paths": []}
    rows = build_tidy_rows(repo, traffic)
    assert len(rows) == 15
    assert all(r["repository"] == "owner/repo" for r in rows)


# ── LIB3-03: .dropna().iloc[-1] for referrers and paths ─────────────────────────

def test_lib3_03_preserves_historical_referrers_when_latest_record_is_nan():
    """LIB3-03: When latest daily record has NaN referrers, historical referrers must be retained."""
    historical_referrers = json.dumps([
        {"referrer": "github.com", "count": 100, "uniques": 45},
        {"referrer": "google.com", "count": 50, "uniques": 20},
    ])
    historical_paths = json.dumps([
        {"path": "/docs", "title": "Documentation", "count": 75, "uniques": 30},
    ])

    df = pd.DataFrame([
        {
            "repository": "owner/repo",
            "date": "2026-11-01",
            "views": 10,
            "unique_visitors": 5,
            "clones": 2,
            "unique_cloners": 1,
            "_raw_referrers": historical_referrers,
            "_raw_paths": historical_paths,
            "stars": 10,
            "forks": 2,
        },
        {
            "repository": "owner/repo",
            "date": "2026-11-02",
            "views": 8,
            "unique_visitors": 4,
            "clones": 1,
            "unique_cloners": 1,
            "_raw_referrers": np.nan,  # Latest day has NaN
            "_raw_paths": None,        # Latest day has None
            "stars": 10,
            "forks": 2,
        },
    ])

    repos = build_react_payload(df)
    assert len(repos) == 1
    repo_data = repos[0]

    assert "_referrers" in repo_data
    assert len(repo_data["_referrers"]) == 2
    assert repo_data["_referrers"][0]["referrer"] == "github.com"
    assert repo_data["_referrers"][1]["referrer"] == "google.com"

    assert "_paths" in repo_data
    assert len(repo_data["_paths"]) == 1
    assert repo_data["_paths"][0]["path"] == "/docs"


def test_lib3_03_all_nan_referrers_and_paths_safely_defaults_to_empty_lists():
    """LIB3-03: When all rows contain NaN or None for referrers/paths, safely return empty lists."""
    df = pd.DataFrame([
        {
            "repository": "owner/repo",
            "date": "2026-11-01",
            "views": 5,
            "unique_visitors": 2,
            "clones": 1,
            "unique_cloners": 1,
            "_raw_referrers": np.nan,
            "_raw_paths": None,
        },
        {
            "repository": "owner/repo",
            "date": "2026-11-02",
            "views": 3,
            "unique_visitors": 1,
            "clones": 0,
            "unique_cloners": 0,
            "_raw_referrers": None,
            "_raw_paths": np.nan,
        },
    ])

    repos = build_react_payload(df)
    repo_data = repos[0]
    assert repo_data["_referrers"] == []
    assert repo_data["_paths"] == []


def test_lib3_03_missing_referrer_and_path_columns_handled_gracefully():
    """LIB3-03: Missing _raw_referrers / _raw_paths columns safely default without KeyError."""
    df = pd.DataFrame([
        {
            "repository": "owner/repo",
            "date": "2026-11-01",
            "views": 5,
            "unique_visitors": 2,
            "clones": 1,
            "unique_cloners": 1,
        },
    ])

    repos = build_react_payload(df)
    repo_data = repos[0]
    assert repo_data["_referrers"] == []
    assert repo_data["_paths"] == []


# ── LIB3-04: Username endpoint Authorization token forwarding ──────────────────

@patch("gitlytics.api.get_public_repos", return_value=[{"name": "repo1", "stargazers_count": 10}])
@patch("gitlytics.api.get_public_user", return_value={"login": "octocat", "name": "The Octocat", "avatar_url": ""})
def test_lib3_04_get_username_forwards_bearer_token(mock_user, mock_repos):
    """LIB3-04: GET /api/username/{username} forwards Bearer token from Authorization header."""
    headers = {"Authorization": "Bearer ghp_secret_test_token_123"}
    resp = client.get("/api/username/octocat", headers=headers)
    assert resp.status_code == 200

    mock_user.assert_called_once_with("octocat", token="ghp_secret_test_token_123")
    mock_repos.assert_called_once_with("octocat", token="ghp_secret_test_token_123")


@patch("gitlytics.api.get_public_repos", return_value=[])
@patch("gitlytics.api.get_public_user", return_value={"login": "octocat", "name": "The Octocat", "avatar_url": ""})
def test_lib3_04_get_username_without_token_calls_unauthenticated(mock_user, mock_repos):
    """LIB3-04: GET /api/username/{username} without Authorization header calls without token."""
    resp = client.get("/api/username/octocat")
    assert resp.status_code == 200

    mock_user.assert_called_once_with("octocat")
    mock_repos.assert_called_once_with("octocat")


@patch("gitlytics.api.get_public_repos", return_value=[])
@patch("gitlytics.api.get_public_user", return_value={"login": "octocat", "name": "The Octocat", "avatar_url": ""})
def test_lib3_04_post_username_forwards_bearer_header(mock_user, mock_repos):
    """LIB3-04: POST /api/username forwards Bearer token from Authorization header."""
    headers = {"Authorization": "Bearer ghp_post_header_token"}
    resp = client.post("/api/username", json={"username": "octocat"}, headers=headers)
    assert resp.status_code == 200

    mock_user.assert_called_once_with("octocat", token="ghp_post_header_token")
    mock_repos.assert_called_once_with("octocat", token="ghp_post_header_token")


@patch("gitlytics.api.get_public_repos", return_value=[])
@patch("gitlytics.api.get_public_user", return_value={"login": "octocat", "name": "The Octocat", "avatar_url": ""})
def test_lib3_04_post_username_forwards_body_token(mock_user, mock_repos):
    """LIB3-04: POST /api/username forwards token provided in JSON request body."""
    resp = client.post("/api/username", json={"username": "octocat", "token": "ghp_body_token_456"})
    assert resp.status_code == 200

    mock_user.assert_called_once_with("octocat", token="ghp_body_token_456")
    mock_repos.assert_called_once_with("octocat", token="ghp_body_token_456")


@patch("gitlytics.api.get_public_repos", return_value=[])
@patch("gitlytics.api.get_public_user", return_value={"login": "octocat", "name": "The Octocat", "avatar_url": ""})
def test_lib3_04_direct_fetch_username_data_forwards_token(mock_user, mock_repos):
    """LIB3-04: Direct call to _fetch_username_data with token passes token to core functions."""
    result = _fetch_username_data("octocat", token="ghp_direct_call_token")
    assert "profile" in result
    assert "repos" in result

    mock_user.assert_called_once_with("octocat", token="ghp_direct_call_token")
    mock_repos.assert_called_once_with("octocat", token="ghp_direct_call_token")
