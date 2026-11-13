"""
tests/test_audit_4_lib_regressions.py
Regression tests for every A4-LIB-* finding in reports/AUDIT_4.md.

One test (or test class) per finding. All tests are offline.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from gitlytics import core
from gitlytics import process
from gitlytics.core import (
    GitHubRateLimitError,
    TokenExpiredError,
    _safe_get,
    fetch_star_history,
    fetch_traffic_data,
)
from gitlytics.process import build_json_payload

ROOT = Path(__file__).resolve().parent.parent


def _resp(status: int, *, body=None, headers=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = text
    return r


# ── A4-LIB-01 ─────────────────────────────────────────────────────────────────

class TestLib01SingleTokenExpiredError:
    def test_reexport_is_the_core_class(self):
        from gitlytics import TokenExpiredError as PublicTokenExpiredError
        from gitlytics import automation

        assert PublicTokenExpiredError is core.TokenExpiredError
        assert automation.TokenExpiredError is core.TokenExpiredError

    def test_non_cron_sync_propagates_core_exception(self, tmp_path):
        from gitlytics.automation import run_sync

        with patch(
            "gitlytics.automation.fetch_traffic_data",
            side_effect=core.TokenExpiredError("dead token"),
        ):
            with pytest.raises(core.TokenExpiredError):
                run_sync("token", data_dir=str(tmp_path))

    def test_cron_mode_exits_instead_of_logging_unexpected(self, tmp_path):
        from gitlytics.automation import run_sync

        with patch("gitlytics.automation.validate_token", return_value=(True, "user")), \
             patch(
                 "gitlytics.automation.run_sync_cycle",
                 side_effect=core.TokenExpiredError("dead token"),
             ), \
             patch("gitlytics.automation._interruptible_sleep"):
            with pytest.raises(core.TokenExpiredError):
                run_sync(
                    "token",
                    data_dir=str(tmp_path),
                    schedule_cron="* * * * *",
                )


# ── A4-LIB-02 ─────────────────────────────────────────────────────────────────

class TestLib02ArtifactsMatchSource:
    def test_built_artifacts_are_not_stale(self):
        dist = ROOT / "dist"
        wheels = sorted(dist.glob("*.whl")) if dist.exists() else []
        if not wheels:
            pytest.skip("dist/ not rebuilt yet (release step builds it last)")

        wheel = wheels[-1]
        assert "0.7.2" in wheel.name, f"unexpected artifact {wheel.name}"
        with zipfile.ZipFile(wheel) as zf:
            core_src = zf.read("gitlytics/core.py").decode("utf-8")
            assert "range(14, -1, -1)" in core_src, "wheel contains pre-Audit-3 core.py"
            wheel_assets = {
                n.split("gitlytics/static/", 1)[1]
                for n in zf.namelist()
                if "gitlytics/static/" in n and not n.endswith("/")
            }
        source_assets = {
            str(p.relative_to(ROOT / "src" / "gitlytics" / "static")).replace("\\", "/")
            for p in (ROOT / "src" / "gitlytics" / "static").rglob("*")
            if p.is_file()
        }
        assert wheel_assets == source_assets, (
            "wheel static assets differ from src — stale build/"
        )


# ── A4-LIB-03 ─────────────────────────────────────────────────────────────────

class TestLib03PinnedReposAuthFailure:
    def test_401_on_pinned_repo_raises_instead_of_skipping(self):
        with patch("gitlytics.core._safe_get", return_value=({}, 401)):
            with pytest.raises(TokenExpiredError):
                fetch_traffic_data("token", repo_names=["owner/repo"])

    def test_404_on_pinned_repo_still_skips(self):
        with patch("gitlytics.core._safe_get", return_value=({}, 404)):
            df = fetch_traffic_data("token", repo_names=["owner/repo"])
        assert df.empty

    def test_cli_exits_1_and_leaves_existing_file_untouched(self, tmp_path, capsys):
        from gitlytics import cli

        out = tmp_path / "traffic.csv"
        out.write_bytes(b"OLD-BYTES")
        with patch("gitlytics.cli.fetch_traffic", return_value=pd.DataFrame()):
            with pytest.raises(SystemExit) as exc:
                cli.main(
                    [
                        "fetch",
                        "--token", "t",
                        "--repo-name", "owner/repo",
                        "--save-file", str(out),
                    ]
                )
        assert exc.value.code == 1
        assert out.read_bytes() == b"OLD-BYTES"


# ── A4-LIB-04 ─────────────────────────────────────────────────────────────────

class TestLib04RateLimitClassification:
    def test_401_is_token_expired(self):
        with patch("gitlytics.core._safe_get", return_value=({}, 401)):
            with pytest.raises(TokenExpiredError):
                core.get_all_repos("token")

    def test_403_with_rate_limit_markers_is_rate_limit(self):
        with patch("gitlytics.core._safe_get", return_value=({}, 429)):
            with pytest.raises(GitHubRateLimitError):
                core.get_all_repos("token")

    def test_plain_403_is_permission_error(self):
        with patch("gitlytics.core._safe_get", return_value=({}, 403)):
            with pytest.raises(TokenExpiredError) as exc:
                core.get_all_repos("token")
        assert "403" in str(exc.value)

    def test_safe_get_normalises_secondary_limit_403_to_429(self):
        limited = _resp(
            403,
            headers={"x-ratelimit-remaining": "0"},
            text="API rate limit exceeded",
        )
        with patch("gitlytics.core._http_get", side_effect=[limited, limited, limited]), \
             patch("gitlytics.core.time.sleep"):
            data, status = _safe_get("https://api.github.com/user/repos", {})
        assert data == {}
        assert status == 429

    def test_safe_get_keeps_plain_403(self):
        denied = _resp(403, headers={}, text="Resource not accessible")
        with patch("gitlytics.core._http_get", return_value=denied), \
             patch("gitlytics.core.time.sleep"):
            data, status = _safe_get("https://api.github.com/user/repos", {})
        assert data == {}
        assert status == 403


# ── A4-LIB-05 ─────────────────────────────────────────────────────────────────

class TestLib05NaNInTimeseries:
    def _df(self):
        return pd.DataFrame(
            [
                {
                    "date": "2026-01-01",
                    "repository": "owner/repo",
                    "is_private": False,
                    "views": 10,
                    "unique_visitors": float("nan"),
                    "clones": 2,
                    "unique_cloners": 1,
                }
            ]
        )

    def test_nan_serialises_to_zero(self):
        payload = build_json_payload(self._df(), return_format="timeseries")
        point = payload["repositories"]["owner/repo"]["timeseries"][0]
        assert point["unique_visitors"] == 0
        # And the result must be JSON-serialisable (no NaN literal).
        json.dumps(payload, allow_nan=False)

    def test_summary_totals_are_ints(self):
        payload = build_json_payload(self._df(), return_format="summary")
        assert payload["repositories"]["owner/repo"]["unique_visitors"] == 0


# ── A4-LIB-06 ─────────────────────────────────────────────────────────────────

class TestLib06UploadIsolation:
    def _write(self, path: Path, repo: str):
        pd.DataFrame(
            [
                {
                    "date": "2026-01-01",
                    "repository": repo,
                    "is_private": False,
                    "views": 1,
                    "unique_visitors": 1,
                    "clones": 0,
                    "unique_cloners": 0,
                    "stars": 0,
                    "forks": 0,
                }
            ]
        ).to_csv(path, index=False)

    def test_traffic_ignores_uploaded_csv(self, tmp_path):
        from fastapi.testclient import TestClient
        from gitlytics.api import app

        self._write(tmp_path / "traffic_2026-01.csv", "owner/sync")
        self._write(tmp_path / "traffic_uploaded_deadbeef.csv", "owner/uploaded")

        with patch.dict("os.environ", {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False), \
             patch("gitlytics.api._validate_token_cached", return_value=(True, "user")), \
             patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame()):
            resp = TestClient(app).post("/api/traffic", json={"token": "t"})
        assert resp.status_code == 200
        repos = {r["repository"] for r in resp.json()}
        assert "owner/sync" in repos
        assert "owner/uploaded" not in repos

    def test_upload_leaves_no_file_behind(self, tmp_path):
        from fastapi.testclient import TestClient
        from gitlytics import api

        created: list[str] = []
        real_mkstemp = api.tempfile.mkstemp

        def tracking_mkstemp(*a, **kw):
            fd, path = real_mkstemp(*a, **kw)
            created.append(path)
            return fd, path

        body = b"repository,date,views\nowner/repo,2026-01-01,5\n"
        with patch.object(api.tempfile, "mkstemp", side_effect=tracking_mkstemp):
            resp = TestClient(api.app).post(
                "/api/upload-csv",
                files={"file": ("x.csv", body, "text/csv")},
                headers={"origin": "http://testserver"},
            )
        assert resp.status_code == 200
        assert created, "expected a temp file to be used"
        assert not any(Path(p).exists() for p in created)


# ── A4-LIB-07 ─────────────────────────────────────────────────────────────────

class TestLib07PooledSessionIsUsed:
    def test_http_get_goes_through_session(self):
        session = MagicMock()
        session.get.return_value = _resp(200, body={})
        with patch("gitlytics.core._get_session", return_value=session):
            core._http_get("https://api.github.com/user", timeout=10)
        session.get.assert_called_once()

    def test_adapter_pool_is_16(self):
        session = core._get_session()
        for prefix in ("https://", "http://"):
            adapter = session.get_adapter(prefix)
            assert adapter._pool_maxsize == 16
            assert adapter._pool_connections == 16

    def test_no_direct_requests_get_calls_remain(self):
        source = (ROOT / "src" / "gitlytics" / "core.py").read_text(encoding="utf-8")
        assert "requests.get(" not in source


# ── A4-LIB-08 ─────────────────────────────────────────────────────────────────

class TestLib08TrafficTTLCache:
    def test_two_requests_hit_github_once_then_expire(self):
        from fastapi.testclient import TestClient
        from gitlytics import api

        frame = pd.DataFrame(
            [
                {
                    "date": "2026-01-01",
                    "repository": "owner/repo",
                    "is_private": False,
                    "views": 1,
                    "unique_visitors": 1,
                    "clones": 0,
                    "unique_cloners": 0,
                    "stars": 0,
                    "forks": 0,
                }
            ]
        )
        client = TestClient(api.app)
        with patch("gitlytics.api._validate_token_cached", return_value=(True, "user")), \
             patch("gitlytics.api.fetch_traffic_data", return_value=frame) as fetch:
            client.post("/api/traffic", json={"token": "cache-test-token"})
            client.post("/api/traffic", json={"token": "cache-test-token"})
            assert fetch.call_count == 1

            # Expire the entry: the next request must fetch again.
            import hashlib

            key = hashlib.sha256(b"cache-test-token").hexdigest()
            _, cached = api._TRAFFIC_CACHE[key]
            api._TRAFFIC_CACHE[key] = (0.0, cached)
            client.post("/api/traffic", json={"token": "cache-test-token"})
            assert fetch.call_count == 2

    def test_cache_key_is_not_the_raw_token(self):
        from gitlytics import api

        with patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame()):
            api._cached_live_traffic("super-secret-token")
        assert all("super-secret-token" not in key for key in api._TRAFFIC_CACHE)


# ── A4-LIB-09 ─────────────────────────────────────────────────────────────────

class TestLib09NumericCoercion:
    def test_object_dtype_strings_sum_as_numbers(self):
        df = pd.DataFrame(
            [
                {"date": "2026-01-01", "repository": "o/r", "views": "10",
                 "unique_visitors": "1", "clones": "0", "unique_cloners": "0"},
                {"date": "2026-01-02", "repository": "o/r", "views": "5",
                 "unique_visitors": "1", "clones": "0", "unique_cloners": "0"},
            ]
        )
        payload = build_json_payload(df, return_format="summary")
        assert payload["repositories"]["o/r"]["total_views"] == 15

    def test_thousands_separator_is_handled(self):
        assert process._safe_int("1,234") == 1234

    def test_react_payload_totals_are_numeric(self):
        df = pd.DataFrame(
            [
                {"date": "2026-01-01", "repository": "o/r", "is_private": False,
                 "views": "10", "unique_visitors": "1", "clones": "0",
                 "unique_cloners": "0", "stars": "3", "forks": "1"},
                {"date": "2026-01-02", "repository": "o/r", "is_private": False,
                 "views": "5", "unique_visitors": "1", "clones": "0",
                 "unique_cloners": "0", "stars": "3", "forks": "1"},
            ]
        )
        payload = process.build_react_payload(df)
        assert payload[0]["views"] == 15

    def test_uploaded_csv_is_coerced_at_ingest(self):
        csv_text = "repository,date,views\no/r,2026-01-01,\"1,234\"\n"
        df = process.process_uploaded_csv(io.StringIO(csv_text))
        assert int(df["views"].iloc[0]) == 1234


# ── A4-LIB-10 ─────────────────────────────────────────────────────────────────

class TestLib10SmallRepoStarHistoryErrors:
    def test_small_repo_429_raises_rate_limit(self):
        meta = _resp(200, body={"stargazers_count": 5, "created_at": "2020-01-01T00:00:00Z"})
        limited = _resp(429, body=[], text="rate limit")
        with patch("gitlytics.core._http_get", side_effect=[meta, limited]), \
             patch("gitlytics.core.time.sleep"):
            with pytest.raises(GitHubRateLimitError):
                fetch_star_history("owner", "repo")

    def test_small_repo_401_raises_fetch_error(self):
        from gitlytics.core import StarHistoryFetchError

        meta = _resp(200, body={"stargazers_count": 5, "created_at": "2020-01-01T00:00:00Z"})
        unauth = _resp(401, body={}, text="Bad credentials")
        with patch("gitlytics.core._http_get", side_effect=[meta, unauth]), \
             patch("gitlytics.core.time.sleep"):
            with pytest.raises(StarHistoryFetchError):
                fetch_star_history("owner", "repo")

    def test_api_maps_rate_limit_to_429(self):
        from fastapi.testclient import TestClient
        from gitlytics.api import app

        with patch(
            "gitlytics.api.fetch_star_history",
            side_effect=GitHubRateLimitError("slow down"),
        ):
            resp = TestClient(app).get(
                "/api/star-history",
                params={"owner": "o", "repo": "r"},
                headers={"authorization": "Bearer t"},
            )
        assert resp.status_code == 429


# ── A4-LIB-11 ─────────────────────────────────────────────────────────────────

class TestLib11CliErrorMapping:
    def test_fetch_rate_limit_exits_1_without_traceback(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.fetch_traffic", side_effect=GitHubRateLimitError("429")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["fetch", "--token", "t"])
        assert exc.value.code == 1
        out = capsys.readouterr()
        assert "Traceback" not in out.err and "Traceback" not in out.out

    def test_sync_rate_limit_exits_1(self, capsys):
        from gitlytics import cli

        with patch("gitlytics.cli.sync", side_effect=GitHubRateLimitError("429")):
            with pytest.raises(SystemExit) as exc:
                cli.main(["sync", "--token", "t"])
        assert exc.value.code == 1


# ── A4-LIB-12 ─────────────────────────────────────────────────────────────────

class TestLib12AtomicCsvWrite:
    def test_failed_write_leaves_previous_bytes(self, tmp_path):
        from gitlytics import _write_csv_safely

        target = tmp_path / "traffic.csv"
        target.write_bytes(b"OLD")

        real_to_csv = pd.DataFrame.to_csv

        def boom(self, path, *a, **kw):
            # Simulate a crash mid-write: partial bytes, then raise.
            Path(path).write_bytes(b"PARTIAL")
            raise OSError("disk full")

        with patch.object(pd.DataFrame, "to_csv", boom):
            with pytest.raises(OSError):
                _write_csv_safely(str(target), pd.DataFrame({"a": [1]}))

        assert target.read_bytes() == b"OLD"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tmp_csv_")]
        assert leftovers == []
        assert real_to_csv is not None  # keep the reference for readability


# ── A4-LIB-13 ─────────────────────────────────────────────────────────────────

class TestLib13VersionFlag:
    def test_version_flag_prints_version_and_exits_0(self, capsys):
        from gitlytics import __version__
        from gitlytics.cli import main

        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert __version__ in capsys.readouterr().out


# ── A4-LIB-14 ─────────────────────────────────────────────────────────────────

class TestLib14CleanBuildStaging:
    def test_release_build_cleans_build_and_dist(self):
        workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        assert "rm -rf build dist" in workflow
        assert workflow.index("rm -rf build dist") < workflow.index("python -m build")

    def test_no_stale_build_lib_tree(self):
        stale = ROOT / "build" / "lib"
        assert not stale.exists(), "remove gitlytics/build before building"


# ── A4-LIB-15 ─────────────────────────────────────────────────────────────────

class TestLib15LiveCliNegativeTests:
    def test_negative_cli_runs_strip_tokens(self):
        source = (ROOT / "tests" / "live_cli.py").read_text(encoding="utf-8")
        assert 'env.pop("GITLYTICS_TOKEN", None)' in source
        assert 'env.pop("GITHUB_TOKEN", None)' in source
        assert "result.returncode == 0" in source
