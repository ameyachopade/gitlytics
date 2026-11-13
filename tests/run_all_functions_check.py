"""
run_all_functions_check.py
Comprehensive execution and validation harness for EVERY function in gitlytics.
Executes all 57 functions across all modules and reports PASS / FAIL.
"""
import sys
import os
import json
import io
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

# Reconfigure stdout for UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure src/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pandas as pd
import requests

# ── Offline tripwire (AF2-LIB-06) ───────────────────────────────────────────
# This harness is documented as OFFLINE. Patch the single choke point every
# `requests` call funnels through so an unmocked request fails loudly instead
# of silently hitting api.github.com.
_real_send = requests.Session.send


def _blocked_send(self, request, **kwargs):
    raise AssertionError(
        f"OFFLINE VIOLATION: an unmocked HTTP request was attempted: "
        f"{getattr(request, 'url', request)}"
    )


requests.Session.send = _blocked_send

# Modules to test
import gitlytics
import gitlytics.core
import gitlytics.process
import gitlytics.automation
import gitlytics.api
import gitlytics.cli

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

results = []

def run_test(module_name: str, func_name: str, test_fn):
    try:
        test_fn()
        results.append((module_name, func_name, True, "OK"))
        print(f"  {GREEN}[PASS]{RESET} {module_name}.{BOLD}{func_name}{RESET}")
    except Exception as exc:
        results.append((module_name, func_name, False, str(exc)))
        print(f"  {RED}[FAIL]{RESET} {module_name}.{BOLD}{func_name}{RESET} -> {exc}")


print(f"\n{BOLD}{'='*60}{RESET}")
print(f"{BOLD}  RUNNING ALL FUNCTIONS VERIFICATION SUITE (57 FUNCTIONS){RESET}")
print(f"{BOLD}{'='*60}{RESET}\n")

# ══════════════════════════════════════════════════════════════════
# 1. gitlytics (__init__.py) - 5 functions
# ══════════════════════════════════════════════════════════════════
print(f"{BOLD}[1/6] gitlytics (5 functions){RESET}")

def test_coerce_metrics():
    assert gitlytics._coerce_metrics(None) is None
    assert gitlytics._coerce_metrics(["views", "clones"]) == ["views", "clones"]
    assert gitlytics._coerce_metrics(["views", "unknown_xyz"]) == ["views"]
run_test("gitlytics", "_coerce_metrics", test_coerce_metrics)

def test_write_json_safely():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "nested" / "file.json"
        gitlytics._write_json_safely(str(p), {"hello": "world"})
        assert p.exists()
        assert json.loads(p.read_text("utf-8")) == {"hello": "world"}
run_test("gitlytics", "_write_json_safely", test_write_json_safely)

def test_fetch_traffic():
    dummy_df = pd.DataFrame([{"date": "2026-06-01", "repository": "u/r", "views": 5, "is_private": False}])
    with patch("gitlytics.fetch_traffic_data", return_value=dummy_df):
        df = gitlytics.fetch_traffic("ghp_fake", return_format="dataframe")
        assert isinstance(df, pd.DataFrame)
        ts = gitlytics.fetch_traffic("ghp_fake", return_format="timeseries")
        assert "account_totals" in ts
run_test("gitlytics", "fetch_traffic", test_fetch_traffic)

def test_sync():
    with patch("gitlytics.run_sync") as mock_rs:
        gitlytics.sync("ghp_fake", repo_name="u/r")
        mock_rs.assert_called_once()
run_test("gitlytics", "sync", test_sync)

def test_serve_dashboard():
    with patch("uvicorn.run") as mock_uv:
        gitlytics.serve_dashboard(port=8888)
        mock_uv.assert_called_once()
run_test("gitlytics", "serve_dashboard", test_serve_dashboard)


# ══════════════════════════════════════════════════════════════════
# 2. gitlytics.core - 23 functions
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[2/6] gitlytics.core (23 functions){RESET}")

