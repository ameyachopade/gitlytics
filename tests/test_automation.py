"""
tests/test_automation.py
Unit tests for gitlytics/automation.py — CSV sync, deduplication, and schema migration.
All network calls are mocked so these tests run completely offline.
"""
import csv
import os
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from gitlytics.automation import run_sync_cycle, get_csv_path, _merge_schema, run_sync, TokenExpiredError


# ── Helper — build a minimal tidy-data DataFrame ─────────────────────────────

def _make_df(repo="user/repo", date="2025-06-14", views=5, clones=3):
    """Builds a one-row DataFrame that looks exactly like what fetch_traffic_data returns."""
    return pd.DataFrame([{
        "date": date,
        "repository": repo,
        "is_private": False,
        "views": views,
        "unique_visitors": 2,
        "clones": clones,
        "unique_cloners": 1,
        "stars": 10,
        "forks": 2,
        "top_referrer": "",
        "top_referrer_views": 0,
        "top_referrer_uniques": 0,
        "top_path": "",
        "top_path_views": 0,
        "top_path_uniques": 0,
        "_raw_referrers": "[]",
        "_raw_paths": "[]",
    }])


# ── get_csv_path ──────────────────────────────────────────────────────────────

class TestGetCsvPath:
    def test_creates_data_dir(self, tmp_path):
        # The data directory should be created automatically if it doesn't exist yet
        target = tmp_path / "new_dir"
        get_csv_path(str(target), "monthly")
        assert target.exists()

    def test_monthly_mode_filename_format(self, tmp_path):
        # Monthly mode must produce a traffic_YYYY-MM.csv filename
        path = get_csv_path(str(tmp_path), "monthly")
        assert "traffic_" in path
        assert path.endswith(".csv")
        filename = os.path.basename(path)
        assert len(filename) == len("traffic_YYYY-MM.csv")

    def test_yearly_mode_filename_format(self, tmp_path):
        # Yearly mode must produce a traffic_YYYY.csv filename with a 4-digit year
        path = get_csv_path(str(tmp_path), "yearly")
        filename = os.path.basename(path)
        assert filename.startswith("traffic_")
        assert filename.endswith(".csv")
        year_part = filename[len("traffic_"):-len(".csv")]
        assert len(year_part) == 4 and year_part.isdigit()


# ── _merge_schema ─────────────────────────────────────────────────────────────

class TestMergeSchema:
    def test_preserves_existing_order(self):
        # Old columns should stay in their original position — don't shuffle the schema
        existing = ["date", "repository", "views"]
        new = ["date", "repository", "views", "stars"]
        merged = _merge_schema(existing, new)
        assert merged[:3] == ["date", "repository", "views"]

    def test_appends_new_columns(self):
        # New columns added by the API should be appended to the end
        existing = ["date", "repository"]
        new = ["date", "repository", "stars"]
        merged = _merge_schema(existing, new)
        assert "stars" in merged

    def test_no_duplicates(self):
        # If the same column appears in both old and new, it should only appear once
        existing = ["date", "repository"]
        new = ["date", "repository", "views"]
        merged = _merge_schema(existing, new)
        assert len(merged) == len(set(merged))

    def test_identical_schemas_unchanged(self):
        # If old and new schemas match exactly, the result should be identical
        schema = ["date", "repository", "views"]
        merged = _merge_schema(schema, schema)
        assert merged == schema


# ── run_sync_cycle ────────────────────────────────────────────────────────────

