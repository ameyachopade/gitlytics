"""
tests/test_api.py
Unit tests for gitlytics/api.py — FastAPI endpoints.
Uses FastAPI's TestClient so no running server or GitHub token is needed.
All GitHub API calls are mocked.

Run with:
    python -m pytest tests/test_api.py -v

Note: requires httpx  →  pip install httpx
"""
import io
import json
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd

# TestClient requires httpx — skip the whole module gracefully if not installed
try:
    from fastapi.testclient import TestClient
    from gitlytics.api import app
    HAS_TESTCLIENT = True
except Exception:
    HAS_TESTCLIENT = False

pytestmark = pytest.mark.skipif(
    not HAS_TESTCLIENT,
    reason="httpx not installed — run: pip install httpx"
)

# One shared client for all tests — no server startup needed
client = TestClient(app, raise_server_exceptions=True)


# ── Helper factories ───────────────────────────────────────────────────────────

def _make_tidy_df(repo="user/repo", views=10, clones=5, is_private=False):
    """Builds a minimal one-row tidy DataFrame as returned by fetch_traffic_data."""
    return pd.DataFrame([{
        "date": "2025-06-14",
        "repository": repo,
        "is_private": is_private,
        "views": views,
        "unique_visitors": 4,
        "clones": clones,
        "unique_cloners": 2,
        "stars": 20,
        "forks": 3,
        "top_referrer": "github.com",
        "top_referrer_views": 5,
        "top_referrer_uniques": 2,
        "top_path": "/README.md",
        "top_path_views": 4,
        "top_path_uniques": 1,
        "_raw_referrers": json.dumps([{"referrer": "github.com", "count": 5, "uniques": 2}]),
        "_raw_paths":     json.dumps([{"path": "/README.md", "count": 4, "uniques": 1}]),
    }])


def _make_csv_body(headers, row):
    """Creates a CSV BytesIO for upload tests."""
    content = ",".join(headers) + "\n" + ",".join(str(v) for v in row) + "\n"
    return io.BytesIO(content.encode("utf-8"))


# ── /api/config ────────────────────────────────────────────────────────────────

class TestConfigEndpoint:
    def test_returns_200(self):
        # The config endpoint must always respond with 200
        response = client.get("/api/config")
        assert response.status_code == 200

    def test_returns_has_token_field(self):
        # has_token tells the dashboard whether the server was started with a pre-set token
        data = client.get("/api/config").json()
        assert "has_token" in data
        assert isinstance(data["has_token"], bool)

    def test_has_token_false_without_env_token(self):
        # With no GITLYTICS_TOKEN in env, has_token should be False
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITLYTICS_TOKEN", None)
            data = client.get("/api/config").json()
        assert data["has_token"] is False

    def test_returns_has_data_dir_field(self):
        # has_data_dir tells the dashboard if a historical CSV directory is configured
        data = client.get("/api/config").json()
        assert "has_data_dir" in data


# ── /api/auth ──────────────────────────────────────────────────────────────────

