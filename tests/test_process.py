"""
tests/test_process.py
Unit tests for gitlytics/process.py — data transformation and payload building.
All tests are fully offline (no GitHub API calls, no token needed).

Run with:
    python -m pytest tests/test_process.py -v
"""
import io
import json
import numpy as np
import pytest
import pandas as pd

from gitlytics.process import (
    build_json_payload,
    build_react_payload,
    process_uploaded_csv,
    _parse_raw,
    _to_bool,
)


# ── Shared test data ───────────────────────────────────────────────────────────

def _make_df(
    repo="user/repo",
    date="2025-06-14",
    views=10,
    clones=5,
    stars=20,
    forks=3,
    is_private=False,
    referrer="github.com",
    path="/README.md",
):
    """Builds a minimal one-row tidy DataFrame — same shape as fetch_traffic_data returns."""
    return pd.DataFrame([{
        "date": date,
        "repository": repo,
        "is_private": is_private,
        "views": views,
        "unique_visitors": 4,
        "clones": clones,
        "unique_cloners": 2,
        "stars": stars,
        "forks": forks,
        "top_referrer": referrer,
        "top_referrer_views": 8,
        "top_referrer_uniques": 3,
        "top_path": path,
        "top_path_views": 6,
        "top_path_uniques": 2,
        "_raw_referrers": json.dumps([{"referrer": referrer, "count": 8, "uniques": 3}]),
        "_raw_paths":     json.dumps([{"path": path, "count": 6, "uniques": 2}]),
    }])


# ── _parse_raw ─────────────────────────────────────────────────────────────────

class TestParseRaw:
    def test_valid_json_string_returns_list(self):
        # A proper JSON array string should be decoded to a Python list
        result = _parse_raw('[{"referrer": "github.com", "count": 5}]')
        assert isinstance(result, list)
        assert result[0]["referrer"] == "github.com"

    def test_python_literal_returns_empty_list(self):
        result = _parse_raw("[{'referrer': 'google.com', 'count': 2}]")
        assert result == []

    def test_empty_string_returns_empty_list(self):
        # An empty string means "no data" — return [] not None
        assert _parse_raw("") == []

    def test_none_value_returns_empty_list(self):
        # None should never crash — just return []
        assert _parse_raw(None) == []

    def test_plain_list_passes_through(self):
        # If the caller already decoded it, pass it back unchanged
        data = [{"referrer": "github.com"}]
        assert _parse_raw(data) == data

    def test_completely_invalid_string_returns_empty_list(self):
        # Garbage input should return [] gracefully, not raise
        assert _parse_raw("not json at all!!!") == []

    def test_empty_json_array_returns_empty_list(self):
        # "[]" is valid JSON and should give back an empty Python list
        assert _parse_raw("[]") == []


# ── build_json_payload ─────────────────────────────────────────────────────────

class TestBuildJsonPayload:
    def test_empty_df_returns_empty_structure(self):
        # An empty DataFrame should produce an empty-but-valid result, not crash
        result = build_json_payload(pd.DataFrame())
        assert result == {"account_totals": {}, "repositories": {}}

    def test_timeseries_returns_required_keys(self):
        # The timeseries payload must have account_totals and repositories at the top level
        result = build_json_payload(_make_df(), return_format="timeseries")
        assert "account_totals" in result
        assert "repositories" in result

    def test_timeseries_repo_has_timeseries_and_totals(self):
        # Each repo in timeseries mode must have a daily timeseries array and a totals dict
        result = build_json_payload(_make_df(), return_format="timeseries")
        repo = result["repositories"]["user/repo"]
        assert "timeseries" in repo
        assert "totals" in repo
        assert isinstance(repo["timeseries"], list)

    def test_summary_returns_per_repo_totals(self):
        # Summary mode should give per-repo totals, not the full daily breakdown
        result = build_json_payload(_make_df(), return_format="summary")
        repo = result["repositories"]["user/repo"]
        assert "total_views" in repo
        assert "total_clones" in repo
        assert "stars" in repo

    def test_account_totals_correct_sum(self):
        # Account totals should correctly sum across all repos
        df = pd.concat([_make_df(views=10), _make_df(repo="user/repo2", views=20)])
        result = build_json_payload(df, return_format="timeseries")
        assert result["account_totals"]["total_views"] == 30

    def test_stars_not_inflated_across_rows(self):
        # Stars are a snapshot — summing them across 14 rows would give wrong numbers
        # build_json_payload should use .iloc[-1], not .sum()
        df = pd.concat([_make_df(stars=50)] * 14, ignore_index=True)  # 14 identical rows
        result = build_json_payload(df, return_format="timeseries")
        assert result["repositories"]["user/repo"]["totals"]["stars"] == 50  # not 700

    def test_export_public_only_strips_private_repos(self):
        # Private repos must NOT appear in the output when export_public_only=True
        df = pd.concat([
            _make_df(repo="user/public", is_private=False),
            _make_df(repo="user/private", is_private=True),
        ])
        result = build_json_payload(df, return_format="timeseries", export_public_only=True)
        assert "user/public" in result["repositories"]
        assert "user/private" not in result["repositories"]

    def test_export_public_only_false_includes_private(self):
        # When the flag is off, private repos should be included in the output
        df = pd.concat([
            _make_df(repo="user/public", is_private=False),
            _make_df(repo="user/private", is_private=True),
        ])
        result = build_json_payload(df, return_format="timeseries", export_public_only=False)
        assert "user/public" in result["repositories"]
        assert "user/private" in result["repositories"]

    def test_all_private_repos_returns_empty_when_public_only(self):
        # If every repo is private and public-only is on, result should be empty
        df = _make_df(is_private=True)
        result = build_json_payload(df, return_format="timeseries", export_public_only=True)
        assert result == {"account_totals": {}, "repositories": {}}

    def test_timeseries_dates_in_chronological_order(self):
        # Timeseries arrays must be oldest → newest so charts render correctly
        dates = ["2025-06-14", "2025-06-13", "2025-06-15"]
        rows = [_make_df(date=d) for d in dates]
        df = pd.concat(rows, ignore_index=True)
        result = build_json_payload(df, return_format="timeseries")
        ts = result["repositories"]["user/repo"]["timeseries"]
        ts_dates = [r["date"] for r in ts]
        assert ts_dates == sorted(ts_dates)