class TestRunSyncCycle:
    @patch("gitlytics.automation.fetch_traffic_data")
    def test_creates_csv_on_first_run(self, mock_fetch, tmp_path):
        # First time sync runs, it should create a brand-new CSV file
        mock_fetch.return_value = _make_df()
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))
        csv_files = list(tmp_path.glob("traffic_*.csv"))
        assert len(csv_files) == 1

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_csv_has_correct_fields(self, mock_fetch, tmp_path):
        # The CSV must have all expected column headers and correct values
        mock_fetch.return_value = _make_df()
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))
        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["date"] == "2025-06-14"
        assert rows[0]["views"] == "5"
        assert rows[0]["unique_cloners"] == "1"

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_does_not_duplicate_same_day(self, mock_fetch, tmp_path):
        # Running sync twice with the same data should not create duplicate rows
        df = _make_df()
        mock_fetch.return_value = df

        run_sync_cycle("dummy_token", data_dir=str(tmp_path))
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        with open(csv_file, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1  # Still 1 row, not 2

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_appends_new_day(self, mock_fetch, tmp_path):
        # A sync run on a new date should add a second row alongside the first
        mock_fetch.return_value = _make_df(date="2025-06-14")
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        mock_fetch.return_value = _make_df(date="2025-06-15")
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        with open(csv_file, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_empty_dataframe_does_not_create_file(self, mock_fetch, tmp_path):
        # If the token has no repos, no CSV should be written at all
        mock_fetch.return_value = pd.DataFrame()
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))
        csv_files = list(tmp_path.glob("traffic_*.csv"))
        assert len(csv_files) == 0

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_schema_migration_adds_new_columns(self, mock_fetch, tmp_path):
        # When the API adds a new column, old rows in the CSV should get it with an empty value
        # First run: write CSV without 'new_col'
        mock_fetch.return_value = _make_df()
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        # Second run: DataFrame gains a new column that didn't exist before
        df_new = _make_df(date="2025-06-15")
        df_new["new_col"] = "hello"
        mock_fetch.return_value = df_new
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        with open(csv_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # The old row (date 2025-06-14) should now have the new column too
        assert "new_col" in rows[0]
        assert len(rows) == 2

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_subset_sync_preserves_existing_columns(self, mock_fetch, tmp_path):
        # First run: full sync — all columns written
        mock_fetch.return_value = _make_df(views=10, clones=5)
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        # Second run: subset sync — only 'views' present in new DataFrame
        subset_df = pd.DataFrame([{
            "date": "2025-06-14",
            "repository": "user/repo",
            "views": 99,
        }])
        mock_fetch.return_value = subset_df
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        with open(csv_file, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        # views must be updated; clones must NOT be erased
        assert rows[0]["views"] == "99"
        assert rows[0]["clones"] == "5"

    @patch("gitlytics.automation.fetch_traffic_data")
    def test_uppercase_header_csv_is_accepted(self, mock_fetch, tmp_path):
        # Bug fix: column comparison should be case-insensitive so an exported
        # CSV with 'Date'/'Repository' headers is migrated correctly.
        # First run: write CSV with the standard lowercase headers
        mock_fetch.return_value = _make_df()
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        # Manually rewrite the CSV with TITLE-CASE headers to simulate an
        # export from an older version of the library (or hand-edited file).
        csv_file = list(tmp_path.glob("traffic_*.csv"))[0]
        content = csv_file.read_text(encoding="utf-8")
        title_cased = (
            content.replace("date", "Date")
                   .replace("repository", "Repository")
                   .replace("views", "Views")
        )
        csv_file.write_text(title_cased, encoding="utf-8")

        # Second run should NOT crash; the schema should be normalised internally.
        mock_fetch.return_value = _make_df(date="2025-06-15")
        run_sync_cycle("dummy_token", data_dir=str(tmp_path))

        rows = list(csv.DictReader(open(csv_file, "r", encoding="utf-8")))
        assert len(rows) == 2  # both rows preserved


# -- LIB-01/LIB-06 regression: zero rows and uploads must not poison exports ---

class TestExportSafety:
    @patch("gitlytics.automation.fetch_traffic_data")
    def test_zero_rows_do_not_overwrite_existing_history(self, mock_fetch, tmp_path):
        mock_fetch.return_value = _make_df(views=9, clones=4)
        run_sync_cycle("token", data_dir=str(tmp_path))
        csv_file = next(tmp_path.glob("traffic_*.csv"))

        # A failed/partial refresh that yields explicit zeros must not erase data.
        mock_fetch.return_value = _make_df(views=0, clones=0)
        run_sync_cycle("token", data_dir=str(tmp_path))

        with open(csv_file, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["views"] == "9"
        assert rows[0]["clones"] == "4"

    def test_uploaded_csv_is_excluded_from_export(self, tmp_path):
        from gitlytics.automation import export_json_database
        real = tmp_path / "traffic_2026-11.csv"
        real.write_text("date,repository,is_private,views\n2026-11-01,user/real,False,5\n", encoding="utf-8")
        uploaded = tmp_path / "traffic_uploaded_deadbeef.csv"
        uploaded.write_text("date,repository,is_private,views\n2026-11-01,attacker/poison,False,999\n", encoding="utf-8")
        export_file = tmp_path / "out.json"
        export_json_database(str(tmp_path), str(export_file), export_public_only=True)

        import json
        payload = json.loads(export_file.read_text(encoding="utf-8"))
        names = [repo["repository"] for repo in payload]
        assert names == ["user/real"]

    def test_concurrent_merges_do_not_lose_rows(self, tmp_path):
        import threading
        from gitlytics.automation import _write_df_to_csv
        csv_path = str(tmp_path / "traffic_2026-11.csv")
        errors = []

        def worker(repo):
            try:
                _write_df_to_csv(_make_df(repo=repo), csv_path)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(f"user/repo{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 6
        assert {row["repository"] for row in rows} == {f"user/repo{i}" for i in range(6)}


# -- LIB-10 regression: library code raises instead of exiting the process ---

class TestRunSyncTokenExpiry:
    @patch("gitlytics.automation._interruptible_sleep")
    @patch("gitlytics.automation.validate_token")
    def test_auth_failure_raises_token_expired_error(self, mock_validate, mock_sleep):
        mock_validate.return_value = (False, "401 Bad credentials")
        with pytest.raises(TokenExpiredError, match="expired, revoked"):
            run_sync("bad-token", schedule_cron="0 0 * * *")
        mock_sleep.assert_called_once()

    @patch("gitlytics.automation._interruptible_sleep")
    @patch("gitlytics.automation.validate_token")
    def test_network_blip_does_not_raise(self, mock_validate, mock_sleep):
        # A non-auth validation failure retries next cycle; stop the loop after
        # the first observation by making the second call terminal.
        mock_validate.side_effect = [
            (False, "Connection timed out"),
            (False, "401 Bad credentials"),
        ]
        with pytest.raises(TokenExpiredError):
            run_sync("flaky-token", schedule_cron="0 0 * * *")
        assert mock_validate.call_count == 2