class TestAuthEndpoint:
    @patch("gitlytics.api.validate_token", return_value=(True, "ameyachopade"))
    @patch("gitlytics.api.get_user_profile", return_value={
        "login": "ameyachopade", "name": "Ameya Chopade", "avatar_url": "https://avatars.github.com/u/1"
    })
    def test_valid_token_returns_authenticated(self, mock_profile, mock_validate):
        # A valid token should give back authenticated=True with the user's profile
        response = client.post("/api/auth", json={"token": "valid_token"})
        assert response.status_code == 200
        data = response.json()
        assert data["authenticated"] is True
        assert data["username"] == "ameyachopade"
        assert data["name"] == "Ameya Chopade"
        assert "avatar_url" in data

    @patch("gitlytics.api.validate_token", return_value=(False, "Invalid token — 401 Unauthorized."))
    def test_invalid_token_returns_401(self, mock_validate):
        # A bad or expired token must return HTTP 401
        response = client.post("/api/auth", json={"token": "bad_token"})
        assert response.status_code == 401

    def test_no_token_returns_401(self):
        # Sending an empty body with no token and no env token must return 401
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITLYTICS_TOKEN", None)
            response = client.post("/api/auth", json={"token": ""})
        assert response.status_code == 401

    @patch("gitlytics.api.validate_token", return_value=(True, "ameyachopade"))
    @patch("gitlytics.api.get_user_profile", return_value={
        "login": "ameyachopade", "name": "Ameya Chopade", "avatar_url": "https://avatars.github.com/u/1"
    })
    def test_response_has_all_profile_fields(self, mock_profile, mock_validate):
        # The dashboard needs login, name, and avatar_url — all three must be present
        data = client.post("/api/auth", json={"token": "valid_token"}).json()
        assert "username" in data
        assert "name" in data
        assert "avatar_url" in data

    @patch("gitlytics.api.validate_token", return_value=(True, "ameyachopade"))
    @patch("gitlytics.api.get_user_profile", return_value={
        "login": "ameyachopade", "name": "", "avatar_url": ""
    })
    def test_null_display_name_falls_back_to_login(self, mock_profile, mock_validate):
        # If the user has no display name set on GitHub, fall back to their username
        data = client.post("/api/auth", json={"token": "valid_token"}).json()
        # name should never be blank — it should fall back to username
        assert data["name"] or data["username"]  # at least one must be non-empty


# ── /api/traffic ───────────────────────────────────────────────────────────────

class TestTrafficEndpoint:
    @patch("gitlytics.api.validate_token", return_value=(True, "user"))
    @patch("gitlytics.api._validate_token_cached", return_value=(True, "user"))
    @patch("gitlytics.api.fetch_traffic_data", return_value=_make_tidy_df())
    def test_valid_token_returns_list(self, mock_fetch, mock_cached, mock_validate):
        # The traffic endpoint must return a list of repo objects
        response = client.post("/api/traffic", json={"token": "valid_token"})
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    @patch("gitlytics.api.validate_token", return_value=(True, "user"))
    @patch("gitlytics.api._validate_token_cached", return_value=(True, "user"))
    @patch("gitlytics.api.fetch_traffic_data", return_value=_make_tidy_df())
    def test_repo_objects_have_required_fields(self, mock_fetch, mock_cached, mock_validate):
        # Each repo object must include the fields the React dashboard depends on
        response = client.post("/api/traffic", json={"token": "valid_token"})
        repos = response.json()
        assert len(repos) > 0
        repo = repos[0]
        for field in ["repository", "views", "clones", "stars", "forks"]:
            assert field in repo, f"Missing field: {field}"

    @patch("gitlytics.api.validate_token", return_value=(False, "Bad token"))
    def test_invalid_token_returns_401(self, mock_validate):
        # A bad token must return 401, not crash or return empty data
        response = client.post("/api/traffic", json={"token": "bad_token"})
        assert response.status_code == 401

    def test_no_token_returns_401(self):
        # Missing token must return 401
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITLYTICS_TOKEN", None)
            response = client.post("/api/traffic", json={"token": ""})
        assert response.status_code == 401

    @patch("gitlytics.api.validate_token", return_value=(True, "user"))
    @patch("gitlytics.api._validate_token_cached", return_value=(True, "user"))
    @patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame())
    def test_empty_data_returns_empty_list(self, mock_fetch, mock_cached, mock_validate):
        # If the user has no repos, the endpoint must return [] not crash
        response = client.post("/api/traffic", json={"token": "valid_token"})
        assert response.status_code == 200
        assert response.json() == []

    @patch("gitlytics.api.validate_token", return_value=(True, "user"))
    @patch("gitlytics.api._validate_token_cached", return_value=(True, "user"))
    @patch("gitlytics.api.fetch_traffic_data")
    @patch("gitlytics.api.pd.read_csv")
    @patch("gitlytics.api.Path.glob")
    @patch("gitlytics.api.Path.exists", return_value=True)
    def test_merges_csv_and_live_traffic(
        self, mock_exists, mock_glob, mock_read_csv, mock_fetch, mock_cached, mock_validate
    ):
        from pathlib import Path
        csv_df = pd.DataFrame([{
            "date": "2026-06-01",
            "repository": "user/repo",
            "is_private": False,
            "views": 10,
            "unique_visitors": 4,
            "clones": 5,
            "unique_cloners": 2,
            "stars": 20,
            "forks": 3,
            "top_referrer": "github.com",
            "top_referrer_views": 5,
            "top_referrer_uniques": 2,
            "top_path": "/README.md",
            "top_path_views": 4,
            "top_path_uniques": 1,
            "_raw_referrers": "[]",
            "_raw_paths": "[]",
        }])
        live_df = pd.DataFrame([{
            "date": "2026-06-14",
            "repository": "user/repo",
            "is_private": False,
            "views": 15,
            "unique_visitors": 6,
            "clones": 8,
            "unique_cloners": 4,
            "stars": 20,
            "forks": 3,
            "top_referrer": "github.com",
            "top_referrer_views": 6,
            "top_referrer_uniques": 3,
            "top_path": "/README.md",
            "top_path_views": 5,
            "top_path_uniques": 2,
            "_raw_referrers": "[]",
            "_raw_paths": "[]",
        }])

        mock_glob.return_value = [Path("traffic_2026-06.csv")]
        mock_read_csv.return_value = csv_df
        mock_fetch.return_value = live_df

        with patch.dict("os.environ", {"GITLYTICS_DATA_DIR": "fake_data_dir"}, clear=False):
            response = client.post("/api/traffic", json={"token": "valid_token"})

        assert response.status_code == 200
        repos = response.json()
        assert len(repos) > 0
        repo_payload = repos[0]
        daily_views = repo_payload.get("_daily_views", [])
        timestamps = [item["timestamp"][:10] for item in daily_views]
        assert "2026-06-01" in timestamps
        assert "2026-06-14" in timestamps