def test_format_auth_header():
    assert gitlytics.core.format_auth_header(None) is None
    assert gitlytics.core.format_auth_header("   ") is None
    assert gitlytics.core.format_auth_header("tok123") == "Bearer tok123"
    assert gitlytics.core.format_auth_header("Bearer tok123") == "Bearer tok123"
run_test("gitlytics.core", "format_auth_header", test_format_auth_header)

def test_make_headers():
    h = gitlytics.core.make_headers("tok123")
    assert h["Authorization"] == "Bearer tok123"
    assert "Accept" in h
run_test("gitlytics.core", "make_headers", test_make_headers)

def test_validate_token():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = {"login": "ameyachopade"}
        ok, user = gitlytics.core.validate_token("tok123")
        assert ok is True and user == "ameyachopade"
run_test("gitlytics.core", "validate_token", test_validate_token)

def test_get_user_profile():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = {"login": "ameya", "name": "Ameya Chopade", "followers": 10}
        p = gitlytics.core.get_user_profile("tok123")
        assert p["login"] == "ameya" and p["name"] == "Ameya Chopade"
run_test("gitlytics.core", "get_user_profile", test_get_user_profile)

def test_validate_github_username():
    assert gitlytics.core.validate_github_username("torvalds") == "torvalds"
    try:
        gitlytics.core.validate_github_username("-invalid")
        assert False, "Should raise ValueError"
    except ValueError:
        pass
run_test("gitlytics.core", "validate_github_username", test_validate_github_username)

def test_get_public_user():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = {"login": "torvalds", "public_repos": 12}
        u = gitlytics.core.get_public_user("torvalds")
        assert u["login"] == "torvalds"
run_test("gitlytics.core", "get_public_user", test_get_public_user)

def test_normalise_repo():
    raw = {"name": "repo1", "full_name": "user/repo1", "stargazers_count": 5}
    norm = gitlytics.core._normalise_repo(raw)
    assert norm["name"] == "repo1" and norm["stargazers_count"] == 5
run_test("gitlytics.core", "_normalise_repo", test_normalise_repo)

def test_fetch_public_repos_by_updated():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = [{"name": "repoA", "full_name": "u/repoA"}]
        repos = gitlytics.core._fetch_public_repos_by_updated("user", 10)
        assert len(repos) == 1
run_test("gitlytics.core", "_fetch_public_repos_by_updated", test_fetch_public_repos_by_updated)

def test_fetch_public_repos_by_stars():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = {"items": [{"name": "repoB", "full_name": "u/repoB"}]}
        repos = gitlytics.core._fetch_public_repos_by_stars("user", 10)
        assert len(repos) == 1
run_test("gitlytics.core", "_fetch_public_repos_by_stars", test_fetch_public_repos_by_stars)

def test_get_public_repos():
    with patch("gitlytics.core._fetch_public_repos_by_updated", return_value=[{"full_name": "u/r1", "stargazers_count": 5}]):
        with patch("gitlytics.core._fetch_public_repos_by_stars", return_value=[{"full_name": "u/r2", "stargazers_count": 10}]):
            res = gitlytics.core.get_public_repos("user", max_repos=5)
            assert len(res) == 2
            assert res[0]["full_name"] == "u/r2"
run_test("gitlytics.core", "get_public_repos", test_get_public_repos)

def test_safe_get():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.json.return_value = {"key": "val"}
        data, status = gitlytics.core._safe_get("http://fake", {})
        assert status == 200 and data == {"key": "val"}
run_test("gitlytics.core", "_safe_get", test_safe_get)

def test_count_via_link_header():
    with patch("gitlytics.core._http_get") as mock_g:
        mock_g.return_value.status_code = 200
        mock_g.return_value.headers = {"Link": '<https://api.github.com?page=42>; rel="last"'}
        count = gitlytics.core._count_via_link_header("http://fake", {})
        assert count == 42
run_test("gitlytics.core", "_count_via_link_header", test_count_via_link_header)