# ── build_react_payload ────────────────────────────────────────────────────────

class TestBuildReactPayload:
    def test_empty_df_returns_empty_list(self):
        # An empty DataFrame must return [] not crash or return None
        assert build_react_payload(pd.DataFrame()) == []

    def test_returns_list(self):
        # The result must always be a list of repo objects
        result = build_react_payload(_make_df())
        assert isinstance(result, list)
        assert len(result) == 1

    def test_each_repo_has_required_fields(self):
        # Every repo object needs these fields for the React dashboard to render
        REQUIRED = {
            "repository", "is_private", "stars", "forks",
            "views", "unique_visitors", "clones", "unique_cloners",
            "top_referrer", "top_path",
            "_daily_views", "_daily_clones", "_referrers", "_paths"
        }
        result = build_react_payload(_make_df())
        missing = REQUIRED - set(result[0].keys())
        assert not missing, f"Missing fields: {missing}"

    def test_daily_views_is_list_of_dicts(self):
        # _daily_views must be a list of {timestamp, count, uniques} objects
        result = build_react_payload(_make_df())
        daily = result[0]["_daily_views"]
        assert isinstance(daily, list)
        assert len(daily) >= 1
        assert "timestamp" in daily[0]
        assert "count" in daily[0]

    def test_referrers_decoded_from_raw_json(self):
        # _referrers should be the decoded list from _raw_referrers, not the raw string
        result = build_react_payload(_make_df(referrer="google.com"))
        refs = result[0]["_referrers"]
        assert isinstance(refs, list)
        assert any("google.com" in str(r) for r in refs)

    def test_views_are_summed_across_rows(self):
        # When a repo has multiple rows (multiple days), views should be summed
        df = pd.concat([
            _make_df(date="2025-06-14", views=10),
            _make_df(date="2025-06-15", views=15),
        ], ignore_index=True)
        result = build_react_payload(df)
        assert result[0]["views"] == 25

    def test_stars_not_summed(self):
        # Stars are a point-in-time snapshot — must use the last row, not sum
        df = pd.concat([_make_df(stars=100)] * 5, ignore_index=True)
        result = build_react_payload(df)
        assert result[0]["stars"] == 100  # not 500

    def test_multiple_repos_returns_multiple_items(self):
        # Multiple distinct repos should produce one list item each
        df = pd.concat([
            _make_df(repo="user/repo1"),
            _make_df(repo="user/repo2"),
        ], ignore_index=True)
        result = build_react_payload(df)
        assert len(result) == 2
        names = {r["repository"] for r in result}
        assert names == {"user/repo1", "user/repo2"}

    def test_fallback_to_summary_columns_when_raw_missing(self):
        # If _raw_referrers column is absent, fall back to top_referrer column
        df = _make_df(referrer="fallback.com")
        df = df.drop(columns=["_raw_referrers", "_raw_paths"])
        result = build_react_payload(df)
        refs = result[0]["_referrers"]
        assert isinstance(refs, list)
        assert len(refs) >= 1
        assert refs[0]["referrer"] == "fallback.com"


# ── process_uploaded_csv ───────────────────────────────────────────────────────