# ── /api/upload-csv ────────────────────────────────────────────────────────────

class TestUploadCsvEndpoint:
    def _upload(self, headers, row):
        """Helper: uploads a CSV and returns the Response."""
        content = ",".join(headers) + "\n" + ",".join(str(v) for v in row) + "\n"
        return client.post(
            "/api/upload-csv",
            files={"file": ("test.csv", content.encode("utf-8"), "text/csv")},
            headers={"origin": "http://testserver"},
        )

    def test_valid_native_csv_returns_200(self):
        # Our own CSV format should be accepted and return a list of repo objects
        response = self._upload(
            ["date", "repository", "is_private", "views", "unique_visitors",
             "clones", "unique_cloners", "stars", "forks",
             "top_referrer", "top_referrer_views", "top_referrer_uniques",
             "top_path", "top_path_views", "top_path_uniques",
             "_raw_referrers", "_raw_paths"],
            ["2025-06-14", "user/repo", "False", 10, 4, 5, 2, 20, 3,
             "github.com", 5, 2, "/README.md", 4, 1, "[]", "[]"]
        )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_minimal_repository_column_accepted(self):
        # A minimal CSV with repository, date, and views is the smallest valid upload
        response = self._upload(
            ["repository", "date", "views"],
            ["user/repo", "2025-06-14", 42]
        )
        assert response.status_code == 200

    def test_invalid_csv_missing_repository_returns_400(self):
        # A CSV with no recognisable repository column must return 400 with a clear error
        response = self._upload(
            ["some_random_column", "data"],
            ["value1", "value2"]
        )
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_repo_name_column_renamed_automatically(self):
        # Old 'repo_name' column from legacy exports should be accepted after renaming
        response = self._upload(
            ["repo_name", "date", "views"],
            ["user/legacy_repo", "2025-06-14", 10]
        )
        assert response.status_code == 200

    def test_upload_returns_list_of_repo_objects(self):
        # The endpoint must always return a list (may be empty but not null)
        response = self._upload(["repository", "date", "views"], ["user/repo", "2025-06-14", 10])
        assert isinstance(response.json(), list)

    def test_upload_is_preview_only_and_never_persists(self, tmp_path):
        # Uploads are previews. Persisting them under traffic_*.csv made every
        # upload a permanent part of every future /api/traffic response.
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        with patch.dict("os.environ", {"GITLYTICS_DATA_DIR": str(data_dir)}, clear=False):
            response = self._upload(
                ["repository", "date", "views"],
                ["user/repo", "2025-06-14", 42]
            )
        assert response.status_code == 200
        assert list(data_dir.iterdir()) == []