def test_get_deep_repo_stats():
    with patch("gitlytics.core._safe_get", side_effect=[
        ([{"total": 100}], 200),  # commit activity
        ({"files": {"readme": {"url": "x"}}}, 200),  # community profile
    ]):
        with patch("gitlytics.core._count_via_link_header", return_value=3):
            with patch("gitlytics.core._http_get") as mock_rel:
                mock_rel.return_value.status_code = 200
                mock_rel.return_value.headers = {}
                mock_rel.return_value.json.return_value = [{"published_at": "2026-01-01"}]
                stats = gitlytics.core.get_deep_repo_stats("tok", "u/r")
                assert stats["total_commits"] == 100
                assert stats["has_readme"] is True
run_test("gitlytics.core", "get_deep_repo_stats", test_get_deep_repo_stats)

def test_fetch_deep_stats_for_top():
    with patch("gitlytics.core.get_deep_repo_stats", return_value={"total_commits": 50}):
        res = gitlytics.core.fetch_deep_stats_for_top("tok", [{"repository": "u/r", "total_views": 100}], top_n=1)
        assert "u/r" in res
run_test("gitlytics.core", "fetch_deep_stats_for_top", test_fetch_deep_stats_for_top)

def test_get_all_repos():
    with patch("gitlytics.core._safe_get", side_effect=[
        ([{"full_name": f"u/r{i}"} for i in range(100)], 200),
        ([{"full_name": "u/r101"}], 200),
    ]):
        repos = gitlytics.core.get_all_repos("tok")
        assert len(repos) == 101
run_test("gitlytics.core", "get_all_repos", test_get_all_repos)

def test_get_single_repo():
    with patch("gitlytics.core._safe_get", return_value=({"name": "r1", "full_name": "u/r1"}, 200)):
        r = gitlytics.core.get_single_repo("tok", "u/r1")
        assert r["name"] == "r1"
run_test("gitlytics.core", "get_single_repo", test_get_single_repo)

def test_star_headers():
    h = gitlytics.core._star_headers("tok")
    assert "vnd.github.star+json" in h["Accept"]
run_test("gitlytics.core", "_star_headers", test_star_headers)

def test_fetch_star_history():
    with patch("gitlytics.core._safe_get", return_value=({"stargazers_count": 0}, 200)):
        pts = gitlytics.core.fetch_star_history("owner", "repo")
        assert len(pts) == 1 and pts[0]["total"] == 0
run_test("gitlytics.core", "fetch_star_history", test_fetch_star_history)

def test_get_repo_traffic():
    # `_count_via_link_header` issues a real HTTP request — it must be mocked
    # so this "offline" harness never touches the network (AF2-LIB-06).
    with patch("gitlytics.core._safe_get", side_effect=[
        ({"views": []}, 200),
        ({"clones": []}, 200),
        ([], 200),
        ([], 200),
    ]):
        with patch("gitlytics.core._count_via_link_header", return_value=0):
            tr = gitlytics.core.get_repo_traffic("tok", "u/r")
            assert "views" in tr and "clones" in tr
run_test("gitlytics.core", "get_repo_traffic", test_get_repo_traffic)

def test_pad_traffic_data():
    padded = gitlytics.core.pad_traffic_data({"views": {"views": []}, "clones": {"clones": []}})
    assert len(padded) == 15
run_test("gitlytics.core", "pad_traffic_data", test_pad_traffic_data)

def test_build_tidy_rows():
    repo = {"full_name": "u/r", "private": False, "stargazers_count": 10, "forks_count": 2}
    traffic = {"views": {"views": []}, "clones": {"clones": []}, "referrers": [], "paths": []}
    rows = gitlytics.core.build_tidy_rows(repo, traffic)
    assert len(rows) == 15
    assert rows[0]["repository"] == "u/r"
run_test("gitlytics.core", "build_tidy_rows", test_build_tidy_rows)

def test_fetch_traffic_data():
    with patch("gitlytics.core.get_single_repo", return_value={"full_name": "u/r", "private": False}):
        with patch("gitlytics.core.get_repo_traffic", return_value={"views": {}, "clones": {}}):
            df = gitlytics.core.fetch_traffic_data("tok", repo_names=["u/r"])
            assert isinstance(df, pd.DataFrame)
            assert len(df) == 15
