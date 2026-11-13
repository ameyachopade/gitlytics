"""
tests/test_audit_final_lib_regressions.py
Regression tests for all Audit Final library findings (AF-LIB-01..AF-LIB-16)
and library sides of AF-XREPO-05/07/08.

All tests are offline.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from gitlytics import _coerce_metrics, core, fetch_traffic, process, sync
from gitlytics.automation import _interruptible_sleep, _merge_csv_locked, export_json_database
from gitlytics.core import (
    GitHubRateLimitError,
    StarHistoryFetchError,
    TokenExpiredError,
    _safe_get,
    fetch_star_history,
    fetch_traffic_data,
    get_all_repos,
    get_deep_repo_stats,
    pad_traffic_data,
    print_repo_table,
    validate_token,
)

ROOT = Path(__file__).resolve().parent.parent


def _resp(status: int, *, body=None, headers=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.headers = headers or {}
    r.text = text
    return r


# ── AF-LIB-01: mixed/uppercase CSV headers do not lose rows or crash export ──

def test_af_lib_01_merge_and_export_preserve_mixed_case_headers(tmp_path):
    csv_path = tmp_path / "traffic_2025-06.csv"
    csv_path.write_text(
        "Date,Repository,Views,Is_Private\n"
        "2025-06-14,user/repo1,10,False\n"
        "2025-06-15,user/repo2,20,False\n",
        encoding="utf-8",
    )
    incoming = pd.DataFrame([
        {"date": "2025-06-14", "repository": "user/repo1", "views": 0, "is_private": False},
        {"date": "2025-06-16", "repository": "user/repo3", "views": 5, "is_private": False},
    ])
    added = _merge_csv_locked(incoming, str(csv_path))
    assert added == 1

    out_df = pd.read_csv(csv_path)
    assert len(out_df) == 3
    row_14 = out_df[out_df["Repository"] == "user/repo1"].iloc[0]
    assert int(row_14["Views"]) == 10  # monotonic zero protection held

    export_path = tmp_path / "out.json"
    export_json_database(str(tmp_path), str(export_path))
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert {r["repository"] for r in exported} == {"user/repo1", "user/repo2", "user/repo3"}


# ── AF-LIB-02: _to_bool handles numpy.integer / numpy.floating scalars ────────

def test_af_lib_02_to_bool_handles_numpy_numeric_scalars():
    assert process._to_bool(np.int64(1)) is True
    assert process._to_bool(np.int32(1)) is True
    assert process._to_bool(np.float32(1.0)) is True
    assert process._to_bool(np.int64(0)) is False
    assert process._to_bool(np.float32("nan")) is False

    df = pd.DataFrame([
        {"date": "2026-01-01", "repository": "o/priv", "is_private": np.int64(1), "views": 10},
        {"date": "2026-01-01", "repository": "o/pub", "is_private": np.int64(0), "views": 5},
    ])
    payload = process.build_json_payload(df, export_public_only=True)
    assert "o/priv" not in payload["repositories"]
    assert "o/pub" in payload["repositories"]


# ── AF-LIB-03: simple cross-origin POST rejected on local API routes ──────────

def test_af_lib_03_cross_origin_simple_post_blocked_on_traffic():
    from fastapi.testclient import TestClient
    from gitlytics import api

    client = TestClient(api.app)
    with patch.dict("os.environ", {"GITLYTICS_ALLOW_ENV_TOKEN": "1", "GITLYTICS_TOKEN": "secret"}, clear=False), \
         patch("gitlytics.api.fetch_traffic_data") as mock_fetch:
        resp = client.post(
            "/api/traffic",
            content=b"{}",
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        assert resp.status_code == 403
        mock_fetch.assert_not_called()

        # Even same-host with text/plain Content-Type is rejected to prevent form CSRF
        resp2 = client.post(
            "/api/traffic",
            content=b"{}",
            headers={"Origin": "http://testserver", "Content-Type": "text/plain"},
        )
        assert resp2.status_code == 415
        mock_fetch.assert_not_called()


# ── AF-LIB-04: deep stats cached, empty frames not cached, full sha256 keys ──

def test_af_lib_04_deep_stats_cached_and_empty_frame_uncached():
    from fastapi.testclient import TestClient
    from gitlytics import api

    frame = pd.DataFrame([
        {"date": "2026-01-01", "repository": "o/r", "is_private": False, "views": 10, "unique_visitors": 1, "clones": 0, "unique_cloners": 0, "stars": 1, "forks": 0}
    ])
    client = TestClient(api.app)
    with patch("gitlytics.api._validate_token_cached", return_value=(True, "user")), \
         patch("gitlytics.api.fetch_traffic_data", return_value=frame) as mock_tr, \
         patch("gitlytics.api.fetch_deep_stats_for_top", return_value={"o/r": {"total_commits": 5}}) as mock_deep:
        r1 = client.post("/api/traffic", json={"token": "tok-deep"})
        r2 = client.post("/api/traffic", json={"token": "tok-deep"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert mock_tr.call_count == 1
        assert mock_deep.call_count == 1
        expected_key = hashlib.sha256(b"tok-deep").hexdigest()
        assert expected_key in api._TRAFFIC_CACHE

    # Empty live frame must not stay cached for 120s
    api._TRAFFIC_CACHE.clear()
    with patch("gitlytics.api.fetch_traffic_data", side_effect=[pd.DataFrame(), frame]) as mock_tr2:
        assert api._cached_live_traffic("tok-recover").empty
        assert not api._cached_live_traffic("tok-recover").empty
        assert mock_tr2.call_count == 2


# ── AF-LIB-06: traffic 401/403/429 propagated + partial outage preserves CSV ─

def test_af_lib_06_traffic_errors_propagate_and_partial_live_preserves_csv(tmp_path):
    from fastapi.testclient import TestClient
    from gitlytics import api

    with patch("gitlytics.core.get_all_repos", return_value=[{"full_name": "o/r", "private": False}]), \
         patch("gitlytics.core._safe_get", return_value=({}, 429)):
        with pytest.raises(GitHubRateLimitError):
            fetch_traffic_data("tok")

    with patch("gitlytics.core.get_all_repos", return_value=[{"full_name": "o/r", "private": False}]), \
         patch("gitlytics.core._safe_get", return_value=({}, 403)):
        with pytest.raises(TokenExpiredError):
            fetch_traffic_data("tok")

    # Partial outage (views NaN/absent, clones updated) preserves stored CSV views
    pd.DataFrame([
        {"date": "2026-01-01", "repository": "o/r", "is_private": False, "views": 50, "clones": 10}
    ]).to_csv(tmp_path / "traffic_2026-01.csv", index=False)
    live_partial = pd.DataFrame([
        {"date": "2026-01-01", "repository": "o/r", "is_private": False, "clones": 12}
    ])
    with patch.dict("os.environ", {"GITLYTICS_DATA_DIR": str(tmp_path)}, clear=False), \
         patch("gitlytics.api._validate_token_cached", return_value=(True, "u")), \
         patch("gitlytics.api.fetch_traffic_data", return_value=live_partial), \
         patch("gitlytics.api.fetch_deep_stats_for_top", return_value={}):
        resp = TestClient(api.app).post("/api/traffic", json={"token": "t"})
    assert resp.status_code == 200
    assert resp.json()[0]["views"] == 50
    assert resp.json()[0]["clones"] == 12


# ── AF-LIB-07: abuse 403 -> 429; plain 403 != RateLimit; owner/repo validated ─

def test_af_lib_07_error_classification_and_repo_validation():
    abuse = _resp(403, text="You have triggered an abuse detection mechanism")
    with patch("gitlytics.core._http_get", side_effect=[abuse, abuse, abuse]), \
         patch("gitlytics.core.time.sleep"):
        _, status = _safe_get("https://api.github.com/user/repos", {})
    assert status == 429

    with patch("gitlytics.core._safe_get", return_value=({}, 403)):
        with pytest.raises(StarHistoryFetchError):
            fetch_star_history("octo", "repo")

    with pytest.raises(ValueError):
        fetch_star_history("octo", "repo?per_page=1")

    from gitlytics import cli
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_repo_names("owner/repo?foo=bar")


# ── AF-LIB-08: non-JSON 200 in validate_token & page-1 soft-error in get_all_repos

def test_af_lib_08_validate_token_non_json_and_page1_soft_error():
    bad = _resp(200)
    bad.json.side_effect = ValueError("not json")
    with patch("gitlytics.core._http_get", return_value=bad):
        ok, msg = validate_token("tok")
    assert ok is False and msg

    with patch("gitlytics.core._safe_get", return_value=({}, 200)):
        with pytest.raises(RuntimeError):
            get_all_repos("tok")


# ── AF-LIB-09: Path save_file + .JSON + empty fetch creates no 0-byte file ───

def test_af_lib_09_path_save_file_and_no_empty_file_created(tmp_path):
    df = pd.DataFrame([{"date": "2026-01-01", "repository": "o/r", "is_private": False, "views": 3}])
    out_json = tmp_path / "Result.JSON"
    with patch("gitlytics.fetch_traffic_data", return_value=df):
        fetch_traffic("tok", return_format="dataframe", save_file=out_json)
    assert out_json.exists()
    assert "account_totals" in json.loads(out_json.read_text(encoding="utf-8"))

    missing_out = tmp_path / "empty.csv"
    with patch("gitlytics.fetch_traffic_data", return_value=pd.DataFrame()):
        fetch_traffic("tok", return_format="dataframe", save_file=missing_out)
    assert not missing_out.exists()


# ── AF-LIB-10 / AF-XREPO-05/07/08: MANIFEST.in, workflows, pyproject, gitignore ─

def test_af_lib_10_and_xrepo_packaging_ci_license():
    assert (ROOT / "MANIFEST.in").exists()
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune tests" in manifest
    assert "global-exclude *.py[cod]" in manifest

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "OSI Approved :: Apache Software License" not in pyproject
    # PEP 639 (AF2-LIB-11): a machine-readable SPDX expression plus an
    # explicit license-files list, instead of the deprecated table form that
    # inlined the whole licence text into the wheel METADATA.
    assert 'license = "LicenseRef-Apache-2.0-with-Commons-Clause"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert 'license = { file = "LICENSE" }' not in pyproject
    # The free-text `License ::` classifier is mutually exclusive with a
    # PEP 639 license expression.
    assert "License :: Other/Proprietary License" not in pyproject
    assert "gitlytics.__pycache__" in pyproject

    tests_yml = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert '".[dev,dashboard]"' in tests_yml
    assert "timeout-minutes:" in tests_yml

    publish_yml = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "npm run build:local" in publish_yml
    assert ".[dev,dashboard]" in publish_yml

    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env.*" in gi and "*.lock" in gi and "traffic_*.csv" in gi
    assert not (ROOT / ".coverage").exists()


# ── AF-LIB-11: duplicate headers coalesced & pd.NA / pd.NaT nullish ──────────

def test_af_lib_11_duplicate_csv_headers_and_pandas_na():
    assert process._is_nullish(pd.NA) is True
    assert process._is_nullish(pd.NaT) is True

    csv_text = "Date,date,repository,Views,views\n2026-01-01,,o/r,10,\n"
    df = process.process_uploaded_csv(io.StringIO(csv_text))
    assert not df.columns.duplicated().any()
    assert int(df["views"].iloc[0]) == 10


# ── AF-LIB-12: yearly CSV sorted before monthly CSV ──────────────────────────

def test_af_lib_12_monthly_csv_wins_over_yearly_csv(tmp_path):
    pd.DataFrame([{"date": "2026-11-01", "repository": "o/r", "is_private": False, "views": 1}]).to_csv(
        tmp_path / "traffic_2026.csv", index=False
    )
    pd.DataFrame([{"date": "2026-11-01", "repository": "o/r", "is_private": False, "views": 99}]).to_csv(
        tmp_path / "traffic_2026-11.csv", index=False
    )
    out = tmp_path / "merged.json"
    export_json_database(str(tmp_path), str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data[0]["views"] == 99


# ── AF-LIB-13: defensive guards in core.py ───────────────────────────────────

def test_af_lib_13_core_defensive_guards():
    padded = pad_traffic_data({"views": None, "clones": {"clones": [None]}})
    assert len(padded) == 15

    with patch("gitlytics.core._safe_get", return_value=({}, 200)), \
         patch("gitlytics.core._count_via_link_header", return_value=0), \
         patch("gitlytics.core._http_get", return_value=_resp(200, body={})):
        stats = get_deep_repo_stats("tok", "o/r")
    assert stats["total_releases"] == 0

    df = pd.DataFrame([{"repository": "o/r", "views": float("inf"), "stars": "1,234"}])
    print_repo_table(df)


# ── AF-LIB-14: CLI dashboard ImportError, metrics all-unknown, sleep, username ─

def test_af_lib_14_cli_dashboard_and_metrics_and_username(capsys):
    from fastapi.testclient import TestClient
    from gitlytics import api, cli

    with pytest.raises(ValueError):
        _coerce_metrics(["totally_bogus"])

    with patch("gitlytics.cli.serve_dashboard", side_effect=ImportError("install [dashboard]")):
        with pytest.raises(SystemExit) as exc:
            cli.main(["dashboard"])
    assert exc.value.code == 1
    assert "Traceback" not in capsys.readouterr().err

    resp = TestClient(api.app).get("/api/username/-invalid-username")
    assert resp.status_code == 400

    # sync(data_dir=None) falls back cleanly instead of TypeError
    with patch("gitlytics.__init__" if False else "gitlytics.run_sync") as mock_rs:
        sync("tok", data_dir=None)
        assert mock_rs.called

    with patch("gitlytics.automation.time.sleep") as mock_sleep, \
         patch("gitlytics.automation.time.monotonic", side_effect=[0.0, 0.0, 0.6]):
        _interruptible_sleep(0.5)
        assert mock_sleep.called


# ── AF-LIB-15 / AF-LIB-16: README schema & run_all_functions_check.py ────────

def test_af_lib_15_and_16_readme_and_all_functions_harness():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "React_19" in readme
    assert "24 Daily Metrics" in readme
    assert "`releases`" in readme
    assert "Commons Clause" in readme
    assert 'host="127.0.0.1"' in readme

    res = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "run_all_functions_check.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + "\n" + res.stderr


# ── AF-XREPO-08: workspace hygiene, .gitignore coverage, no tracked secrets ──

def test_af_xrepo_08_workspace_hygiene_and_gitignore_coverage():
    workspace = ROOT.parent
    repos = {
        "gitlytics": ROOT,
        "gitlytics-backend": workspace / "gitlytics-backend",
        "gitlytics-frontend": workspace / "gitlytics-frontend",
        "gitlytics-metadata-pages": workspace / "gitlytics-metadata-pages",
        "gitlytics-traffic-monitor-automation": workspace / "gitlytics-traffic-monitor-automation",
    }

    # Every repository must ignore local secrets and build artefacts.
    required = {
        "gitlytics": [".env", ".env.*", "!.env.example", ".coverage", ".pytest_cache/", "*.lock", "*.tmp", "traffic_*.csv"],
        "gitlytics-backend": [".env", ".env.*", "!.env.example", ".coverage", ".pytest_cache/"],
        "gitlytics-frontend": [".env", ".env.*", "!.env.example"],
        "gitlytics-traffic-monitor-automation": [".env", ".env.*", "!.env.example", "*.lock"],
    }
    for name, patterns in required.items():
        repo = repos[name]
        if not repo.exists():
            continue
        ignore = repo / ".gitignore"
        assert ignore.is_file(), f"{name} has no .gitignore"
        text = ignore.read_text(encoding="utf-8")
        for pattern in patterns:
            assert pattern in text, f"{name}/.gitignore is missing {pattern}"

    # The metadata sites ship their own .gitignore (blog used to have none).
    for app in ("homepage", "docs", "blog"):
        ignore = repos["gitlytics-metadata-pages"] / app / ".gitignore"
        assert ignore.is_file(), f"{app} has no .gitignore"
        text = ignore.read_text(encoding="utf-8")
        assert ".env" in text, f"{app}/.gitignore does not ignore .env"

    # No repository may track a real .env file.
    for name, repo in repos.items():
        if not repo.exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            continue
        tracked = [line for line in result.stdout.splitlines() if line.strip()]
        for path in tracked:
            name_only = path.split("/")[-1]
            if name_only == ".env.example":
                continue  # the template is intentionally tracked
            assert not (
                name_only == ".env" or name_only.startswith(".env.")
            ), f"{name} tracks {path}"
            assert name_only != ".coverage", f"{name} tracks {path}"

    # The library must not leave a coverage file behind.
    assert not (ROOT / ".coverage").exists()


# ── AF-LIB-05: shipped static metadata assets are internally consistent ─────

def test_af_lib_05_static_metadata_assets_are_consistent():
    static = ROOT / "src" / "gitlytics" / "static"
    index_html = (static / "index.html").read_text(encoding="utf-8")
    headers = (static / "_headers").read_text(encoding="utf-8")

    # Every inline <script> body must be allowed by the shipped CSP. The hash
    # is computed over LF-normalised text, exactly like the header generator.
    normalized = index_html.replace("\r\n", "\n")
    inline_bodies = re.findall(
        r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", normalized, re.S
    )
    assert inline_bodies, "the dashboard entry point should still inline the theme bootstrap"
    for body in inline_bodies:
        if not body.strip():
            continue
        digest = base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        assert f"'sha256-{digest}'" in headers, "CSP does not allow an inline script"

    # The advertised OG image must actually be published.
    og = re.search(r'property="og:image" content="https://dashboard\.gitlytics\.dev/([^"]+)"', index_html)
    assert og, "index.html has no og:image"
    assert (static / og.group(1)).is_file(), f"og:image {og.group(1)} is missing"
    assert "gitlytics-banner.png" not in index_html, "og:image still points at a deleted banner"

    # The PWA manifest must not ship blank names.
    manifest = json.loads((static / "site.webmanifest").read_text(encoding="utf-8"))
    assert manifest["name"].strip(), "site.webmanifest has a blank name"
    assert manifest["short_name"].strip(), "site.webmanifest has a blank short_name"
    for icon in manifest["icons"]:
        assert (static / icon["src"].lstrip("/")).is_file(), f"missing icon {icon['src']}"

    # The advertised software version must match the library version.
    with (ROOT / "pyproject.toml").open("rb") as handle:
        expected_version = tomllib.load(handle)["project"]["version"]
    version = re.search(r'"softwareVersion":\s*"([^"]+)"', index_html)
    assert version, "index.html has no softwareVersion"
    assert version.group(1) == expected_version, "softwareVersion is stale"

    # The sitemap must describe exactly one host.
    sitemap = (static / "sitemap.xml").read_text(encoding="utf-8")
    locations = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    assert locations, "sitemap.xml is empty"
    hosts = {re.sub(r"^https?://([^/]+).*$", r"\1", loc) for loc in locations}
    assert hosts == {"dashboard.gitlytics.dev"}, f"sitemap mixes hosts: {hosts}"


# ── AF-XREPO-01: packaged static bundle must be compiled in local mode ──────

def test_af_xrepo_01_static_bundle_built_in_local_mode():
    assets = ROOT / "src" / "gitlytics" / "static" / "assets"
    entries = sorted(assets.glob("index-*.js"))
    assert entries, "missing static entry bundle"
    bundle = entries[0].read_text(encoding="utf-8", errors="replace")
    assert re.search(r"=\s*`local`", bundle), "static bundle is not in local mode"
    assert not re.search(r"(?<![=!<>])=\s*`cloud`", bundle), "static bundle was built in cloud mode"
    assert "http://localhost:8000" not in bundle