# ── Root and SPA fallback ──────────────────────────────────────────────────────

class TestRootEndpoint:
    def test_root_returns_200_or_503(self):
        # Root can return 200 (dashboard built) or 503 (dashboard not built yet)
        response = client.get("/")
        assert response.status_code in (200, 503)

    def test_unknown_path_returns_200_or_503(self):
        # Any unknown path goes to the SPA fallback — should not return 404
        response = client.get("/some/deep/react/route")
        assert response.status_code in (200, 503)

    def test_api_routes_not_caught_by_spa(self):
        # API routes must NOT be swallowed by the SPA catch-all
        response = client.get("/api/config")
        assert response.status_code == 200  # always returns JSON, not HTML

    def test_unknown_api_path_returns_404_not_html(self):
        # Bug fix: a /api/* path that doesn't match any route must return 404, not the SPA index
        response = client.get("/api/this-endpoint-does-not-exist")
        assert response.status_code == 404
        # Make sure we got JSON, not the HTML SPA shell
        assert "text/html" not in response.headers.get("content-type", "")

    def test_path_traversal_is_blocked(self):
        # Bug fix: a request like `/..%2F..%2Fapp.py` previously let
        # `asset_file = frontend_dir / full_path` resolve OUTSIDE the
        # static directory and serve any file the worker could read.
        # The SPA fallback must now resolve + confine the path so the
        # worker never streams source files back to the browser.
        for evil_path in ("/../api.py", "/..%2F..%2Fapi.py", "/../../etc/passwd"):
            response = client.get(evil_path)
            # Two acceptable outcomes:
            #   200 + HTML — the SPA fallback kicked in (no asset file
            #     resolved *inside* frontend_dir, so we served index.html).
            #   404 / 503 — no index.html available.
            # What is NEVER acceptable: serving a non-HTML response body
            # whose contents are a source file (api.py, /etc/passwd).
            assert response.status_code in (200, 404, 503), (
                f"Unexpected status for {evil_path!r}: {response.status_code}"
            )
            # If we served HTML (SPA fallback), the body is the index — not
            # a leaked source file. If we served JSON 404/503, body must
            # not contain the literal path we tried to read.
            body = response.text
            assert "api.py" not in body or "<!doctype html>" in body.lower(), (
                f"Source-file content leaked for {evil_path!r}: {body[:200]!r}"
            )


# ── CORS allowlist (v0.2.1 fix) ──────────────────────────────────────────────

class TestCorsHeaders:
    def test_authorization_header_is_allowed(self):
        # Bug fix: 'Authorization' must be in CORS allow_headers for the dashboard
        # to send the token via header in cross-origin deployments.
        from gitlytics.api import app as api_app
        from fastapi.middleware.cors import CORSMiddleware
        cors_mw = next((m for m in api_app.user_middleware if m.cls is CORSMiddleware), None)
        assert cors_mw is not None, "CORS middleware is not registered"
        kwargs = cors_mw.kwargs
        assert "Authorization" in kwargs.get("allow_headers", []), (
            "CORS allow_headers must include 'Authorization'"
        )
        # Vite dev port (5173) is intentionally NOT in the allowlist (v0.2.1 fix).
        assert "http://localhost:5173" not in kwargs.get("allow_origins", [])


# ── /api/upload-csv oversize guard (v0.2.1 fix) ──────────────────────────────

