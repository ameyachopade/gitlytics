# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Architectural guardrail tests for the modular gitlytics package layout."""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def test_public_all_exports_exist_and_match_version():
    """Every symbol in gitlytics.__all__ resolves cleanly and version matches pyproject.toml."""
    import gitlytics

    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert gitlytics.__version__ == declared
    for symbol in gitlytics.__all__:
        assert hasattr(gitlytics, symbol), f"gitlytics.__all__ symbol {symbol!r} missing"


def test_backward_compat_imports_for_backend_and_cli():
    """Verify external import paths used by gitlytics-backend, uvicorn, and legacy callers."""
    import gitlytics
    import gitlytics.api
    import gitlytics.automation
    import gitlytics.cli
    import gitlytics.core
    import gitlytics.exceptions
    import gitlytics.io
    import gitlytics.process
    import gitlytics.username

    # Contracts consumed by gitlytics-backend/app/routes.py and app/auth/token.py.
    for attr in (
        "GitHubRateLimitError",
        "TokenExpiredError",
        "fetch_deep_stats_for_top",
        "fetch_traffic_data",
        "get_public_repos",
        "get_public_user",
        "get_single_repo",
        "validate_github_username",
        "validate_token",
    ):
        assert hasattr(gitlytics.core, attr), f"gitlytics.core missing {attr}"

    # Legacy process.py and username.py compatibility shims.
    assert gitlytics.process.build_react_payload is gitlytics.io.build_react_payload
    assert gitlytics.process.build_json_payload is gitlytics.io.build_json_payload
    assert gitlytics.process._to_bool is gitlytics.io._to_bool
    assert gitlytics.username.get_public_user is gitlytics.core.get_public_user
    assert gitlytics.username.get_public_repos is gitlytics.core.get_public_repos
    assert gitlytics.username.validate_github_username is gitlytics.core.validate_github_username

    # Entry points expected by pyproject.toml scripts and uvicorn.run("gitlytics.api:app").
    assert callable(gitlytics.cli.main)
    assert hasattr(gitlytics.api, "app")
    assert hasattr(gitlytics.automation, "run_sync")
    assert hasattr(gitlytics, "fetch_traffic")


def test_exception_identity_across_facades():
    """All exception classes share a single canonical class object across every facade."""
    import gitlytics
    from gitlytics import automation, core, exceptions

    for name in (
        "GitHubAPIError",
        "GitHubRateLimitError",
        "StarHistoryFetchError",
        "StargazersRestrictedError",
        "TokenExpiredError",
    ):
        canonical = getattr(exceptions, name)
        assert getattr(gitlytics, name) is canonical
        assert getattr(core, name) is canonical
        assert issubclass(canonical, exceptions.GitlyticsError)

    assert gitlytics.NoTrafficDataError is exceptions.NoTrafficDataError
    assert automation.NoTrafficDataError is exceptions.NoTrafficDataError
    assert automation.TokenExpiredError is exceptions.TokenExpiredError
    assert issubclass(exceptions.GitHubAPIError, RuntimeError)
    assert issubclass(exceptions.NoTrafficDataError, RuntimeError)


def test_core_imports_do_not_require_dashboard_extras():
    """Importing gitlytics and its non-dashboard subpackages must work without fastapi/uvicorn."""
    probe = (
        "import sys\n"
        "for mod in ('fastapi', 'uvicorn', 'starlette', 'multipart'):\n"
        "    sys.modules[mod] = None\n"
        "import gitlytics\n"
        "import gitlytics.core\n"
        "import gitlytics.io\n"
        "import gitlytics.process\n"
        "import gitlytics.automation\n"
        "import gitlytics.cli\n"
        "import gitlytics.username\n"
        "assert gitlytics.__version__\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr


def test_static_bundle_excludes_hosted_and_cloud_only_files():
    """Local static bundle ships only strictly required assets - no hosted docs or cloud chunks."""
    static_dir = SRC / "gitlytics" / "static"
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "_headers").is_file()
    assert (static_dir / "site.webmanifest").is_file()

    forbidden_top_level = (
        "_redirects",
        "robots.txt",
        "sitemap.xml",
        "llms.txt",
        "llms-full.txt",
        "404.html",
        "gitlytics-banner.png",
        "og-image.png",
        "og-cover.png",
        "favicon.png",
        "google-search-icon.png",
        "google-search-icon-48x48.png",
        "google1d385271124f275a.html",
        "322e4f16125148b7a5e8f8bb966a1d51.txt",
    )
    for name in forbidden_top_level:
        assert not (static_dir / name).exists(), f"hosted-only file leaked into static/: {name}"

    assets_dir = static_dir / "assets"
    assert assets_dir.is_dir()
    asset_names = [p.name for p in assets_dir.iterdir() if p.is_file()]
    assert any(n.startswith("index-") and n.endswith(".js") for n in asset_names)

    forbidden_chunks = (
        "AuthCallback-",
        "DeepAnalysisView-",
        "IntegrationsPage-",
        "WidgetPage-",
        "WorkspaceView-",
    )
    leaked = [n for n in asset_names if n.startswith(forbidden_chunks)]
    assert leaked == [], f"cloud-only lazy chunks leaked into static/assets/: {leaked}"


def test_every_source_module_declares_spdx_header():
    """Every Python module under src/gitlytics/ declares the matching SPDX header."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["license"]
    py_files = sorted((SRC / "gitlytics").rglob("*.py"))
    assert len(py_files) >= 18
    for py_file in py_files:
        text = py_file.read_text(encoding="utf-8")
        assert f"SPDX-License-Identifier: {declared}" in text, (
            f"Missing or mismatched SPDX header in {py_file.relative_to(ROOT)}"
        )
