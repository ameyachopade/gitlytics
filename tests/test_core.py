"""
tests/test_core.py
Unit tests for gitlytics/core.py — network calls are mocked so these run offline.
"""
import pytest
from unittest.mock import patch, MagicMock
import warnings
import pandas as pd

from gitlytics.core import (
    validate_token, get_user_profile, make_headers,
    get_all_repos, get_repo_traffic, build_tidy_rows,
    fetch_traffic_data, pad_traffic_data, print_repo_table
)


# ── make_headers ──────────────────────────────────────────────────────────────

class TestMakeHeaders:
    def test_returns_dict(self):
        # Should give back a plain dict with auth info
        result = make_headers("mytoken")
        assert isinstance(result, dict)

    def test_contains_authorization(self):
        # The Authorization header must use the Bearer scheme
        result = make_headers("mytoken")
        assert result["Authorization"] == "Bearer mytoken"

    def test_contains_accept_header(self):
        # GitHub's v3 JSON media type must be set
        result = make_headers("mytoken")
        assert "application/vnd.github" in result.get("Accept", "")


# ── validate_token ────────────────────────────────────────────────────────────

class TestValidateToken:
    @patch("gitlytics.core._http_get")
    def test_valid_token_returns_true_and_login(self, mock_get):
        # A 200 response with a login field means the token is good
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"login": "testuser"}
        mock_get.return_value = mock_response

        valid, username = validate_token("valid_token")
        assert valid is True
        assert username == "testuser"

    @patch("gitlytics.core._http_get")
    def test_401_returns_false(self, mock_get):
        # 401 = wrong token or it's expired
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        valid, msg = validate_token("bad_token")
        assert valid is False
        assert "401" in msg

    @patch("gitlytics.core._http_get")
    def test_403_returns_false(self, mock_get):
        # 403 = token exists but doesn't have enough permissions
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        valid, msg = validate_token("limited_token")
        assert valid is False
        assert "403" in msg

    @patch("gitlytics.core._http_get")
    def test_network_error_returns_false(self, mock_get):
        # Simulate being offline — should return False, not crash
        import requests
        mock_get.side_effect = requests.exceptions.ConnectionError("No internet")
        valid, msg = validate_token("any_token")
        assert valid is False
        assert "No internet" in msg


# ── get_user_profile ──────────────────────────────────────────────────────────

class TestGetUserProfile:
    @patch("gitlytics.core._http_get")
    def test_returns_all_fields(self, mock_get):
        # Should hand back login, name, and avatar_url from the GitHub response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "ameyachopade",
            "name": "Ameya Chopade",
            "avatar_url": "https://avatars.githubusercontent.com/u/123"
        }
        mock_get.return_value = mock_response

        profile = get_user_profile("token")
        assert profile["login"] == "ameyachopade"
        assert profile["name"] == "Ameya Chopade"
        assert profile["avatar_url"].startswith("https://")

    @patch("gitlytics.core._http_get")
    def test_null_name_falls_back_to_login(self, mock_get):
        # GitHub allows users to leave their display name blank — fall back to username
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"login": "ameyachopade", "name": None, "avatar_url": "https://x"}
        mock_get.return_value = mock_response

        profile = get_user_profile("token")
        assert profile["name"] == "ameyachopade"  # login used as fallback

    @patch("gitlytics.core._http_get")
    def test_non_200_returns_empty_strings(self, mock_get):
        # If the API call fails, return empty strings so callers don't have to handle None
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        profile = get_user_profile("bad_token")
        assert profile == {"login": "", "name": "", "avatar_url": "", "followers": 0, "following": 0}


# ── pad_traffic_data ──────────────────────────────────────────────────────────

