"""
tests/test_audit_final2_lib_regressions.py
Regression tests for the Audit Final 2 core library & CLI findings.

Covers:
  AF2-LIB-02  legacy/title-case CSV headers crash /api/traffic with KeyError
  AF2-LIB-03  GitHub 5xx on a pinned repo raises a raw traceback in the CLI
  AF2-LIB-04  `gitlytics sync` exits 0 and writes nothing on zero rows
  AF2-LIB-05  the stargazer "restricted" negative cache is shared by all tokens
  AF2-LIB-06  the "offline" function harness performs a live GitHub API call
  AF2-LIB-07  serve_dashboard() silently discards a pre-existing GITLYTICS_TOKEN
  AF2-LIB-08  run_sync_cycle silently deletes repositories on a date mismatch
  AF2-LIB-09  all CLI diagnostics are written to stdout instead of stderr
  AF2-LIB-10  `python -m gitlytics --help` reports the program as __main__.py
  AF2-LIB-11  the licence uses the deprecated PEP 621 table form
  AF2-LIB-12  a CI regression test hardcodes the version string
  AF2-LIB-13  cross-repo version/contract tests are silently skipped
  AF2-LIB-14  rate-limit backoff blocks ~2 minutes with no output or deadline
  AF2-LIB-16  the README advertises a card/badge engine the package lacks
  AF2-LIB-17  the packaged Content-Security-Policy is never applied locally

Every test here is fully offline — all GitHub calls are mocked.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from gitlytics import core
from gitlytics.core import GitHubAPIError

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _resp(status: int, *, body=None, headers=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = text
    return r


# ── AF2-LIB-02: legacy / title-case CSV headers must not 500 the dashboard ──

class TestLib02LegacyCsvHeaders:
    def test_read_csv_cached_normalises_title_case_headers(self, tmp_path):
        from gitlytics.api import _read_csv_cached

        csv = tmp_path / "traffic_2026-01.csv"
        csv.write_text(
            "Date,Repository,Views\n2026-01-01,owner/repo,10\n",
            encoding="utf-8",
        )
        df = _read_csv_cached(csv)
        assert list(df.columns) == ["date", "repository", "views"]
        assert df["repository"].iloc[0] == "owner/repo"

    def test_read_csv_cached_strips_bom_and_drops_duplicate_columns(self, tmp_path):
        from gitlytics.api import _read_csv_cached

        csv = tmp_path / "traffic_2026-01.csv"
        csv.write_text(
            "﻿Date,date,Repository,Views\n2026-01-01,,owner/repo,10\n",
            encoding="utf-8",
        )
        df = _read_csv_cached(csv)
        assert not df.columns.duplicated().any()
        assert "date" in df.columns and "repository" in df.columns

    def test_traffic_endpoint_survives_a_title_case_csv(self, tmp_path):
        """A legacy CSV must not take the whole dashboard down with a 500."""
        from fastapi.testclient import TestClient
        from gitlytics import api

        (tmp_path / "traffic_2026-01.csv").write_text(
            "Date,Repository,Is_Private,Views\n"
            "2026-01-01,owner/repo,False,10\n"
            "2026-01-02,owner/repo,False,12\n",
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False), \
             patch("gitlytics.api._validate_token_cached", return_value=(True, "u")), \
             patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame()), \
             patch("gitlytics.api.fetch_deep_stats_for_top", return_value={}):
            response = TestClient(api.app).post("/api/traffic", json={"token": "t"})

        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        assert payload[0]["repository"] == "owner/repo"
        assert payload[0]["views"] == 22

    def test_build_react_payload_without_repository_column_is_empty(self):
        """A frame with no grouping key must not raise KeyError."""
        from gitlytics.process import build_react_payload

        frame = pd.DataFrame([{"date": "2026-01-01", "views": 5}])
        assert build_react_payload(frame) == []

    def test_csv_without_required_columns_is_skipped_not_fatal(self, tmp_path):
        """A junk CSV in the data dir is skipped with a warning, not a 500."""
        from fastapi.testclient import TestClient
        from gitlytics import api

        (tmp_path / "traffic_2026-01.csv").write_text(
            "foo,bar\n1,2\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False), \
             patch("gitlytics.api._validate_token_cached", return_value=(True, "u")), \
             patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame()), \
             patch("gitlytics.api.fetch_deep_stats_for_top", return_value={}):
            response = TestClient(api.app).post("/api/traffic", json={"token": "t"})

        assert response.status_code == 200
        assert response.json() == []


# ── AF2-LIB-03: a GitHub 5xx must not produce a raw traceback ──────────────

class TestLib03GitHubApiError:
    def test_get_single_repo_raises_github_api_error_on_5xx(self):
        from gitlytics.core import get_single_repo

        with patch("gitlytics.core._safe_get", return_value=({}, 500)):
            with pytest.raises(GitHubAPIError) as exc:
                get_single_repo("tok", "owner/repo")
        assert "owner/repo" in str(exc.value)

    def test_github_api_error_is_a_runtime_error(self):
        """Callers that already guard against RuntimeError keep working."""
        assert issubclass(GitHubAPIError, RuntimeError)

    def test_cli_fetch_maps_github_api_error_to_exit_1(self, capsys):
        from gitlytics import cli

        with patch(
            "gitlytics.cli.fetch_traffic",
            side_effect=GitHubAPIError("Failed to fetch repository 'owner/repo' (HTTP 500)."),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main(["fetch", "--token", "t", "--repo-name", "owner/repo"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out
        assert "Traceback" not in captured.err
        assert "HTTP 500" in captured.err

    def test_cli_sync_maps_github_api_error_to_exit_1(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.sync", side_effect=GitHubAPIError("upstream exploded")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["sync", "--token", "t"])
        assert exc.value.code == 1
        assert "upstream exploded" in capsys.readouterr().err

    def test_github_api_error_is_exported_from_the_package(self):
        import gitlytics

        assert "GitHubAPIError" in gitlytics.__all__
        assert gitlytics.GitHubAPIError is GitHubAPIError


# ── AF2-LIB-04: `gitlytics sync` must not report success on zero rows ──────

class TestLib04SyncZeroRows:
    def test_run_sync_cycle_reports_zero_rows(self, tmp_path):
        from gitlytics.automation import run_sync_cycle

        with patch("gitlytics.automation.fetch_traffic_data", return_value=pd.DataFrame()):
            rows = run_sync_cycle("tok", data_dir=str(tmp_path))
        assert rows == 0
        assert list(tmp_path.glob("traffic_*.csv")) == []

    def test_run_sync_cycle_reports_the_row_count(self, tmp_path):
        from gitlytics.automation import run_sync_cycle

        frame = pd.DataFrame([
            {"date": "2026-01-01", "repository": "owner/repo", "is_private": False, "views": 5},
            {"date": "2026-01-02", "repository": "owner/repo", "is_private": False, "views": 7},
        ])
        with patch("gitlytics.automation.fetch_traffic_data", return_value=frame):
            rows = run_sync_cycle("tok", data_dir=str(tmp_path))
        assert rows == 2

    def test_cli_sync_exits_1_when_nothing_was_fetched(self, capsys, tmp_path):
        from gitlytics import cli

        with patch("gitlytics.cli.sync", return_value=0):
            with pytest.raises(SystemExit) as exc:
                cli.main(["sync", "--token", "t", "--data-dir", str(tmp_path)])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "no traffic data returned" in captured.err
        assert "nothing was synced" in captured.err

    def test_cli_sync_succeeds_and_reports_the_count(self, capsys, tmp_path):
        from gitlytics import cli

        with patch("gitlytics.cli.sync", return_value=42):
            main_result = None
            try:
                cli.main(["sync", "--token", "t", "--data-dir", str(tmp_path)])
            except SystemExit as exc:  # pragma: no cover - must not exit
                main_result = exc.code
        assert main_result is None
        assert "42 row(s) synced." in capsys.readouterr().out

    def test_run_sync_raises_no_traffic_data_error_on_zero_rows(self, tmp_path):
        """The library-level sync must fail loudly, not return quietly."""
        from gitlytics.automation import NoTrafficDataError, run_sync

        with patch("gitlytics.automation.fetch_traffic_data", return_value=pd.DataFrame()):
            with pytest.raises(NoTrafficDataError):
                run_sync("tok", data_dir=str(tmp_path))

    def test_the_cli_maps_no_traffic_data_error_to_exit_1(self, capsys):
        from gitlytics import cli
        from gitlytics.automation import NoTrafficDataError

        with patch(
            "gitlytics.cli.sync",
            side_effect=NoTrafficDataError("No traffic data was returned by GitHub - nothing was synced."),
        ):
            with pytest.raises(SystemExit) as exc:
                cli.main(["sync", "--token", "t"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "nothing was synced" in captured.err
        assert captured.out == ""

    def test_no_traffic_data_error_is_exported_from_the_package(self):
        import gitlytics

        assert "NoTrafficDataError" in gitlytics.__all__


# ── AF2-LIB-05: the stargazer negative cache must be per-token ────────────

class TestLib05StargazerCacheIsPerToken:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        core._stargazer_restricted_cache.clear()
        yield
        core._stargazer_restricted_cache.clear()

    def test_cache_key_includes_a_token_fingerprint(self):
        key_a = core._stargazer_cache_key("o", "r", "token-a")
        key_b = core._stargazer_cache_key("o", "r", "token-b")
        assert key_a != key_b
        assert key_a[0] == "o" and key_a[1] == "r"

    def test_cache_key_never_contains_the_raw_token(self):
        key = core._stargazer_cache_key("o", "r", "ghp_supersecret")
        assert "ghp_supersecret" not in "".join(key)

    def test_anonymous_requests_share_one_key(self):
        assert core._stargazer_cache_key("o", "r") == core._stargazer_cache_key("o", "r", None)

    def test_one_users_404_does_not_poison_another_user(self):
        """A 404 for token A must not short-circuit token B's real timeline."""
        from gitlytics.core import fetch_star_history

        meta = {"stargazers_count": 2, "created_at": "2020-01-01T00:00:00Z"}
        stargazers_ok = _resp(200, body=[
            {"starred_at": "2024-01-01T00:00:00Z"},
            {"starred_at": "2024-02-01T00:00:00Z"},
        ])
        restricted = _resp(404)

        # Token A hits the restricted stargazer endpoint.
        with patch("gitlytics.core._safe_get", return_value=(meta, 200)), \
             patch("gitlytics.core._http_get", side_effect=[restricted]):
            fetch_star_history("o", "r", "token-a")
        # The approximation carries the repository creation date and today.
        with patch("gitlytics.core._safe_get", return_value=(meta, 200)), \
             patch("gitlytics.core._http_get", return_value=restricted):
            points_a = fetch_star_history("o", "r", "token-a")
        assert len(points_a) == 2  # the documented approximation

        # Token B is the repository owner: it must get the real timeline with
        # the actual star dates, not the fabricated approximation.
        with patch("gitlytics.core._safe_get", return_value=(meta, 200)), \
             patch("gitlytics.core._http_get", return_value=stargazers_ok):
            points_b = fetch_star_history("o", "r", "token-b")
        dates_b = {p["date"] for p in points_b}
        assert "2024-01-01" in dates_b, (
            "token B received the fabricated approximation instead of the real "
            f"timeline: {points_b}"
        )
        assert "2024-02-01" in dates_b

    def test_the_same_token_still_short_circuits(self):
        """The negative cache must still work for the token that hit the 404."""
        from gitlytics.core import fetch_star_history

        meta = {"stargazers_count": 2, "created_at": "2020-01-01T00:00:00Z"}
        restricted = _resp(404)

        with patch("gitlytics.core._safe_get", return_value=(meta, 200)), \
             patch("gitlytics.core._http_get", side_effect=[restricted]):
            fetch_star_history("o", "r", "token-a")

        # The second call must NOT issue another stargazer request.
        with patch("gitlytics.core._safe_get", return_value=(meta, 200)), \
             patch("gitlytics.core._http_get") as mock_get:
            fetch_star_history("o", "r", "token-a")
        assert mock_get.call_count == 0