run_test("gitlytics.core", "fetch_traffic_data", test_fetch_traffic_data)

def test_print_repo_table():
    df = pd.DataFrame([{"repository": "u/r", "views": 10, "unique_visitors": 5, "clones": 2, "unique_cloners": 1}])
    with patch("builtins.print") as mock_p:
        gitlytics.core.print_repo_table(df)
        assert mock_p.called
run_test("gitlytics.core", "print_repo_table", test_print_repo_table)


# ══════════════════════════════════════════════════════════════════
# 3. gitlytics.process - 8 functions
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[3/6] gitlytics.process (8 functions){RESET}")

def test_is_nullish():
    assert gitlytics.process._is_nullish(None) is True
    assert gitlytics.process._is_nullish("") is True
    assert gitlytics.process._is_nullish("nan") is True
    assert gitlytics.process._is_nullish("valid") is False
run_test("gitlytics.process", "_is_nullish", test_is_nullish)

def test_to_bool():
    assert gitlytics.process._to_bool(True) is True
    assert gitlytics.process._to_bool("true") is True
    assert gitlytics.process._to_bool("1") is True
    assert gitlytics.process._to_bool("false") is False
    assert gitlytics.process._to_bool(0) is False
run_test("gitlytics.process", "_to_bool", test_to_bool)

def test_build_json_payload():
    df = pd.DataFrame([{
        "date": "2026-06-01", "repository": "u/r", "views": 10,
        "clones": 5, "unique_visitors": 4, "unique_cloners": 2,
        "stars": 20, "forks": 1, "is_private": False
    }])
    ts = gitlytics.process.build_json_payload(df, return_format="timeseries")
    assert ts["account_totals"]["total_views"] == 10
    sum_res = gitlytics.process.build_json_payload(df, return_format="summary")
    assert sum_res["repositories"]["u/r"]["total_views"] == 10
run_test("gitlytics.process", "build_json_payload", test_build_json_payload)

def test_process_uploaded_csv():
    csv_text = "date,repository,views\n2026-06-01,u/r,5\n"
    df = gitlytics.process.process_uploaded_csv(io.StringIO(csv_text))
    assert len(df) == 1
    assert "repository" in df.columns
run_test("gitlytics.process", "process_uploaded_csv", test_process_uploaded_csv)

def test_parse_raw():
    assert gitlytics.process._parse_raw('[{"a": 1}]') == [{"a": 1}]
    assert gitlytics.process._parse_raw(None) == []
    assert gitlytics.process._parse_raw("invalid json") == []
run_test("gitlytics.process", "_parse_raw", test_parse_raw)

def test_safe_int():
    assert gitlytics.process._safe_int("42") == 42
    assert gitlytics.process._safe_int(None, fallback=-1) == -1
    assert gitlytics.process._safe_int("abc", fallback=0) == 0
run_test("gitlytics.process", "_safe_int", test_safe_int)

def test_safe_str():
    assert gitlytics.process._safe_str("hello") == "hello"
    assert gitlytics.process._safe_str(None, fallback="default") == "default"
run_test("gitlytics.process", "_safe_str", test_safe_str)

def test_build_react_payload():
    df = pd.DataFrame([{
        "date": "2026-06-01", "repository": "u/r", "views": 10,
        "clones": 5, "unique_visitors": 4, "unique_cloners": 2,
        "stars": 20, "forks": 1, "is_private": False
    }])
    payload = gitlytics.process.build_react_payload(df)
    assert len(payload) == 1
    assert payload[0]["repository"] == "u/r"
    assert payload[0]["views"] == 10
run_test("gitlytics.process", "build_react_payload", test_build_react_payload)


# ══════════════════════════════════════════════════════════════════
# 4. gitlytics.automation - 8 functions
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[4/6] gitlytics.automation (8 functions){RESET}")