class TestPadTrafficData:
    def test_always_returns_15_days(self):
        # GitHub only returns days with activity — we always want a full 15-day window
        traffic = {"views": {"views": []}, "clones": {"clones": []}}
        result = pad_traffic_data(traffic)
        assert len(result) == 15

    def test_fills_zeros_for_missing_days(self):
        # Days with no activity should get 0 counts, not be skipped
        traffic = {"views": {"views": []}, "clones": {"clones": []}}
        result = pad_traffic_data(traffic)
        assert all(r["views"] == 0 for r in result)

    def test_uses_api_data_for_active_days(self):
        # Actual traffic counts from GitHub should appear in the right day slot
        traffic = {
            "views": {"views": [{"timestamp": "2025-06-14T00:00:00Z", "count": 42, "uniques": 10}]},
            "clones": {"clones": [{"timestamp": "2025-06-14T00:00:00Z", "count": 5, "uniques": 3}]}
        }
        result = pad_traffic_data(traffic)
        day = next(r for r in result if r["date"] == "2025-06-14")
        assert day["views"] == 42
        assert day["clones"] == 5


# ── build_tidy_rows ───────────────────────────────────────────────────────────

class TestBuildTidyRows:
    def test_returns_15_rows(self):
        # One row per calendar day — always 15 rows for each repo
        repo = {"full_name": "user/repo", "private": False, "stargazers_count": 5, "forks_count": 2}
        traffic = {"views": {"views": []}, "clones": {"clones": []}, "referrers": [], "paths": []}
        rows = build_tidy_rows(repo, traffic)
        assert len(rows) == 15

    def test_each_row_has_repository_field(self):
        # Every row must carry the repo name so the DataFrame can be grouped
        repo = {"full_name": "user/repo", "private": False, "stargazers_count": 0, "forks_count": 0}
        traffic = {"views": {"views": []}, "clones": {"clones": []}, "referrers": [], "paths": []}
        rows = build_tidy_rows(repo, traffic)
        assert all(r["repository"] == "user/repo" for r in rows)

    def test_raw_fields_are_json_strings(self):
        # _raw_referrers and _raw_paths must be JSON strings — the dashboard decodes them
        repo = {"full_name": "u/r", "private": False, "stargazers_count": 0, "forks_count": 0}
        traffic = {
            "views": {"views": []}, "clones": {"clones": []},
            "referrers": [{"referrer": "github.com", "count": 5, "uniques": 3}],
            "paths": []
        }
        import json
        rows = build_tidy_rows(repo, traffic)
        assert json.loads(rows[0]["_raw_referrers"])[0]["referrer"] == "github.com"


# ── fetch_traffic_data (integration-style with mocks) ─────────────────────────

class TestFetchTrafficData:
    @patch("gitlytics.core.get_all_repos")
    @patch("gitlytics.core.get_repo_traffic")
    def test_returns_dataframe(self, mock_traffic, mock_repos):
        # Should always return a DataFrame — even when a repo has zero traffic
        mock_repos.return_value = [
            {"full_name": "user/repo", "private": False, "stargazers_count": 0, "forks_count": 0}
        ]
        mock_traffic.return_value = {
            "views": {"views": []}, "clones": {"clones": []},
            "referrers": [], "paths": []
        }
        result = fetch_traffic_data("token")
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 15  # 15-day window even with zero traffic

    @patch("gitlytics.core.get_all_repos")
    def test_empty_repo_list_returns_empty_df(self, mock_repos):
        # If the token has no repos at all, return an empty DataFrame gracefully
        mock_repos.return_value = []
        result = fetch_traffic_data("token")
        assert isinstance(result, pd.DataFrame)
        assert result.empty


# ── print_repo_table ──────────────────────────────────────────────────────────

