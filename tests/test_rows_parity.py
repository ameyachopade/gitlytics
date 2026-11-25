"""Parity and zero-pandas tests for gitlytics.io.rows, storage, and MCP CSV vault."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from gitlytics.automation.storage import _merge_csv_locked, export_json_database
from gitlytics.io.payloads import build_json_payload, build_react_payload
from gitlytics.io.rows import (
    build_json_payload_rows,
    build_react_payload_rows,
    filter_public_only_rows,
    normalize_field,
    read_csv_rows,
)
from gitlytics.mcp.server import LocalMcpServer

SRC = Path(__file__).resolve().parent.parent / "src"


def _strip_fetched_at(payload: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if k != "fetched_at"} for item in payload]


def test_build_react_payload_rows_exact_parity_with_dataframe():
    rows = [
        {
            "date": "2026-10-02",
            "repository": "owner/zeta",
            "is_private": "False",
            "views": "1,234",
            "unique_visitors": "456",
            "clones": "12",
            "unique_cloners": "5",
            "stars": "1,000",
            "forks": "50",
            "watchers_count": "25",
            "open_issues_count": "4",
            "language": "Python",
            "topics": json.dumps(["python", "analytics"]),
            "pushed_at": "2026-10-02T12:00:00Z",
            "created_at": "2024-01-01T00:00:00Z",
            "top_referrer": "github.com",
            "top_referrer_views": "800",
            "top_referrer_uniques": "300",
            "top_path": "/README.md",
            "top_path_views": "900",
            "top_path_uniques": "350",
            "_raw_referrers": None,
            "_raw_paths": float("nan"),
        },
        {
            "date": "2026-10-01",
            "repository": "owner/zeta",
            "is_private": False,
            "views": 100,
            "unique_visitors": 50,
            "clones": "",
            "unique_cloners": None,
            "stars": 999,
            "forks": 49,
            "watchers_count": 25,
            "open_issues_count": 3,
            "language": "Python",
            "topics": json.dumps(["python"]),
            "pushed_at": "2026-10-01T12:00:00Z",
            "created_at": "2024-01-01T00:00:00Z",
            "top_referrer": "google.com",
            "top_referrer_views": 40,
            "top_referrer_uniques": 20,
            "top_path": "/docs",
            "top_path_views": 30,
            "top_path_uniques": 15,
            "_raw_referrers": json.dumps([{"referrer": "google.com", "count": 40, "uniques": 20}]),
            "_raw_paths": json.dumps([{"path": "/docs", "count": 30, "uniques": 15}]),
        },
        {
            "date": "2026-10-01",
            "repository": "owner/alpha",
            "is_private": "true",
            "views": 10,
            "unique_visitors": 5,
            "clones": 2,
            "unique_cloners": 1,
            "stars": 5,
            "forks": 1,
            "top_referrer": "direct",
            "top_referrer_views": 10,
            "top_referrer_uniques": 5,
            "top_path": "/",
            "top_path_views": 10,
            "top_path_uniques": 5,
        },
    ]
    deep_stats = {
        "owner/zeta": {
            "total_commits": 321,
            "open_prs": 2,
            "total_releases": 9,
            "last_release_at": "2026-09-01T00:00:00Z",
            "has_readme": True,
            "has_license": True,
            "has_contributing": None,
            "has_code_of_conduct": False,
        }
    }

    df = pd.DataFrame(rows)
    from_rows = build_react_payload_rows(rows, deep_stats=deep_stats)
    from_df = build_react_payload(df, deep_stats=deep_stats)
    from_payload_rows = build_react_payload(rows, deep_stats=deep_stats)

    assert _strip_fetched_at(from_rows) == _strip_fetched_at(from_df)
    assert _strip_fetched_at(from_rows) == _strip_fetched_at(from_payload_rows)

    zeta = next(r for r in from_rows if r["repository"] == "owner/zeta")
    assert zeta["is_private"] is False
    assert zeta["views"] == 1334
    assert zeta["unique_visitors"] == 506
    assert zeta["clones"] == 12
    assert zeta["stars"] == 1000
    assert zeta["forks"] == 50
    assert zeta["topics"] == ["python", "analytics"]
    assert zeta["_referrers"] == [{"referrer": "google.com", "count": 40, "uniques": 20}]
    assert zeta["_paths"] == [{"path": "/docs", "count": 30, "uniques": 15}]
    assert zeta["total_commits"] == 321
    assert "has_contributing" not in zeta
    assert [d["timestamp"] for d in zeta["_daily_views"]] == ["2026-10-01", "2026-10-02"]

    alpha = next(r for r in from_rows if r["repository"] == "owner/alpha")
    assert alpha["is_private"] is True
    assert alpha["_referrers"] == [{"referrer": "direct", "count": 10, "uniques": 5}]


def test_build_json_payload_rows_exact_parity_with_dataframe():
    rows = [
        {
            "date": "2026-10-02",
            "repository": "owner/pub",
            "is_private": "False",
            "views": "1,200",
            "unique_visitors": "300",
            "clones": "40",
            "unique_cloners": "15",
            "stars": "50",
            "forks": "10",
            "top_referrer": "github.com",
            "top_path": "/README.md",
        },
        {
            "date": "2026-10-01",
            "repository": "owner/pub",
            "is_private": False,
            "views": 100,
            "unique_visitors": 20,
            "clones": 5,
            "unique_cloners": 2,
            "stars": 48,
            "forks": 9,
            "top_referrer": "google.com",
            "top_path": "/docs",
        },
        {
            "date": "2026-10-01",
            "repository": "owner/priv",
            "is_private": False,
            "views": 500,
            "unique_visitors": 100,
            "clones": 10,
            "unique_cloners": 5,
            "stars": 1,
            "forks": 0,
        },
        {
            "date": "2026-10-02",
            "repository": "owner/priv",
            "is_private": "True",
            "views": 600,
            "unique_visitors": 110,
            "clones": 12,
            "unique_cloners": 6,
            "stars": 2,
            "forks": 0,
        },
    ]
    df = pd.DataFrame(rows)
    for fmt in ("timeseries", "summary"):
        for pub_only in (True, False):
            r_rows = build_json_payload_rows(rows, return_format=fmt, export_public_only=pub_only)
            r_df = build_json_payload(df, return_format=fmt, export_public_only=pub_only)
            assert r_rows == r_df
            if pub_only:
                assert list(r_rows["repositories"].keys()) == ["owner/pub"]
            else:
                assert set(r_rows["repositories"].keys()) == {"owner/pub", "owner/priv"}


def test_read_csv_rows_handles_bom_legacy_aliases_and_duplicates(tmp_path):
    csv_path = tmp_path / "legacy.csv"
    csv_path.write_text(
        "\ufeffDate,date,Repo_Name,Total Views,views,Total Clones,Is_Private\n"
        '2026-10-01,,owner/a,"1,234",,7,False\n'
        ",2026-10-02,owner/a,,55,3,False\n",
        encoding="utf-8",
    )
    cols, rows = read_csv_rows(csv_path)
    assert normalize_field("\ufeff Date ") == "date"
    assert "date" in cols
    assert "repository" in cols
    assert "views" in cols
    assert "clones" in cols
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-10-01"
    assert rows[0]["repository"] == "owner/a"
    assert rows[0]["views"] == "1,234"
    assert rows[1]["date"] == "2026-10-02"
    assert rows[1]["views"] == "55"

    payload = build_react_payload_rows(rows)
    assert len(payload) == 1
    assert payload[0]["views"] == 1289
    assert payload[0]["clones"] == 10
    assert payload[0]["is_private"] is False


def test_filter_public_only_rows_strips_all_rows_of_private_repo():
    rows = [
        {"date": "2026-10-01", "repository": "o/secret", "is_private": "", "views": 10},
        {"date": "2026-10-02", "repository": "o/secret", "is_private": "False", "views": 20},
        {"date": "2026-10-03", "repository": "o/secret", "is_private": "True", "views": 30},
        {"date": "2026-10-01", "repository": "o/public", "is_private": np.bool_(False), "views": 5},
    ]
    filtered = filter_public_only_rows(rows)
    assert len(filtered) == 1
    assert filtered[0]["repository"] == "o/public"


def test_storage_merge_accepts_row_lists_and_exports_json_without_pandas(tmp_path):
    csv_path = tmp_path / "traffic_2026-10.csv"
    incoming_rows = [
        {"date": "2026-10-02", "repository": "o/b", "is_private": False, "views": 10, "clones": 2},
        {"date": "2026-10-01", "repository": "o/a", "is_private": False, "views": 5, "clones": 1},
    ]
    added = _merge_csv_locked(incoming_rows, str(csv_path))
    assert added == 2

    # Monotonic zero guard with list-of-dicts input
    added2 = _merge_csv_locked(
        [{"date": "2026-10-01", "repository": "o/a", "is_private": False, "views": 0, "clones": 3}],
        str(csv_path),
    )
    assert added2 == 0

    out_json = tmp_path / "export.json"
    export_json_database(str(tmp_path), str(out_json), export_public_only=True)
    exported = json.loads(out_json.read_text(encoding="utf-8"))
    by_repo = {r["repository"]: r for r in exported}
    assert by_repo["o/a"]["views"] == 5
    assert by_repo["o/a"]["clones"] == 3
    assert by_repo["o/b"]["views"] == 10


def test_mcp_query_csv_vault_runs_without_importing_pandas(tmp_path):
    csv_file = tmp_path / "traffic_2026-11.csv"
    csv_file.write_text(
        "\ufeffdate,repository,views,unique_visitors,clones,unique_cloners\n"
        '2026-11-20,owner/repo,"1,000",500,40,20\n'
        "2026-11-21,owner/repo,250,125,10,5\n",
        encoding="utf-8",
    )

    code = (
        "import json, sys\n"
        "from gitlytics.mcp.server import LocalMcpServer\n"
        f"srv = LocalMcpServer(data_dir={str(tmp_path)!r})\n"
        "res = srv._query_csv_vault(repo_name='owner/repo', days=3650)\n"
        "print(json.dumps({"
        "'row_count': res['row_count'], "
        "'views': res['summary']['total_views'], "
        "'clones': res['summary']['total_clones'], "
        "'pandas': 'pandas' in sys.modules, "
        "'numpy': 'numpy' in sys.modules"
        "}))\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", code],
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=60,
    )
    data = json.loads(out)
    assert data == {
        "row_count": 2,
        "views": 1250,
        "clones": 50,
        "pandas": False,
        "numpy": False,
    }

    # Verify repeated queries hit cache and detect file modification
    srv = LocalMcpServer(data_dir=str(tmp_path))
    r1 = srv._query_csv_vault(repo_name="owner/repo", days=3650)
    r2 = srv._query_csv_vault(repo_name="owner/repo", days=3650)
    assert r1["summary"] == r2["summary"]
    csv_file.write_text(
        "date,repository,views,unique_visitors,clones,unique_cloners\n"
        "2026-11-20,owner/repo,2000,500,40,20\n"
        "2026-11-21,owner/repo,250,125,10,5\n",
        encoding="utf-8",
    )
    r3 = srv._query_csv_vault(repo_name="owner/repo", days=3650)
    assert r3["summary"]["total_views"] == 2250