class TestUploadSizeLimit:
    def test_oversize_upload_returns_413(self, tmp_path):
        # Bug fix: an upload larger than 5 MB must return 413, not consume memory
        from gitlytics.api import _MAX_UPLOAD_BYTES
        # Build a body that's just over the limit (1 byte extra)
        body = b"x" * (_MAX_UPLOAD_BYTES + 1)
        # Set GITLYTICS_DATA_DIR so the streaming path (with size check) runs
        with patch.dict(
            "os.environ", {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False
        ):
            response = client.post(
                "/api/upload-csv",
                files={"file": ("huge.csv", body, "text/csv")},
                headers={"origin": "http://testserver"},
            )
        assert response.status_code == 413

    def test_oversize_upload_returns_413_without_data_dir(self, tmp_path):
        # Bug fix: the 5 MB cap must be enforced even when GITLYTICS_DATA_DIR is
        # not set (the default/common configuration). The original fix only enforced
        # the limit inside the `if data_dir:` branch.
        from gitlytics.api import _MAX_UPLOAD_BYTES
        body = b"x" * (_MAX_UPLOAD_BYTES + 1)
        # Ensure no data dir is set so the unbounded read path is exercised
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("GITLYTICS_DATA_DIR", None)
            os.environ.pop("GITLYTICS_TOKEN", None)
            response = client.post(
                "/api/upload-csv",
                files={"file": ("huge.csv", body, "text/csv")},
                headers={"origin": "http://testserver"},
            )
        assert response.status_code == 413

    def test_under_limit_upload_proceeds(self):
        # A small upload must NOT trigger 413
        body = b"date,repository,views\n2025-06-14,u/r,10\n"
        response = client.post(
            "/api/upload-csv",
            files={"file": ("ok.csv", body, "text/csv")},
            headers={"origin": "http://testserver"},
        )
        assert response.status_code in (200, 400)  # 400 only if column recognition fails


# -- LIB-03/LIB-04 regression: token delegation and cross-origin uploads ------

class TestLibraryApiHardening:
    def test_env_token_fallback_is_disabled_by_default(self):
        import os
        from gitlytics.api import _get_token
        with patch.dict(os.environ, {"GITLYTICS_TOKEN": "env_tok"}, clear=False):
            os.environ.pop("GITLYTICS_ALLOW_ENV_TOKEN", None)
            assert _get_token("") is None

    def test_env_token_fallback_requires_opt_in(self):
        import os
        from gitlytics.api import _get_token
        with patch.dict(
            os.environ,
            {"GITLYTICS_TOKEN": "env_tok", "GITLYTICS_ALLOW_ENV_TOKEN": "1"},
            clear=False,
        ):
            assert _get_token("") == "env_tok"

    def test_explicit_request_token_always_wins(self):
        from gitlytics.api import _get_token
        assert _get_token(" request_tok ") == "request_tok"

    def test_star_history_requires_bearer_token(self):
        response = client.get("/api/star-history?owner=u&repo=r")
        assert response.status_code == 401

    def test_malformed_star_history_bearer_is_rejected(self):
        response = client.get(
            "/api/star-history?owner=u&repo=r",
            headers={"authorization": "Token abc"},
        )
        assert response.status_code == 401

    def test_cross_origin_upload_is_rejected(self):
        body = b"date,repository,views\n2025-06-14,u/r,10\n"
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLYTICS_UPLOAD_KEY", None)
            response = client.post(
                "/api/upload-csv",
                files={"file": ("test.csv", body, "text/csv")},
                headers={"origin": "https://evil.example"},
            )
        assert response.status_code == 403

    def test_upload_key_allows_non_browser_client(self):
        body = b"date,repository,views\n2025-06-14,u/r,10\n"
        import os
        with patch.dict(os.environ, {"GITLYTICS_UPLOAD_KEY": "secret-key"}, clear=False):
            response = client.post(
                "/api/upload-csv",
                files={"file": ("test.csv", body, "text/csv")},
                headers={"x-upload-key": "secret-key"},
            )
        assert response.status_code == 200