class TestPrintRepoTable:
    def test_empty_df_prints_no_data_message(self, capsys):
        # When there's nothing to show, just print a friendly message
        print_repo_table(pd.DataFrame())
        captured = capsys.readouterr()
        assert "No data" in captured.out

    def test_prints_header_with_data(self, capsys):
        # A non-empty DataFrame should produce a table with a REPOSITORY header
        df = pd.DataFrame([{
            "repository": "user/repo", "views": 10, "unique_visitors": 5,
            "clones": 3, "unique_cloners": 2, "stars": 1, "forks": 0,
            "top_referrer": "github.com"
        }])
        print_repo_table(df)
        captured = capsys.readouterr()
        assert "REPOSITORY" in captured.out

    def test_no_nan_string_in_output(self, capsys):
        # Bug fix: NaN values must NOT render as the literal string "nan"
        import math
        df = pd.DataFrame([{
            "repository": "user/repo",
            "views": 10,
            "unique_visitors": 5,
            "clones": 3,
            "unique_cloners": 2,
            "stars": 1,
            "forks": 0,
            "top_referrer": float("nan"),  # The bug we are guarding against.
        }])
        print_repo_table(df)
        captured = capsys.readouterr()
        assert "nan" not in captured.out.lower()

    def test_no_pandas_downcast_future_warning(self, capsys):
        # LIB-09: fillna("") on a mixed-dtype frame triggered a pandas 3.1
        # FutureWarning about silent object downcasting.
        df = pd.DataFrame([{
            "repository": "user/repo", "views": 10, "unique_visitors": 5,
            "clones": 3, "unique_cloners": 2, "stars": 1, "forks": 0,
            "top_referrer": float("nan"),
        }])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            print_repo_table(df)
        downcast = [w for w in caught if "downcast" in str(w.message).lower()]
        assert not downcast, [str(w.message) for w in downcast]

    def test_long_referrer_does_not_truncate_header(self, capsys):
        # Bug fix: column width is computed from data, not hardcoded
        df = pd.DataFrame([{
            "repository": "user/repo", "views": 1, "unique_visitors": 1,
            "clones": 0, "unique_cloners": 0, "stars": 0, "forks": 0,
            "top_referrer": "https://very-long-referrer-domain.example.com/very/long/path"
        }])
        print_repo_table(df)
        captured = capsys.readouterr()
        # The long referrer should appear in full, not be cut off
        assert "very-long-referrer-domain" in captured.out


# ── pad_traffic_data with malformed input (regression test for KeyError) ─────

class TestPadTrafficDataMalformed:
    def test_handles_missing_timestamp(self):
        # Bug fix: rows missing the 'timestamp' key must not crash the function
        traffic = {
            "views": {"views": [
                {"timestamp": "2025-06-14T00:00:00Z", "count": 5, "uniques": 2},
                {"count": 99, "uniques": 1},  # No timestamp — should be skipped, not crash
            ]},
            "clones": {"clones": []}
        }
        result = pad_traffic_data(traffic)
        # Should still produce 15 rows and have the 5 views on the right day
        assert len(result) == 15
        day = next(r for r in result if r["date"] == "2025-06-14")
        assert day["views"] == 5


# ── fetch_traffic_data with empty repo_names list ─────────────────────────────

class TestFetchTrafficDataEmptyList:
    @patch("gitlytics.core.get_all_repos")
    def test_explicit_empty_repo_list_does_not_call_get_all_repos(self, mock_all):
        # Bug fix: repo_names=[] must mean "fetch nothing", not "fetch everything"
        from gitlytics.core import fetch_traffic_data
        result = fetch_traffic_data("token", repo_names=[])
        assert result.empty
        mock_all.assert_not_called()


# ── get_public_repos respects max_repos (no over-fetching) ────────────────────

class TestGetPublicReposRespectMaxRepos:
    @patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[])
    @patch("gitlytics.core._fetch_public_repos_by_updated")
    def test_max_repos_caps_per_page(self, mock_recent, mock_starred):
        # Bug fix: don't hardcode per_page=50 — respect max_repos to avoid wasted calls
        from gitlytics.core import get_public_repos
        mock_recent.return_value = []
        mock_starred.return_value = []
        get_public_repos("user", max_repos=5)
        # Each call should request per_page=5, not 50
        for call in mock_recent.call_args_list + mock_starred.call_args_list:
            assert call.kwargs.get("per_page") == 5


# ── _safe_get soft-error detection ───────────────────────────────────────────

class TestSafeGetSoftError:
    @patch("gitlytics.core._http_get")
    def test_generic_message_field_is_treated_as_soft_error(self, mock_get):
        # Bug fix: any 200-OK response with a 'message' field should be a soft error,
        # not just the two literal strings "Not Found" / "Bad credentials".
        from gitlytics.core import _safe_get
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"message": "Validation Failed"}
        mock_get.return_value = mock_response
        data, status = _safe_get("https://api.github.com/test", {})
        assert data == {}
        assert status == 200  # Caller can distinguish via empty body

    @patch("gitlytics.core._http_get")
    def test_normal_200_is_returned_intact(self, mock_get):
        # Sanity check: a normal 200-without-message response still works
        from gitlytics.core import _safe_get
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"items": [1, 2, 3]}
        mock_get.return_value = mock_response
        data, status = _safe_get("https://api.github.com/test", {})
        assert data == {"items": [1, 2, 3]}
        assert status == 200


