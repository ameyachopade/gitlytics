"""
tests/test_init.py
Unit tests for gitlytics/__init__.py public entrypoints:
  - _coerce_metrics
  - _write_json_safely
  - fetch_traffic
  - sync
  - serve_dashboard
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd

import gitlytics
from gitlytics import (
    _coerce_metrics,
    _write_json_safely,
    fetch_traffic,
    sync,
    serve_dashboard,
    GitHubRateLimitError,
    StarHistoryFetchError,
    StargazersRestrictedError,
)


# ── _coerce_metrics ──────────────────────────────────────────────────────────

class TestCoerceMetrics:
    def test_none_returns_none(self):
        assert _coerce_metrics(None) is None

    def test_string_raises_value_error(self):
        with pytest.raises(ValueError, match="must be a list/tuple/set of strings"):
            _coerce_metrics("views clones")

    def test_non_iterable_raises_value_error(self):
        with pytest.raises(ValueError, match="must be a list/tuple/set"):
            _coerce_metrics(123)

    def test_non_string_entry_raises_value_error(self):
        with pytest.raises(ValueError, match="entries must be strings"):
            _coerce_metrics(["views", 123])

    def test_valid_metrics_list(self):
        assert _coerce_metrics(["views", "clones"]) == ["views", "clones"]

    def test_tuple_and_set_coerced_to_list(self):
        res = _coerce_metrics(("views", "stars"))
        assert isinstance(res, list)
        assert set(res) == {"views", "stars"}

    def test_unknown_metrics_dropped(self):
        res = _coerce_metrics(["views", "invalid_metric", "clones"])
        assert res == ["views", "clones"]


# ── _write_json_safely ───────────────────────────────────────────────────────

class TestWriteJsonSafely:
    def test_writes_to_nested_path(self, tmp_path):
        target = tmp_path / "subdir" / "nested" / "output.json"
        payload = {"status": "ok", "count": 42}
        _write_json_safely(str(target), payload)

        assert target.exists()
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data == payload


# ── fetch_traffic ────────────────────────────────────────────────────────────

class TestFetchTraffic:
    def _sample_df(self):
        return pd.DataFrame([
            {
                "date": "2026-06-01",
                "repository": "owner/repo",
                "is_private": False,
                "views": 10,
                "unique_visitors": 5,
                "clones": 4,
                "unique_cloners": 2,
                "stars": 15,
                "forks": 3,
                "top_referrer": "google.com",
                "top_referrer_views": 8,
                "top_referrer_uniques": 4,
                "top_path": "/README.md",
                "top_path_views": 6,
                "top_path_uniques": 3,
                "_raw_referrers": "[]",
                "_raw_paths": "[]",
            }
        ])

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_dataframe(self, mock_fetch):
        mock_fetch.return_value = self._sample_df()
        res = fetch_traffic("ghp_fake", return_format="dataframe")
        assert isinstance(res, pd.DataFrame)
        assert len(res) == 1

    @patch("gitlytics.fetch_traffic_data")
    @patch("gitlytics.print_repo_table")
    def test_print_table_invoked(self, mock_print, mock_fetch):
        mock_fetch.return_value = self._sample_df()
        fetch_traffic("ghp_fake", print_table=True)
        mock_print.assert_called_once()

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_dataframe_save_csv(self, mock_fetch, tmp_path):
        mock_fetch.return_value = self._sample_df()
        csv_file = str(tmp_path / "out.csv")
        res = fetch_traffic("ghp_fake", return_format="dataframe", save_file=csv_file)
        assert isinstance(res, pd.DataFrame)
        assert Path(csv_file).exists()

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_dataframe_save_json(self, mock_fetch, tmp_path):
        mock_fetch.return_value = self._sample_df()
        json_file = str(tmp_path / "out.json")
        res = fetch_traffic("ghp_fake", return_format="dataframe", save_file=json_file)
        assert isinstance(res, pd.DataFrame)
        assert Path(json_file).exists()
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "account_totals" in data

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_timeseries(self, mock_fetch):
        mock_fetch.return_value = self._sample_df()
        res = fetch_traffic("ghp_fake", return_format="timeseries")
        assert isinstance(res, dict)
        assert "account_totals" in res
        assert "repositories" in res

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_timeseries_save_json(self, mock_fetch, tmp_path):
        mock_fetch.return_value = self._sample_df()
        json_file = str(tmp_path / "timeseries.json")
        res = fetch_traffic("ghp_fake", return_format="timeseries", save_file=json_file)
        assert isinstance(res, dict)
        assert Path(json_file).exists()

    @patch("gitlytics.fetch_traffic_data")
    def test_return_format_summary(self, mock_fetch):
        mock_fetch.return_value = self._sample_df()
        res = fetch_traffic("ghp_fake", return_format="summary")
        assert isinstance(res, dict)
        assert "account_totals" in res

    def test_invalid_return_format_raises(self):
        with patch("gitlytics.fetch_traffic_data", return_value=self._sample_df()):
            with pytest.raises(ValueError, match="Invalid return_format"):
                fetch_traffic("ghp_fake", return_format="invalid_fmt")


# ── sync ─────────────────────────────────────────────────────────────────────

class TestSync:
    @patch("gitlytics.run_sync")
    def test_sync_delegates_to_run_sync(self, mock_run_sync, tmp_path):
        sync(
            token="ghp_fake",
            repo_name="owner/repo",
            data_dir=str(tmp_path),
            output_mode="yearly",
            schedule_cron=None,
            export_json=None,
            export_public_only=True,
            metrics=["views"]
        )
        mock_run_sync.assert_called_once()
        _, kwargs = mock_run_sync.call_args
        assert kwargs["token"] == "ghp_fake"
        assert kwargs["repo_names"] == "owner/repo"
        assert kwargs["output_mode"] == "yearly"
        assert kwargs["metrics"] == ["views"]


# ── serve_dashboard ──────────────────────────────────────────────────────────

class TestServeDashboard:
    @patch("uvicorn.run")
    def test_serve_dashboard_runs_uvicorn(self, mock_uvicorn_run, tmp_path):
        test_dir = str(tmp_path / "data")
        os.makedirs(test_dir, exist_ok=True)
        # Create a dummy CSV so no warning
        (Path(test_dir) / "traffic_2026-06.csv").write_text("date,repository\n2026-06-01,owner/repo\n")

        serve_dashboard(host="127.0.0.1", port=9999, token="ghp_dash", data_dir=test_dir)
        mock_uvicorn_run.assert_called_once_with(
            "gitlytics.api:app", host="127.0.0.1", port=9999, reload=False, log_level="warning"
        )
        # Verify env vars were cleaned up
        assert os.environ.get("GITLYTICS_TOKEN") != "ghp_dash"

    def test_serve_dashboard_warns_on_missing_dir(self, capsys):
        with patch("uvicorn.run"):
            serve_dashboard(host="127.0.0.1", port=9999, data_dir="nonexistent_xyz_data_dir_123")
            captured = capsys.readouterr()
            assert "Warning" in captured.out