class TestProcessUploadedCsv:
    def _make_csv_bytes(self, columns: dict) -> io.BytesIO:
        """Helper: creates a CSV BytesIO with the given column values."""
        headers = list(columns.keys())
        row = ",".join(str(v) for v in columns.values())
        content = ",".join(headers) + "\n" + row + "\n"
        return io.BytesIO(content.encode("utf-8"))

    def test_native_format_passes_through(self):
        # Our own CSV format (with 'repository' column) should work without any renaming
        csv_file = self._make_csv_bytes({
            "date": "2025-06-14",
            "repository": "user/repo",
            "views": 10,
        })
        result = process_uploaded_csv(csv_file)
        assert "repository" in result.columns

    def test_repo_name_column_renamed(self):
        # Old 'repo_name' column (from pre-rebranding) should be renamed to 'repository'
        csv_file = self._make_csv_bytes({
            "date": "2025-06-14",
            "repo_name": "user/repo",
            "views": 10,
        })
        result = process_uploaded_csv(csv_file)
        assert "repository" in result.columns
        assert "repo_name" not in result.columns

    def test_title_case_columns_renamed(self):
        # Title-case columns from old export format should be normalised to lowercase
        csv_file = self._make_csv_bytes({
            "Repository": "user/repo",
            "date": "2025-06-14",
            "Total Views": 10,
            "Unique Visitors": 4,
            "Total Clones": 3,
            "Unique Cloners": 1,
            "Stars": 20,
            "Forks": 5,
        })
        result = process_uploaded_csv(csv_file)
        assert "repository" in result.columns
        # Multi-word legacy columns must be mapped to the canonical names
        # that build_react_payload expects — otherwise views/clones silently
        # default to 0 in the dashboard.
        assert "views" in result.columns
        assert "unique_visitors" in result.columns
        assert "clones" in result.columns
        assert "unique_cloners" in result.columns
        assert "stars" in result.columns
        assert "forks" in result.columns
        # Verify data values are preserved through the rename
        assert result["views"].iloc[0] == 10
        assert result["unique_visitors"].iloc[0] == 4
        assert result["clones"].iloc[0] == 3
        assert result["unique_cloners"].iloc[0] == 1
        assert result["stars"].iloc[0] == 20
        assert result["forks"].iloc[0] == 5

    def test_missing_repository_column_raises_value_error(self):
        # A CSV with no recognisable repository column must raise a clear error
        csv_file = self._make_csv_bytes({
            "date": "2025-06-14",
            "some_random_col": "value",
        })
        with pytest.raises(ValueError, match="repository"):
            process_uploaded_csv(csv_file)

    def test_is_private_string_bools_are_coerced(self):
        # Regression for the `bool("False") → True` bug: a CSV that
        # was roundtripped through disk write+read comes back with
        # `is_private` as strings ("True"/"False") rather than real
        # bools. process_uploaded_csv must coerce these to a real bool
        # column so downstream consumers (e.g. build_react_payload)
        # don't silently mislabel public repos as private.
        csv_file = self._make_csv_bytes({
            "date": "2025-06-14",
            "repository": "user/repo",
            "is_private": "False",
        })
        result = process_uploaded_csv(csv_file)
        assert result["is_private"].dtype == bool
        assert bool(result["is_private"].iloc[0]) is False

    def test_is_private_numeric_coerced(self):
        # Same regression — many external CSV exporters write 1/0
        # rather than True/False; both must round-trip correctly.
        csv_file = self._make_csv_bytes({
            "date": "2025-06-14",
            "repository": "user/private",
            "is_private": "1",
        })
        result = process_uploaded_csv(csv_file)
        assert bool(result["is_private"].iloc[0]) is True


    def test_returns_dataframe(self):
        # The function must always return a pandas DataFrame
        csv_file = self._make_csv_bytes({
            "repository": "user/repo",
            "date": "2025-06-14",
            "views": 5,
        })
        result = process_uploaded_csv(csv_file)
        assert isinstance(result, pd.DataFrame)

    def test_data_values_preserved(self):
        # After normalising column names, the actual data values must be unchanged
        csv_file = self._make_csv_bytes({
            "repository": "user/repo",
            "date": "2025-06-14",
            "views": 42,
        })
        result = process_uploaded_csv(csv_file)
        assert result["views"].iloc[0] == 42

    def test_capitalized_date_column_accepted(self):
        # A CSV with 'Date' (capital D) should be normalised and accepted
        csv_file = self._make_csv_bytes({
            "repository": "user/repo",
            "Date": "2025-06-14",
            "views": 10,
        })
        result = process_uploaded_csv(csv_file)
        assert "date" in result.columns
        assert "Date" not in result.columns

    def test_none_values_in_metrics_do_not_crash_payload(self):
        # A DataFrame with None metric values must not crash build_react_payload
        from gitlytics.process import build_react_payload
        import pandas as pd
        df = pd.DataFrame([{
            "date": "2025-06-14",
            "repository": "user/repo",
            "is_private": False,
            "views": None,
            "unique_visitors": None,
            "clones": None,
            "unique_cloners": None,
            "stars": None,
            "forks": None,
        }])
        result = build_react_payload(df)
        assert isinstance(result, list)
        assert result[0]["views"] == 0
        assert result[0]["clones"] == 0

    def test_to_bool_recognizes_numpy_bool(self):
        # LIB-07: numpy.bool_ is not a subclass of Python bool and previously
        # fell through to the string/None path, misclassifying private repos.
        assert _to_bool(np.bool_(True)) is True
        assert _to_bool(np.bool_(False)) is False
