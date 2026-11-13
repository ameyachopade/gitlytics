"""Regression tests for the metadata-site audit findings.

Covers META-06 (CLI/API docs drift), META-07 (single-source version),
META-08 (README topology), META-10 (CSP inline scripts), and META-12
(HTTP redirects).

Run from the repository root:
    .venv/Scripts/python.exe -m pytest gitlytics/tests/test_metadata_contract.py -q
"""
import base64
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
METADATA = REPO_ROOT / "gitlytics-metadata-pages"
DOCS_PAGE = METADATA / "docs" / "src" / "components" / "DocsPage.tsx"

APPS = ("homepage", "docs", "blog")
SRC_FILES = (
    METADATA / "homepage" / "src" / "components" / "HomePage.tsx",
    DOCS_PAGE,
    METADATA / "docs" / "src" / "components" / "MobileSiteMenu.tsx",
    METADATA / "blog" / "src" / "components" / "MobileSiteMenu.tsx",
)

_SKIP_MESSAGE = (
    "sibling gitlytics-metadata-pages repo not checked out - the cross-repo "
    "version-parity contract is NOT being enforced"
)


def _skip_if_sibling_missing() -> None:
    """Skip *loudly* when the sibling metadata repo is not checked out.

    AF2-LIB-13: the cross-repo contract must never be silently skipped. When
    the sibling repo is absent the whole module used to be skipped with a
    one-line reason, so the version-parity guarantee was never enforced
    anywhere and nobody noticed. The skip now emits an explicit
    RuntimeWarning that names the missing checkout and states that the
    contract is not being enforced.
    """
    if METADATA.exists():
        return
    warnings.warn(
        "CROSS-REPO CONTRACT CHECKS SKIPPED: the sibling "
        "'gitlytics-metadata-pages' repository is not checked out at "
        f"{METADATA}. The version-parity guarantee between the library "
        "and the metadata sites is NOT being enforced. Check out the "
        "sibling repository (see .github/workflows/tests.yml) or run "
        "these tests from a workspace where it exists.",
        RuntimeWarning,
        stacklevel=2,
    )
    pytest.skip(_SKIP_MESSAGE)


@pytest.fixture(autouse=True)
def _require_sibling_metadata_repo(request):
    if request.node.name in (
        "test_stars_cli_help_matches_documented_flags",
        "test_cross_repo_contract_skip_is_loud",
    ):
        return
    _skip_if_sibling_missing()


