# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Offline regression tests for API auth, isolation, client, CLI, and release controls."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from gitlytics.api import app
from gitlytics.automation.storage import export_json_database
from gitlytics.cli import main as cli_main
from gitlytics.core import client as core_client
from gitlytics.core.repos import get_deep_repo_stats
from gitlytics.core.traffic import get_repo_traffic
from gitlytics.core.users import get_public_repos
from gitlytics.io.csv import process_uploaded_csv
from gitlytics.io.payloads import build_json_payload

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_traffic_scopes_data_dir_to_authenticated_owner(tmp_path):
    """POST /api/traffic only returns GITLYTICS_DATA_DIR rows owned by the caller."""
    (tmp_path / "traffic_2026-01.csv").write_text(
        "date,repository,is_private,views\n"
        "2026-01-01,alice/secret-repo,True,42\n"
        "2026-01-01,bob/public-repo,False,7\n",
        encoding="utf-8",
    )
    with (
        patch.dict(os.environ, {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False),
        patch("gitlytics.api.cache._validate_token_cached", return_value=(True, "bob")),
        patch("gitlytics.api.cache.fetch_traffic_data", return_value=pd.DataFrame()),
        patch("gitlytics.api.cache.fetch_deep_stats_for_top", return_value={}),
    ):
        resp = TestClient(app).post("/api/traffic", json={"token": "bob-token"})

    assert resp.status_code == 200
    repos = {item["repository"] for item in resp.json()}
    assert repos == {"bob/public-repo"}


def test_upload_key_cannot_be_bypassed_with_matching_origin_host():
    """When GITLYTICS_UPLOAD_KEY is set, matching Origin/Host must not bypass X-Upload-Key."""
    csv_bytes = b"date,repository,views\n2026-01-01,alice/repo,5\n"
    with patch.dict(os.environ, {"GITLYTICS_UPLOAD_KEY": "top-secret"}, clear=False):
        resp = TestClient(app).post(
            "/api/upload-csv",
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            headers={"Host": "localhost:8000", "Origin": "http://localhost:8000"},
        )
    assert resp.status_code == 403


def test_dns_rebinding_host_header_is_rejected():
    """Untrusted external Host headers matching Origin must be rejected on browser requests."""
    with patch.dict(
        os.environ,
        {"GITLYTICS_TOKEN": "ghp_secret", "GITLYTICS_ALLOW_ENV_TOKEN": "1"},
        clear=False,
    ):
        resp = TestClient(app).post(
            "/api/traffic",
            json={},
            headers={
                "Host": "rebind.attacker.example:8000",
                "Origin": "http://rebind.attacker.example:8000",
            },
        )
    assert resp.status_code == 403


def test_upload_csv_enforces_cardinality_cap(tmp_path):
    """process_uploaded_csv rejects CSVs that exceed the unique repository limit."""
    csv_path = tmp_path / "many_repos.csv"
    lines = ["date,repository,views"] + [
        f"2026-01-01,owner/r{i},1" for i in range(2005)
    ]
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unique repositories"):
        process_uploaded_csv(csv_path)


def test_public_repos_search_includes_is_public_and_filters_private():
    """get_public_repos passes is:public to GitHub Search and drops private items."""
    captured_params = {}

    def fake_get(url, headers=None, params=None, timeout=10):
        resp = MagicMock()
        resp.status_code = 200
        if "/search/repositories" in url:
            captured_params.update(params or {})
            resp.json.return_value = {
                "items": [
                    {
                        "name": "pub",
                        "full_name": "alice/pub",
                        "private": False,
                        "stargazers_count": 10,
                    },
                    {
                        "name": "priv",
                        "full_name": "alice/priv",
                        "private": True,
                        "stargazers_count": 99,
                    },
                ]
            }
        else:
            resp.json.return_value = []
        return resp

    with patch("gitlytics.core.client._http_get", side_effect=fake_get):
        repos = get_public_repos("alice", token="ghp_test")

    assert "is:public" in captured_params.get("q", "")
    assert [r["full_name"] for r in repos] == ["alice/pub"]


def test_export_public_only_drops_all_rows_of_private_repo(tmp_path):
    """Older blank or False rows for a private repo must not leak into public exports."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "traffic_2026-03.csv").write_text(
        "date,repository,is_private,views\n"
        "2026-03-01,alice/secret,,15\n"
        "2026-03-02,alice/secret,False,20\n"
        "2026-03-15,alice/secret,True,30\n"
        "2026-03-15,alice/public,False,5\n",
        encoding="utf-8",
    )
    export_path = tmp_path / "public.json"
    export_json_database(str(data_dir), str(export_path), export_public_only=True)
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert [item["repository"] for item in exported] == ["alice/public"]

    df = pd.read_csv(data_dir / "traffic_2026-03.csv")
    payload = build_json_payload(df, return_format="summary", export_public_only=True)
    assert list(payload["repositories"].keys()) == ["alice/public"]


def test_unvalidated_full_name_rejected_in_deep_stats_and_traffic():
    """get_deep_repo_stats and get_repo_traffic reject path or fragment injection in full_name."""
    for bad_name in (
        "owner/repo/tarball/HEAD#",
        "../../user",
        "owner/repo?x=1",
        "justrepo",
    ):
        with pytest.raises(ValueError):
            get_deep_repo_stats("tok", bad_name)
        with pytest.raises(ValueError):
            get_repo_traffic("tok", bad_name)


def test_operation_deadline_is_isolated_across_threads():
    """Concurrent threads exiting out of LIFO order must not poison _current_deadline."""
    t1_entered = threading.Event()
    t2_entered = threading.Event()
    t1_exited = threading.Event()

    def worker_1():
        with core_client._operation_deadline(0.05):
            t1_entered.set()
            t2_entered.wait(timeout=2)
        t1_exited.set()

    def worker_2():
        t1_entered.wait(timeout=2)
        with core_client._operation_deadline(0.05):
            t2_entered.set()
            t1_exited.wait(timeout=2)

    th1 = threading.Thread(target=worker_1)
    th2 = threading.Thread(target=worker_2)
    th1.start()
    th2.start()
    th1.join(timeout=5)
    th2.join(timeout=5)

    time.sleep(0.08)
    with core_client._operation_deadline(60.0) as dl:
        assert dl > time.monotonic() + 30.0


def test_cli_dashboard_does_not_promote_ambient_github_token():
    """gitlytics dashboard must not pass ambient GITHUB_TOKEN as an explicit token."""
    captured = {}

    def fake_serve(**kwargs):
        captured.update(kwargs)

    with (
        patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_ambient"}, clear=False),
        patch("gitlytics.cli.commands.serve_dashboard", side_effect=fake_serve),
    ):
        os.environ.pop("GITLYTICS_TOKEN", None)
        cli_main(["dashboard"])

    assert captured["token"] is None


def test_cli_dashboard_gates_all_non_loopback_hosts_and_preserves_explicit_ip():
    """Non-loopback hosts require --public-bind, and --public-bind keeps an explicit IP."""
    with pytest.raises(SystemExit) as exc:
        cli_main(["dashboard", "--host", "::"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        cli_main(["dashboard", "--host", "192.168.1.50"])
    assert exc.value.code == 2

    captured = {}
    with patch(
        "gitlytics.cli.commands.serve_dashboard",
        side_effect=lambda **kw: captured.update(kw),
    ):
        cli_main(["dashboard", "--host", "192.168.1.50", "--public-bind"])
    assert captured["host"] == "192.168.1.50"


def test_publish_workflow_isolates_oidc_publish_from_build_and_test():
    """publish.yml must build/test once in the test job and only download/publish in pypi-publish."""
    text = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text(
        encoding="utf-8"
    )
    publish_section = text.split("  pypi-publish:", 1)[1]
    assert "actions/upload-artifact@" in text
    assert "actions/download-artifact@" in publish_section
    assert "actions/checkout@" not in publish_section
    assert "python -m build" not in publish_section
    assert "pytest" not in publish_section


def test_spa_fallback_blocks_internal_headers_file():
    """GET /_headers must not serve the packaged static _headers file."""
    resp = TestClient(app).get("/_headers")
    assert resp.status_code == 404