# ── AF2-LIB-06: the "offline" harness must never hit the network ───────────

class TestLib06OfflineHarness:
    def test_harness_never_performs_a_live_request(self):
        """The harness must be offline: an unmocked request must fail loudly."""
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / "run_all_functions_check.py")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={**os.environ, "GITLYTICS_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        assert "ALL" in result.stdout
        assert "OFFLINE VIOLATION" not in result.stdout
        assert "OFFLINE VIOLATION" not in result.stderr

    def test_harness_mocks_the_link_header_count(self):
        """The releases count must be mocked exactly like the deep-stats test."""
        source = (ROOT / "tests" / "run_all_functions_check.py").read_text(
            encoding="utf-8"
        )
        assert "gitlytics.core._count_via_link_header" in source


# ── AF2-LIB-07: serve_dashboard() must not discard an env token ────────────

class TestLib07ServeDashboardKeepsEnvToken:
    def test_env_token_is_kept_on_loopback(self, capsys):
        import gitlytics

        with patch.dict(os.environ, {"GITLYTICS_TOKEN": "pre-existing-pat"}, clear=False):
            os.environ.pop("GITLYTICS_ALLOW_ENV_TOKEN", None)
            seen = {}

            def _fake_run(*args, **kwargs):
                seen["token"] = os.environ.get("GITLYTICS_TOKEN")
                seen["allowed"] = os.environ.get("GITLYTICS_ALLOW_ENV_TOKEN")

            with patch("uvicorn.run", side_effect=_fake_run):
                gitlytics.serve_dashboard(host="127.0.0.1", port=9999)

        assert seen["token"] == "pre-existing-pat"
        assert seen["allowed"] == "1"

    def test_explicit_token_still_wins_on_loopback(self):
        import gitlytics

        with patch.dict(os.environ, {"GITLYTICS_TOKEN": "env-pat"}, clear=False):
            seen = {}

            def _fake_run(*args, **kwargs):
                seen["token"] = os.environ.get("GITLYTICS_TOKEN")

            with patch("uvicorn.run", side_effect=_fake_run):
                gitlytics.serve_dashboard(host="127.0.0.1", port=9999, token="explicit")

        assert seen["token"] == "explicit"

    def test_non_loopback_drops_the_token_and_warns(self, capsys):
        import gitlytics

        with patch.dict(os.environ, {"GITLYTICS_TOKEN": "env-pat"}, clear=False):
            seen = {}

            def _fake_run(*args, **kwargs):
                seen["token"] = os.environ.get("GITLYTICS_TOKEN")

            with patch("uvicorn.run", side_effect=_fake_run):
                gitlytics.serve_dashboard(host="0.0.0.0", port=9999)

        assert seen["token"] is None
        assert "dropped" in capsys.readouterr().out

    def test_no_token_at_all_is_fine(self):
        import gitlytics

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLYTICS_TOKEN", None)
            seen = {}

            def _fake_run(*args, **kwargs):
                seen["token"] = os.environ.get("GITLYTICS_TOKEN")

            with patch("uvicorn.run", side_effect=_fake_run):
                gitlytics.serve_dashboard(host="127.0.0.1", port=9999)

        assert seen["token"] is None


# ── AF2-LIB-08: mixed date formats must not delete whole repositories ──────

class TestLib08MixedDateFormats:
    def test_parse_date_column_handles_mixed_formats(self):
        from gitlytics.automation import _parse_date_column

        series = pd.Series(["2026-11-01", "01/02/2026"])
        parsed = _parse_date_column(series)
        assert parsed.notna().all()
        assert parsed.iloc[0].strftime("%Y-%m-%d") == "2026-11-01"
        assert parsed.iloc[1].strftime("%Y-%m-%d") == "2026-01-02"

    def test_mixed_formats_do_not_silently_drop_rows(self, tmp_path):
        """Every parseable row must survive the monthly bucketing."""
        from gitlytics.automation import run_sync_cycle

        frame = pd.DataFrame([
            {"date": "2026-11-01", "repository": "owner/repo1", "is_private": False, "views": 10},
            {"date": "01/02/2026", "repository": "owner/repo2", "is_private": False, "views": 20},
        ])
        with patch("gitlytics.automation.fetch_traffic_data", return_value=frame):
            rows = run_sync_cycle("tok", data_dir=str(tmp_path))

        assert rows == 2
        written = []
        for csv in tmp_path.glob("traffic_*.csv"):
            written.extend(pd.read_csv(csv).to_dict("records"))
        assert {r["repository"] for r in written} == {"owner/repo1", "owner/repo2"}

    def test_unparseable_dates_raise_a_visible_error(self, tmp_path):
        """A genuinely malformed date must be an error, not a silent drop."""
        from gitlytics.automation import run_sync_cycle

        frame = pd.DataFrame([
            {"date": "2026-11-01", "repository": "owner/repo1", "is_private": False, "views": 10},
            {"date": "not-a-date", "repository": "owner/repo2", "is_private": False, "views": 20},
        ])
        with patch("gitlytics.automation.fetch_traffic_data", return_value=frame):
            with pytest.raises(ValueError) as exc:
                run_sync_cycle("tok", data_dir=str(tmp_path))

        message = str(exc.value)
        assert "unparseable date" in message
        assert "not-a-date" in message
        # Nothing was written — no repository silently disappeared.
        assert list(tmp_path.glob("traffic_*.csv")) == []

    def test_yearly_mode_also_uses_the_defensive_parser(self, tmp_path):
        from gitlytics.automation import run_sync_cycle

        frame = pd.DataFrame([
            {"date": "2026-01-01", "repository": "owner/repo1", "is_private": False, "views": 10},
            {"date": "01/02/2026", "repository": "owner/repo2", "is_private": False, "views": 20},
        ])
        with patch("gitlytics.automation.fetch_traffic_data", return_value=frame):
            rows = run_sync_cycle("tok", data_dir=str(tmp_path), output_mode="yearly")

        assert rows == 2
        written = []
        for csv in tmp_path.glob("traffic_*.csv"):
            written.extend(pd.read_csv(csv).to_dict("records"))
        assert {r["repository"] for r in written} == {"owner/repo1", "owner/repo2"}


# ── AF2-LIB-09: every CLI diagnostic belongs on stderr ────────────────────

class TestLib09CliDiagnosticsGoToStderr:
    def test_missing_token_error_goes_to_stderr(self, capsys):
        from gitlytics import cli

        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                cli.main(["fetch"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "No GitHub token provided" in captured.err
        assert captured.out == ""

    def test_invalid_repo_name_error_goes_to_stderr(self, capsys):
        from gitlytics import cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["fetch", "--token", "t", "--repo-name", "no-slash"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "❌ Error" in captured.err
        assert captured.out == ""

    def test_fetch_empty_result_error_goes_to_stderr(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.fetch_traffic", return_value=pd.DataFrame()):
            with pytest.raises(SystemExit) as exc:
                cli.main(["fetch", "--token", "t", "--repo-name", "owner/repo"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "no traffic data returned" in captured.err
        assert captured.out == ""

    def test_dashboard_public_bind_warning_goes_to_stderr(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.serve_dashboard"):
            with pytest.raises(SystemExit) as exc:
                cli.main(["dashboard", "--host", "0.0.0.0"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "❌ Error" in captured.err
        assert captured.out == ""

    def test_the_dashboard_startup_banner_stays_on_stdout(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.serve_dashboard"):
            cli.main(["dashboard"])
        captured = capsys.readouterr()
        assert "Starting Gitlytics Dashboard" in captured.out

    def test_dashboard_warnings_are_written_to_stderr(self, capsys):
        """The dashboard's own warnings must not pollute stdout."""
        import gitlytics

        with patch("uvicorn.run"):
            gitlytics.serve_dashboard(
                host="127.0.0.1",
                port=9999,
                data_dir="nonexistent_xyz_data_dir_123",
                stream=sys.stderr,
            )
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert captured.out == ""

    def test_stars_errors_go_to_stderr(self, capsys):
        from gitlytics import cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["stars", "not-a-repo"])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "owner/repo" in captured.err
        assert captured.out == ""

    def test_the_ascii_table_stays_on_stdout(self, capsys):
        """The ASCII table is the payload — it must stay on stdout."""
        from gitlytics.core import print_repo_table

        frame = pd.DataFrame([
            {"repository": "owner/repo", "views": 10, "unique_visitors": 5,
             "clones": 2, "unique_cloners": 1, "stars": 3, "forks": 0,
             "top_referrer": "google.com"},
        ])
        print_repo_table(frame)
        captured = capsys.readouterr()
        assert "REPOSITORY" in captured.out
        assert captured.err == ""

    def test_the_success_summary_stays_on_stdout(self, capsys):
        from gitlytics import cli

        frame = pd.DataFrame([
            {"date": "2026-01-01", "repository": "owner/repo", "views": 10},
        ])
        with patch("gitlytics.cli.fetch_traffic", return_value=frame):
            cli.main(["fetch", "--token", "t"])
        captured = capsys.readouterr()
        assert "Fetch successful." in captured.out
        assert captured.err == ""


# ── AF2-LIB-10: `python -m gitlytics --help` must print "gitlytics" ───────

class TestLib10ProgramName:
    def test_parser_uses_the_gitlytics_prog_name(self):
        from gitlytics.cli import build_parser

        assert build_parser().prog == "gitlytics"

    def test_module_help_does_not_mention_main_py(self):
        result = subprocess.run(
            [sys.executable, "-m", "gitlytics", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env={**os.environ, "PYTHONPATH": str(SRC)},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("usage: gitlytics")
        assert "__main__.py" not in result.stdout


# ── AF2-LIB-11: the PEP 639 licence declaration ────────────────────────────

class TestLib11Pep639License:
    def test_license_is_a_machine_readable_expression(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["license"] == "LicenseRef-Apache-2.0-with-Commons-Clause"
        assert data["project"]["license-files"] == ["LICENSE"]

    def test_the_deprecated_table_form_is_gone(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert not isinstance(data["project"]["license"], dict)

    def test_the_license_classifier_was_removed(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        classifiers = data["project"]["classifiers"]
        assert not any(c.startswith("License ::") for c in classifiers)

    def test_the_build_backend_supports_pep639(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        requires = " ".join(data["build-system"]["requires"])
        assert "setuptools>=77" in requires

    def test_the_license_appendix_names_the_actual_terms(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "License: Apache 2.0 with Commons Clause" in license_text
        assert "License: Apache 2.0\n" not in license_text

    def test_the_source_headers_agree_with_the_declared_spdx_id(self):
        """The SPDX identifier in the source matches the declared licence."""
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]["license"]
        headers = [
            path
            for path in (SRC / "gitlytics").glob("*.py")
            if "SPDX-License-Identifier:" in path.read_text(encoding="utf-8")
        ]
        assert headers, "no source file declares an SPDX identifier"
        for path in headers:
            match = re.search(
                r"SPDX-License-Identifier:\s*(\S+)",
                path.read_text(encoding="utf-8"),
            )
            assert match
            assert match.group(1) == declared, (
                f"{path.name} declares {match.group(1)} but pyproject declares {declared}"
            )


# ── AF2-LIB-12: the CI regression test must not hardcode the version ───────

class TestLib12DynamicVersion:
    def test_the_ci_test_no_longer_hardcodes_a_version(self):
        text = (ROOT / "tests" / "test_audit_4_ci_regressions.py").read_text(
            encoding="utf-8"
        )
        assert '"0.7.2"' not in text

    def test_the_declared_version_matches_the_package(self):
        import gitlytics

        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert data["project"]["version"] == gitlytics.__version__

    def test_the_declared_version_matches_the_shipped_dashboard(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        index_html = (ROOT / "src" / "gitlytics" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        match = re.search(r'"softwareVersion":\s*"([^"]+)"', index_html)
        assert match
        assert match.group(1) == data["project"]["version"]


# ── AF2-LIB-13: the cross-repo contract must not be silently skipped ───────

class TestLib13CrossRepoContractIsLoud:
    def test_metadata_contract_emits_a_warning_when_it_skips(self):
        """A missing sibling repo must be announced, not silently skipped."""
        source = (ROOT / "tests" / "test_metadata_contract.py").read_text(encoding="utf-8")
        assert "warnings.warn" in source
        assert "NOT being enforced" in source

    def test_packaging_emits_a_warning_when_it_skips(self):
        source = (ROOT / "tests" / "test_packaging.py").read_text(encoding="utf-8")
        assert "warnings.warn" in source
        assert "NOT" in source

    def test_the_silent_return_is_gone(self):
        source = (ROOT / "tests" / "test_packaging.py").read_text(encoding="utf-8")
        assert "if not homepage.exists():\n        return" not in source

    def test_the_ci_workflow_checks_out_the_sibling_repo(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        assert "gitlytics-metadata-pages" in workflow
        assert "actions/checkout@" in workflow


# ── AF2-LIB-14: rate-limit backoff must be bounded and observable ─────────

class TestLib14BoundedBackoff:
    def test_the_retry_budget_is_capped(self):
        assert 0 < core._RETRY_BUDGET_SECONDS <= 60
        assert 0 < core._REQUEST_DEADLINE_SECONDS <= 300

    def test_backoff_is_clamped_to_the_remaining_budget(self):
        deadline = core.time.monotonic() + 1000
        delay, exhausted = core._cap_backoff(60.0, 0.0, deadline)
        assert exhausted is False
        assert delay <= core._RETRY_BUDGET_SECONDS

    def test_backoff_is_exhausted_when_the_budget_is_spent(self):
        deadline = core.time.monotonic() + 1000
        delay, exhausted = core._cap_backoff(60.0, core._RETRY_BUDGET_SECONDS, deadline)
        assert exhausted is True
        assert delay == 0.0

    def test_backoff_is_exhausted_when_the_deadline_passed(self):
        delay, exhausted = core._cap_backoff(5.0, 0.0, core.time.monotonic() - 1)
        assert exhausted is True
        assert delay == 0.0

    def test_a_rate_limited_request_announces_the_wait_on_stderr(self, capsys):
        """A user must never stare at a silent multi-second stall."""
        limited = _resp(403, headers={"Retry-After": "5"}, text="rate limit exceeded")
        ok = _resp(200, body={"ok": True})
        with patch("gitlytics.core._http_get", side_effect=[limited, ok]), \
             patch("gitlytics.core.time.sleep"):
            data, status = core._safe_get("https://api.github.com/x", {})
        assert status == 200 and data == {"ok": True}
        captured = capsys.readouterr()
        assert "rate limited" in captured.err
        assert "waiting 5s" in captured.err

    def test_the_total_sleep_is_bounded_by_the_retry_budget(self):
        """The whole backoff sequence must fit in the retry budget."""
        always_limited = _resp(429, headers={"Retry-After": "60"})
        slept = []
        with patch("gitlytics.core._http_get", return_value=always_limited), \
             patch("gitlytics.core.time.sleep", side_effect=lambda d: slept.append(d)):
            data, status = core._safe_get("https://api.github.com/x", {})
        assert status == 429
        assert sum(slept) <= core._RETRY_BUDGET_SECONDS

    def test_an_overall_deadline_is_published_for_the_operation(self):
        with core._operation_deadline(30.0):
            deadline = core._active_deadline()
            assert deadline > core.time.monotonic()
            assert deadline <= core.time.monotonic() + 30.0


# ── AF2-LIB-16: the README must not promise what the wheel lacks ──────────

class TestLib16ReadmeScope:
    @pytest.fixture(autouse=True)
    def _readme(self):
        self.text = (ROOT / "README.md").read_text(encoding="utf-8")
        return self

    def test_the_card_section_is_scoped_to_the_hosted_service(self):
        assert "api.gitlytics.dev" in self.text
        assert "not the pip package" in self.text.lower() or \
            "not part of the pip package" in self.text.lower()

    def test_the_wheel_contents_are_stated_explicitly(self):
        assert "pip install gitlytics" in self.text
        assert "wheel" in self.text.lower()

    def test_the_badge_count_is_the_real_one(self):
        """The hosted service ships 181 tech logos, not "200+"."""
        assert "200+ Tech Badges" not in self.text
        assert "181" in self.text

    def test_the_package_really_has_no_card_generator(self):
        """The README must not describe a module that does not exist."""
        package = SRC / "gitlytics"
        modules = {p.stem for p in package.glob("*.py")}
        assert not any(
            name in modules
            for name in ("cards", "card", "svg", "badges", "logos", "renderer")
        )

    def test_the_realistic_card_slug_count_is_stated(self):
        assert "31" in self.text


# ── AF2-LIB-17: the packaged CSP must be applied by the local server ──────

class TestLib17LocalCsp:
    def test_the_headers_file_is_parsed(self, tmp_path):
        from gitlytics.api import _parse_static_security_headers

        headers = tmp_path / "_headers"
        headers.write_text(
            "/*\n"
            "  X-Frame-Options: DENY\n"
            "  Content-Security-Policy: default-src 'self'\n"
            "  Cache-Control: no-cache\n"
            "\n"
            "/assets/*\n"
            "  Cache-Control: public, max-age=31536000, immutable\n",
            encoding="utf-8",
        )
        parsed = _parse_static_security_headers(headers)
        assert parsed["X-Frame-Options"] == "DENY"
        assert parsed["Content-Security-Policy"] == "default-src 'self'"
        # Cache rules are CDN-specific and must not leak into the local server.
        assert "Cache-Control" not in parsed

    def test_the_local_server_emits_the_content_security_policy(self):
        from fastapi.testclient import TestClient
        from gitlytics import api

        response = TestClient(api.app).get("/")
        csp = response.headers.get("content-security-policy")
        assert csp, "the local dashboard must emit a Content-Security-Policy"
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_the_local_server_still_emits_the_basic_headers(self):
        from fastapi.testclient import TestClient
        from gitlytics import api

        response = TestClient(api.app).get("/")
        assert response.headers.get("x-content-type-options") == "nosniff"
        assert response.headers.get("x-frame-options") == "DENY"
        assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    def test_the_csp_matches_the_packaged_headers_file(self):
        from fastapi.testclient import TestClient
        from gitlytics import api

        packaged = (SRC / "gitlytics" / "static" / "_headers").read_text(
            encoding="utf-8"
        )
        expected = next(
            line for line in packaged.splitlines()
            if "Content-Security-Policy:" in line
        ).split("Content-Security-Policy:", 1)[1].strip()

        response = TestClient(api.app).get("/")
        assert response.headers["content-security-policy"] == expected


# ---------------------------------------------------------------------------
# AF2-ACT-08 - the sync path can exclude private repositories from the vault
# ---------------------------------------------------------------------------

def test_af2_act_08_public_only_filters_private_repos_in_all_mode(monkeypatch):
    repos = [
        {"full_name": "me/public", "private": False},
        {"full_name": "me/private", "private": True},
    ]
    monkeypatch.setattr(core, "get_all_repos", lambda token: list(repos))
    monkeypatch.setattr(core, "get_repo_traffic", lambda *a, **k: {})
    monkeypatch.setattr(
        core,
        "build_tidy_rows",
        lambda repo, traffic, metrics: [{"repository": repo["full_name"]}],
    )

    df = core.fetch_traffic_data("tok", None, public_only=True)
    assert list(df["repository"]) == ["me/public"]


def test_af2_act_08_public_only_defaults_off_for_library_callers(monkeypatch):
    repos = [
        {"full_name": "me/public", "private": False},
        {"full_name": "me/private", "private": True},
    ]
    monkeypatch.setattr(core, "get_all_repos", lambda token: list(repos))
    monkeypatch.setattr(core, "get_repo_traffic", lambda *a, **k: {})
    monkeypatch.setattr(
        core,
        "build_tidy_rows",
        lambda repo, traffic, metrics: [{"repository": repo["full_name"]}],
    )

    df = core.fetch_traffic_data("tok", None)
    assert sorted(df["repository"]) == ["me/private", "me/public"]


def test_af2_act_08_explicit_repo_names_are_never_filtered(monkeypatch):
    monkeypatch.setattr(
        core,
        "get_single_repo",
        lambda token, name: {"full_name": name, "private": True},
    )
    monkeypatch.setattr(core, "get_repo_traffic", lambda *a, **k: {})
    monkeypatch.setattr(
        core,
        "build_tidy_rows",
        lambda repo, traffic, metrics: [{"repository": repo["full_name"]}],
    )

    df = core.fetch_traffic_data("tok", ["me/explicit"], public_only=True)
    assert list(df["repository"]) == ["me/explicit"]


def test_af2_act_08_sync_plumbs_public_only_to_the_fetcher(monkeypatch):
    seen = {}

    def fake_fetch(token, repo_names, metrics, public_only=False):
        seen.update(repo_names=repo_names, public_only=public_only)
        return pd.DataFrame()

    monkeypatch.setattr(core, "fetch_traffic_data", fake_fetch)
    from gitlytics import automation

    monkeypatch.setattr(automation, "fetch_traffic_data", fake_fetch)
    automation.run_sync_cycle("tok", None, data_dir="./data", public_only=True)
    assert seen == {"repo_names": None, "public_only": True}


def test_af2_xc_04_upload_cap_matches_the_cloud_backend_and_docs():
    """The library must enforce the same 5 MB ceiling as the cloud backend.

    A 25 MB local cap let a backup that worked in `gitlytics dashboard` fail
    with HTTP 413 against the cloud API (Audit Final 2, AF2-XC-04).
    """
    from gitlytics import api

    assert api._MAX_UPLOAD_BYTES == 5 * 1024 * 1024