def _pyproject_version() -> str:
    with (REPO_ROOT / "gitlytics" / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


# ---------------------------------------------------------------------------
# AF2-LIB-13 - the cross-repo contract must not be silently skipped
# ---------------------------------------------------------------------------

def test_cross_repo_contract_skip_is_loud():
    """A missing sibling repo must produce a *visible* warning, not a silent skip.

    Previously the whole module was skipped with a one-line reason, so the
    "single source of version truth" guarantee between the library and the
    metadata sites was never enforced and nobody noticed. The skip is now
    announced with a RuntimeWarning that names the missing checkout and
    states that the contract is not being enforced.
    """
    if METADATA.exists():
        # The sibling repo is present — the real contract assertions in this
        # module run and enforce the version parity.
        assert METADATA.is_dir()
        return

    with pytest.warns(RuntimeWarning, match="NOT being enforced"):
        with pytest.raises(pytest.skip.Exception):
            _skip_if_sibling_missing()


# ---------------------------------------------------------------------------
# META-06 - docs match the real CLI and Python API
# ---------------------------------------------------------------------------

def test_star_history_signature_and_docs_agree():
    from gitlytics.core import fetch_star_history

    parameters = inspect.signature(fetch_star_history).parameters
    assert list(parameters) == ["owner", "repo", "token"]
    assert parameters["token"].default is None

    docs = DOCS_PAGE.read_text(encoding="utf-8")
    for stale in (
        "--days",
        "days: int",
        "days=180",
        "StargazersRestrictedError",
        "{ timestamp, count }",
        "JSON to stdout",
    ):
        assert stale not in docs, f"docs still document stale API surface: {stale}"

    assert "{ date, total }" in docs
    assert "two-point approximation" in docs


def test_stars_cli_help_matches_documented_flags():
    env = os.environ.copy()
    src = str(REPO_ROOT / "gitlytics" / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "gitlytics.cli", "stars", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "--token" in result.stdout
    assert "owner/repo" in result.stdout
    assert "--days" not in result.stdout


# ---------------------------------------------------------------------------
# META-07 - one version source for every metadata app
# ---------------------------------------------------------------------------

def test_metadata_version_matches_the_library_and_is_injected_at_build():
    expected = _pyproject_version()
    shared = json.loads((METADATA / "version.json").read_text(encoding="utf-8"))
    assert shared["gitlytics"] == expected

    for app in APPS:
        config = (METADATA / app / "vite.config.ts").read_text(encoding="utf-8")
        assert 'from "../version.json"' in config
        assert "__GITLYTICS_VERSION__" in config

    for source in SRC_FILES:
        text = source.read_text(encoding="utf-8")
        assert "0.6.3" not in text, f"stale hardcoded version in {source.name}"
        assert "__GITLYTICS_VERSION__" in text


# ---------------------------------------------------------------------------
# META-08 - README describes the real topology
# ---------------------------------------------------------------------------

def test_readmes_do_not_claim_embedded_dashboard_or_docs():
    readme = (METADATA / "README.md").read_text(encoding="utf-8")
    lowered = readme.lower()
    assert "three" in lowered
    assert "embeds a copy" not in lowered
    assert "in-app copy" not in lowered
    for app in APPS:
        assert f"`/{app}`" in readme or f"`{app}/`" in readme

    library_readme = (REPO_ROOT / "gitlytics" / "README.md").read_text(encoding="utf-8").lower()
    assert "in-app copy" not in library_readme
    assert "embeds a copy" not in library_readme


# ---------------------------------------------------------------------------
# META-10 - external scripts, no 'unsafe-inline', hashed JSON-LD
# ---------------------------------------------------------------------------

def test_metadata_csp_has_no_unsafe_inline_and_loads_external_bootstraps():
    for app in APPS:
        headers = (METADATA / app / "public" / "_headers").read_text(encoding="utf-8")
        csp = next(line for line in headers.splitlines() if "Content-Security-Policy:" in line)
        script_src = csp.split("script-src", 1)[1].split(";", 1)[0]
        assert "'unsafe-inline'" not in script_src, f"{app} CSP still allows inline scripts"
        assert "object-src 'none'" in csp

        for asset in ("theme-init.js", "analytics.js"):
            assert (METADATA / app / "public" / asset).is_file(), f"{app} missing {asset}"

        index = (METADATA / app / "index.html").read_text(encoding="utf-8")
        assert '<script src="/theme-init.js"></script>' in index
        assert '<script type="module" src="/analytics.js"></script>' in index
        assert "googletagmanager.com/gtag/js" not in index
        assert "clarity.ms/tag" not in index

        inline_tags = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", index)
        assert all(
            'type="application/ld+json"' in tag for tag in inline_tags
        ), f"{app} still has an executable inline script"


def test_homepage_json_ld_hash_matches_the_csp():
    """The homepage CSP must match the homepage's inline scripts exactly.

    The invariant is bidirectional and therefore stable regardless of how the
    homepage chooses to ship its JSON-LD:

      * every *executable* inline ``<script>`` must be hash-pinned in the
        CSP (``script-src`` with a ``sha256-`` source), and
      * every ``sha256-`` hash in the CSP must correspond to a script that
        actually exists — an orphaned hash is dead weight that only widens
        the policy.

    A ``<script type="application/ld+json">`` block is a *data* block: it is
    never executed, so ``script-src`` does not govern it and it needs no
    hash. The sibling repository moved its JSON-LD to exactly that form, so
    pinning the (now absent) digest would have asserted a stale contract.
    """
    index = (METADATA / "homepage" / "index.html").read_text(encoding="utf-8")
    headers = (METADATA / "homepage" / "public" / "_headers").read_text(encoding="utf-8")

    csp = next(
        line for line in headers.splitlines() if "Content-Security-Policy:" in line
    )
    script_src = csp.split("script-src", 1)[1].split(";", 1)[0]
    pinned = set(re.findall(r"'sha256-([^']+)'", script_src))

    inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index, re.S)
    executable = []
    for body, tag in zip(
        inline, re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", index)
    ):
        # A JSON-LD / importmap block is data, never executed.
        if 'type="application/ld+json"' in tag or 'type="application/json"' in tag:
            continue
        executable.append(body)

    for body in executable:
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert digest in pinned, (
            "the homepage ships an inline script that the CSP does not "
            f"hash-pin (sha256-{digest})"
        )

    # No orphaned hashes: every pinned digest must belong to a script that
    # actually ships.
    external = []
    for name in ("analytics.js", "theme-init.js"):
        candidate = METADATA / "homepage" / "public" / name
        if candidate.exists():
            external.append(
                base64.b64encode(
                    hashlib.sha256(candidate.read_bytes()).digest()
                ).decode()
            )

    known = {
        base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        for body in inline
    } | set(external)

    for digest in pinned:
        assert digest in known, (
            f"the homepage CSP pins sha256-{digest}, which matches no "
            "shipped script (orphaned hash)"
        )


# ---------------------------------------------------------------------------
# META-12 - host-level redirects precede the SPA fallback
# ---------------------------------------------------------------------------

def test_homepage_redirects_are_http_level_and_ordered_before_fallback():
    text = (METADATA / "homepage" / "public" / "_redirects").read_text(encoding="utf-8")
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "/dashboard https://dashboard.gitlytics.dev 301" in rules
    assert "/dashboard/* https://dashboard.gitlytics.dev/:splat 301" in rules
    assert "/docs https://docs.gitlytics.dev 301" in rules
    assert "/docs/* https://docs.gitlytics.dev/:splat 301" in rules
    assert rules[-1] == "/* /index.html 200"
    assert rules.index("/dashboard https://dashboard.gitlytics.dev 301") < len(rules) - 1
    assert rules.index("/docs https://docs.gitlytics.dev 301") < len(rules) - 1