def test_get_csv_path():
    with tempfile.TemporaryDirectory() as td:
        p_m = gitlytics.automation.get_csv_path(td, "monthly", datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert "traffic_2026-06.csv" in p_m
        p_y = gitlytics.automation.get_csv_path(td, "yearly", datetime(2026, 6, 1, tzinfo=timezone.utc))
        assert "traffic_2026.csv" in p_y
run_test("gitlytics.automation", "get_csv_path", test_get_csv_path)

def test_export_json_database():
    with tempfile.TemporaryDirectory() as td:
        csv_file = Path(td) / "traffic_2026-06.csv"
        csv_file.write_text("date,repository,is_private,views\n2026-06-01,u/r,False,10\n", encoding="utf-8")
        out_json = Path(td) / "export.json"
        gitlytics.automation.export_json_database(td, str(out_json))
        assert out_json.exists()
run_test("gitlytics.automation", "export_json_database", test_export_json_database)

def test_normalize_field():
    assert gitlytics.automation._normalize_field(" Date ") == "date"
    assert gitlytics.automation._normalize_field(123) == ""
run_test("gitlytics.automation", "_normalize_field", test_normalize_field)

def test_merge_schema():
    merged = gitlytics.automation._merge_schema(["date", "repo"], ["date", "repo", "views"])
    assert merged == ["date", "repo", "views"]
run_test("gitlytics.automation", "_merge_schema", test_merge_schema)

def test_write_df_to_csv():
    with tempfile.TemporaryDirectory() as td:
        csv_path = str(Path(td) / "traffic.csv")
        df = pd.DataFrame([{"date": "2026-06-01", "repository": "u/r", "views": 5}])
        added = gitlytics.automation._write_df_to_csv(df, csv_path)
        assert added == 1
        assert Path(csv_path).exists()
run_test("gitlytics.automation", "_write_df_to_csv", test_write_df_to_csv)

def test_run_sync_cycle():
    with tempfile.TemporaryDirectory() as td:
        df = pd.DataFrame([{"date": "2026-06-01", "repository": "u/r", "views": 5}])
        with patch("gitlytics.automation.fetch_traffic_data", return_value=df):
            gitlytics.automation.run_sync_cycle("tok", data_dir=td, output_mode="monthly")
            csvs = list(Path(td).glob("traffic_*.csv"))
            assert len(csvs) == 1
run_test("gitlytics.automation", "run_sync_cycle", test_run_sync_cycle)

def test_interruptible_sleep():
    with patch("time.sleep") as mock_sleep:
        gitlytics.automation._interruptible_sleep(0.5)
        assert mock_sleep.called
run_test("gitlytics.automation", "_interruptible_sleep", test_interruptible_sleep)

def test_run_sync():
    with patch("gitlytics.automation.run_sync_cycle") as mock_cycle:
        gitlytics.automation.run_sync("tok", schedule_cron=None)
        mock_cycle.assert_called_once()
run_test("gitlytics.automation", "run_sync", test_run_sync)


# ══════════════════════════════════════════════════════════════════
# 5. gitlytics.api - 11 functions
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[5/6] gitlytics.api (11 functions){RESET}")

from fastapi.testclient import TestClient
client = TestClient(gitlytics.api.app)

def test_require_upload_access():
    mock_req = MagicMock()
    mock_req.headers = {"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"}
    gitlytics.api._require_upload_access(mock_req)  # should not raise
run_test("gitlytics.api", "_require_upload_access", test_require_upload_access)

def test_get_token():
    assert gitlytics.api._get_token(" explicit ") == "explicit"
    with patch.dict(os.environ, {"GITLYTICS_TOKEN": "env_tok", "GITLYTICS_ALLOW_ENV_TOKEN": "1"}):
        assert gitlytics.api._get_token("") == "env_tok"
run_test("gitlytics.api", "_get_token", test_get_token)

def test_validate_token_cached():
    with patch("gitlytics.api.validate_token", return_value=(True, "cached_user")) as mock_v:
        ok1, u1 = gitlytics.api._validate_token_cached("tok_c")
        ok2, u2 = gitlytics.api._validate_token_cached("tok_c")
        assert ok1 and ok2 and u1 == u2 == "cached_user"
        assert mock_v.call_count == 1
run_test("gitlytics.api", "_validate_token_cached", test_validate_token_cached)

def test_get_config():
    r = client.get("/api/config")
    assert r.status_code == 200
    assert "has_token" in r.json()
run_test("gitlytics.api", "get_config", test_get_config)

def test_auth():
    with patch("gitlytics.api.validate_token", return_value=(True, "test_user")):
        with patch("gitlytics.api.get_user_profile", return_value={"login": "test_user", "name": "Tester", "avatar_url": ""}):
            r = client.post("/api/auth", json={"token": "valid_tok"})
            assert r.status_code == 200
            assert r.json()["authenticated"] is True
run_test("gitlytics.api", "auth", test_auth)

def test_get_username_data():
    with patch("gitlytics.api.get_public_user", return_value={"login": "torvalds"}):
        with patch("gitlytics.api.get_public_repos", return_value=[]):
            r = client.post("/api/username", json={"username": "torvalds"})
            assert r.status_code == 200
            assert "profile" in r.json()
run_test("gitlytics.api", "get_username_data", test_get_username_data)

def test_get_traffic():
    with patch("gitlytics.api._validate_token_cached", return_value=(True, "user")):
        with patch("gitlytics.api.fetch_traffic_data", return_value=pd.DataFrame()):
            r = client.post("/api/traffic", json={"token": "valid_tok"})
            assert r.status_code == 200
run_test("gitlytics.api", "get_traffic", test_get_traffic)

def test_get_star_history_endpoint():
    with patch("gitlytics.api.fetch_star_history", return_value=[{"date": "2026-01-01", "total": 5}]):
        r = client.get("/api/star-history?owner=u&repo=r", headers={"authorization": "Bearer tok"})
        assert r.status_code == 200
        assert r.json()["points"] == [{"date": "2026-01-01", "total": 5}]
run_test("gitlytics.api", "get_star_history_endpoint", test_get_star_history_endpoint)

def test_upload_csv():
    csv_bytes = b"date,repository,views\n2026-06-01,u/r,5\n"
    r = client.post(
        "/api/upload-csv",
        files={"file": ("traffic.csv", csv_bytes, "text/csv")},
        headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"},
    )
    assert r.status_code == 200
run_test("gitlytics.api", "upload_csv", test_upload_csv)

def test_serve_index():
    r = client.get("/")
    assert r.status_code in (200, 503)
run_test("gitlytics.api", "serve_index", test_serve_index)

def test_serve_spa_fallback():
    r = client.get("/dashboard/view")
    assert r.status_code in (200, 404, 503)
run_test("gitlytics.api", "serve_spa_fallback", test_serve_spa_fallback)


# ══════════════════════════════════════════════════════════════════
# 6. gitlytics.cli - 2 functions
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}[6/6] gitlytics.cli (2 functions){RESET}")

def test_parse_repo_names():
    assert gitlytics.cli.parse_repo_names(None) is None
    assert gitlytics.cli.parse_repo_names("") is None
    assert gitlytics.cli.parse_repo_names("u/r1, u/r2") == ["u/r1", "u/r2"]
run_test("gitlytics.cli", "parse_repo_names", test_parse_repo_names)

def test_main():
    with patch.object(sys, "argv", ["gitlytics"]):
        with patch("sys.exit") as mock_exit:
            gitlytics.cli.main()
            mock_exit.assert_called_with(0)
run_test("gitlytics.cli", "main", test_main)


# ══════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'='*60}{RESET}")
total = len(results)
passed = sum(1 for _, _, ok, _ in results if ok)
failed = total - passed

print(f"{BOLD}TOTAL FUNCTIONS TESTED: {total}{RESET}")
print(f"{GREEN}PASSED: {passed}{RESET}")
if failed:
    print(f"{RED}FAILED: {failed}{RESET}")
    for mod, fn, ok, err in results:
        if not ok:
            print(f"  - {mod}.{fn}: {err}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}ALL {total} FUNCTIONS PASSED SUCCESSFULLY!{RESET}")
    sys.exit(0)
