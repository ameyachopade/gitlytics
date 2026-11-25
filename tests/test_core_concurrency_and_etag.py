"""Concurrency, ETag conditional caching, hoisted JSON, and GZip middleware tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitlytics.core import (
    _clear_etag_cache,
    _safe_get,
    build_tidy_rows,
    fetch_star_history,
    fetch_traffic_rows,
    get_deep_repo_stats,
    get_public_repos,
    get_repo_traffic,
    make_headers,
)

SRC = Path(__file__).resolve().parent.parent / "src"


def _resp(status: int, *, body=None, headers=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = text
    return r


def test_make_headers_includes_gzip_accept_encoding():
    headers = make_headers("tok_123")
    assert headers.get("Accept-Encoding") == "gzip, deflate"


def test_safe_get_etag_cache_sends_if_none_match_and_returns_cached_on_304():
    _clear_etag_cache()
    seen_headers: list[dict] = []

    def fake_http_get(url, headers=None, params=None, timeout=10):
        seen_headers.append(dict(headers or {}))
        if len(seen_headers) == 1:
            return _resp(200, body={"views": [{"count": 42}]}, headers={"ETag": '"abc123"'})
        return _resp(304, body={}, headers={"ETag": '"abc123"'})

    url = "https://api.github.com/repos/owner/repo/traffic/views"
    headers = make_headers("tok_etag_test")

    with patch("gitlytics.core.client._http_get", side_effect=fake_http_get):
        data1, status1 = _safe_get(url, headers)
        data2, status2 = _safe_get(url, headers)

    assert status1 == 200
    assert data1 == {"views": [{"count": 42}]}
    assert status2 == 200
    assert data2 == {"views": [{"count": 42}]}
    assert "If-None-Match" not in seen_headers[0]
    assert seen_headers[1].get("If-None-Match") == '"abc123"'

    # Different token must not share cached ETag entry.
    with patch("gitlytics.core.client._http_get", side_effect=fake_http_get):
        seen_headers.clear()
        data3, status3 = _safe_get(url, make_headers("other_token"))
    assert status3 == 200
    assert "If-None-Match" not in seen_headers[0]

    # _clear_etag_cache evicts cached entries cleanly.
    _clear_etag_cache()
    with patch("gitlytics.core.client._http_get", side_effect=fake_http_get):
        seen_headers.clear()
        _safe_get(url, headers)
    assert "If-None-Match" not in seen_headers[0]


def test_get_repo_traffic_runs_five_endpoints_concurrently():
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def slow_safe_get(url, headers, params=None):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        if url.endswith("/traffic/views"):
            return {"views": []}, 200
        if url.endswith("/traffic/clones"):
            return {"clones": []}, 200
        return [], 200

    def slow_releases(url, headers, params=None):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        return 7

    t0 = time.perf_counter()
    with (
        patch("gitlytics.core.client._safe_get", side_effect=slow_safe_get),
        patch("gitlytics.core.client._count_via_link_header", side_effect=slow_releases),
    ):
        traffic = get_repo_traffic("tok", "owner/repo")
    elapsed = time.perf_counter() - t0

    assert traffic["_fetch_ok"] is True
    assert traffic["releases"] == 7
    assert state["max"] >= 3
    assert elapsed < 0.35, f"Expected concurrent execution (<0.35s), got {elapsed:.3f}s"


def test_get_deep_repo_stats_runs_four_endpoints_concurrently():
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def mark_sleep():
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1

    def slow_safe_get(url, headers, params=None):
        mark_sleep()
        if url.endswith("/stats/commit_activity"):
            return [{"total": 50}, {"total": 25}], 200
        if url.endswith("/community/profile"):
            return {"files": {"readme": {"url": "x"}, "license": {"url": "y"}}}, 200
        return {}, 404

    def slow_count(url, headers, params=None):
        mark_sleep()
        return 4

    def slow_http_get(url, headers=None, params=None, timeout=10):
        mark_sleep()
        return _resp(
            200,
            body=[{"published_at": "2026-10-01T00:00:00Z"}],
            headers={"Link": '<https://api.github.com?page=3>; rel="last"'},
        )

    t0 = time.perf_counter()
    with (
        patch("gitlytics.core.client._safe_get", side_effect=slow_safe_get),
        patch("gitlytics.core.client._count_via_link_header", side_effect=slow_count),
        patch("gitlytics.core.client._http_get", side_effect=slow_http_get),
    ):
        stats = get_deep_repo_stats("tok", "owner/repo")
    elapsed = time.perf_counter() - t0

    assert stats["total_commits"] == 75
    assert stats["open_prs"] == 4
    assert stats["total_releases"] == 3
    assert stats["has_readme"] is True
    assert stats["has_license"] is True
    assert state["max"] >= 3
    assert elapsed < 0.35, f"Expected concurrent execution (<0.35s), got {elapsed:.3f}s"


def test_fetch_star_history_probes_page_1_then_fetches_remaining_concurrently():
    # 1) Page 1 404 restriction stops in 1 stargazer call.
    meta_restricted = _resp(
        200, body={"stargazers_count": 5000, "created_at": "2020-01-01T00:00:00Z"}
    )
    restricted_page = _resp(404, body={})
    with patch(
        "gitlytics.core.client._http_get",
        side_effect=[meta_restricted, restricted_page],
    ) as mock_restricted:
        pts = fetch_star_history("restrict-owner", "restrict-repo")
    assert len(pts) == 2
    assert mock_restricted.call_count == 2  # metadata + page 1 only

    # 2) Unrestricted large repo probes page 1 first, then runs pages 2..10 concurrently.
    state = {"active": 0, "max": 0, "pages": []}
    lock = threading.Lock()

    def fake_http(url, headers=None, params=None, timeout=10):
        if "/stargazers" not in url:
            return _resp(
                200,
                body={"stargazers_count": 5000, "created_at": "2019-01-01T00:00:00Z"},
            )
        page = int((params or {}).get("page", 1))
        with lock:
            state["pages"].append(page)
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.08)
        with lock:
            state["active"] -= 1
        year = 2020 + (page % 6)
        month = (page % 12) + 1
        return _resp(
            200,
            body=[{"starred_at": f"{year}-{month:02d}-10T00:00:00Z"}] * 100,
        )

    t0 = time.perf_counter()
    with patch("gitlytics.core.client._http_get", side_effect=fake_http):
        points = fetch_star_history("fast-owner", "fast-repo")
    elapsed = time.perf_counter() - t0

    assert state["pages"][0] == 1  # Page 1 probed first
    assert len(state["pages"]) == 10
    assert state["max"] >= 3  # Pages 2..10 ran concurrently
    assert points[-1]["total"] == 5000
    assert elapsed < 0.5, f"Expected parallel star pages (<0.5s), got {elapsed:.3f}s"


def test_get_public_repos_fetches_updated_and_starred_concurrently():
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def slow_updated(username, per_page, token=None):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        return [{"full_name": "u/recent", "stargazers_count": 5}]

    def slow_starred(username, per_page, token=None):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.1)
        with lock:
            state["active"] -= 1
        return [{"full_name": "u/top", "stargazers_count": 50}]

    t0 = time.perf_counter()
    with (
        patch("gitlytics.core.users._fetch_public_repos_by_updated", side_effect=slow_updated),
        patch("gitlytics.core.users._fetch_public_repos_by_stars", side_effect=slow_starred),
    ):
        repos = get_public_repos("octocat", max_repos=10)
    elapsed = time.perf_counter() - t0

    assert [r["full_name"] for r in repos] == ["u/top", "u/recent"]
    assert state["max"] == 2
    assert elapsed < 0.25


def test_build_tidy_rows_hoists_json_dumps_out_of_15_day_loop():
    repo = {
        "full_name": "owner/repo",
        "private": False,
        "stargazers_count": 10,
        "forks_count": 2,
        "topics": ["python", "cli"],
    }
    traffic = {
        "views": {"views": []},
        "clones": {"clones": []},
        "referrers": [{"referrer": "github.com", "count": 5, "uniques": 3}],
        "paths": [{"path": "/README.md", "count": 5, "uniques": 3}],
        "releases": 1,
    }
    real_dumps = json.dumps
    with patch("gitlytics.core.traffic.json.dumps", side_effect=real_dumps) as spy:
        rows = build_tidy_rows(repo, traffic)

    assert len(rows) == 15
    assert spy.call_count == 3  # once each for topics, referrers, paths (not 45x)


def test_fetch_traffic_rows_parallelizes_explicit_repo_lookups_and_avoids_pandas():
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def slow_repo(token, name):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.08)
        with lock:
            state["active"] -= 1
        return {"full_name": name, "private": False, "stargazers_count": 1, "forks_count": 0}

    with (
        patch("gitlytics.core.traffic.get_single_repo", side_effect=slow_repo),
        patch(
            "gitlytics.core.traffic.get_repo_traffic",
            return_value={"views": {"views": []}, "clones": {"clones": []}, "referrers": [], "paths": [], "releases": 0},
        ),
    ):
        rows = fetch_traffic_rows("tok", repo_names=["o/r1", "o/r2", "o/r3"])

    assert len(rows) == 45
    assert state["max"] >= 2

    # Verify fetch_traffic_rows + run_sync_cycle never import pandas in a clean process.
    code = (
        "import json, sys, tempfile\n"
        "from unittest.mock import patch\n"
        "from gitlytics.automation.scheduler import run_sync_cycle\n"
        "with tempfile.TemporaryDirectory() as td:\n"
        "    with patch('gitlytics.core.traffic.get_all_repos', return_value=[{'full_name': 'o/r', 'private': False}]):\n"
        "        with patch('gitlytics.core.traffic.get_repo_traffic', return_value={'views': {'views': []}, 'clones': {'clones': []}, 'referrers': [], 'paths': [], 'releases': 0}):\n"
        "            count = run_sync_cycle('tok', data_dir=td)\n"
        "print(json.dumps({'count': count, 'pandas': 'pandas' in sys.modules, 'numpy': 'numpy' in sys.modules, 'croniter': 'croniter' in sys.modules}))\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", code],
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=60,
    )
    res = json.loads(out)
    assert res == {"count": 15, "pandas": False, "numpy": False, "croniter": False}


def test_dashboard_app_registers_gzip_middleware():
    from fastapi.middleware.gzip import GZipMiddleware

    from gitlytics.api import app as api_app

    gzip_mw = next(
        (m for m in api_app.user_middleware if m.cls is GZipMiddleware), None
    )
    assert gzip_mw is not None
    assert gzip_mw.kwargs.get("minimum_size") == 1024
    assert gzip_mw.kwargs.get("compresslevel") == 4