# -- LIB-01/LIB-02 regression: failed traffic must not fabricate zeros ---------

class TestTrafficFailureHandling:
    @patch("gitlytics.core._safe_get")
    def test_both_traffic_endpoints_failed_marks_fetch_not_ok(self, mock_safe_get):
        from gitlytics.core import get_repo_traffic
        mock_safe_get.return_value = ({}, 403)
        traffic = get_repo_traffic("token", "owner/repo", metrics=["views", "clones"])
        assert traffic["_fetch_ok"] is False
        assert traffic["_views_ok"] is False
        assert traffic["_clones_ok"] is False

    @patch("gitlytics.core._safe_get")
    def test_failed_fetch_produces_no_tidy_rows(self, mock_safe_get):
        from gitlytics.core import get_repo_traffic, build_tidy_rows
        mock_safe_get.return_value = ({}, 429)
        repo = {"full_name": "owner/repo", "private": False,
                "stargazers_count": 1, "forks_count": 0}
        traffic = get_repo_traffic("token", "owner/repo", metrics=["views", "clones"])
        assert build_tidy_rows(repo, traffic) == []

    @patch("gitlytics.core._safe_get")
    def test_partial_fetch_keeps_successful_metric_only(self, mock_safe_get):
        from gitlytics.core import get_repo_traffic, build_tidy_rows

        def side_effect(url, headers, params=None):
            if url.endswith("/traffic/views"):
                return {}, 403
            if url.endswith("/traffic/clones"):
                return {"clones": [{"timestamp": "2025-06-14T00:00:00Z", "count": 7, "uniques": 3}]}, 200
            if url.endswith("/traffic/popular/referrers"):
                return [], 200
            if url.endswith("/traffic/popular/paths"):
                return [], 200
            return 0, 200

        mock_safe_get.side_effect = side_effect
        repo = {"full_name": "owner/repo", "private": False,
                "stargazers_count": 1, "forks_count": 0}
        traffic = get_repo_traffic("token", "owner/repo", metrics=["views", "clones"])
        assert traffic["_fetch_ok"] is True
        rows = build_tidy_rows(repo, traffic)
        assert len(rows) == 15
        assert all("views" not in row for row in rows)
        day = next(r for r in rows if r["date"] == "2025-06-14")
        assert day["clones"] == 7

    @patch("gitlytics.core._http_get")
    @patch("gitlytics.core.time.sleep", return_value=None)
    def test_transient_5xx_is_retried(self, mock_sleep, mock_get):
        from gitlytics.core import _safe_get
        first = MagicMock(status_code=503, headers={})
        second = MagicMock(status_code=200, headers={})
        second.json.return_value = {"ok": True}
        mock_get.side_effect = [first, second]
        data, status = _safe_get("https://api.github.com/test", {})
        assert data == {"ok": True}
        assert status == 200
        assert mock_get.call_count == 2

    @patch("gitlytics.core.get_all_repos")
    @patch("gitlytics.core.get_repo_traffic")
    def test_repos_are_fetched_concurrently(self, mock_traffic, mock_repos):
        import threading
        import time as _time
        from gitlytics.core import fetch_traffic_data
        mock_repos.return_value = [
            {"full_name": f"owner/repo{i}", "private": False,
             "stargazers_count": 0, "forks_count": 0}
            for i in range(4)
        ]
        state = {"active": 0, "max": 0}
        lock = threading.Lock()

        def fake_traffic(token, full_name, metrics=None):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            _time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return {"views": {"views": []}, "clones": {"clones": []},
                    "referrers": [], "paths": [], "releases": 0}

        mock_traffic.side_effect = fake_traffic
        df = fetch_traffic_data("token")
        assert len(df) == 60  # 4 repos x 15 days
        assert state["max"] > 1
