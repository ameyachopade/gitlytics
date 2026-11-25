"""Offline tests for release workflows, action SHA pinning, and version consistency."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
PUBLISH = WORKFLOWS_DIR / "publish.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)


def _uses(path: Path) -> list[str]:
    return USES.findall(path.read_text(encoding="utf-8"))


def test_every_workflow_action_is_pinned_to_a_commit() -> None:
    workflows = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert workflows, "expected at least one workflow file"
    for workflow in workflows:
        for reference in _uses(workflow):
            if reference.startswith("./"):
                continue
            assert "@" in reference, reference
            _, revision = reference.rsplit("@", 1)
            assert SHA.fullmatch(revision), (
                f"{workflow.name}: {reference} must be pinned to a 40-character SHA"
            )


def test_publish_workflow_enables_attestations() -> None:
    text = PUBLISH.read_text(encoding="utf-8")

    assert "attestations: true" in text
    assert re.search(r"^\s*id-token:\s*write", text, re.MULTILINE)


def test_publish_workflow_has_concurrency_and_tests_the_released_commit() -> None:
    text = PUBLISH.read_text(encoding="utf-8")

    assert re.search(r"^concurrency:", text, re.MULTILINE)
    assert "group: pypi-${{ github.event.release.tag_name }}" in text
    assert "cancel-in-progress: false" in text
    assert re.search(r"^  pypi-publish:", text, re.MULTILINE)
    assert "needs: test" in text

    test_job = text.split("  test:", 1)[1].split("  pypi-publish:", 1)[0]
    assert "ref: ${{ github.event.release.tag_name }}" in test_job
    assert "pytest" in test_job

    publish_job = text.split("  pypi-publish:", 1)[1]
    assert "actions/download-artifact@" in publish_job
    assert "actions/checkout@" not in publish_job


def test_public_surfaces_use_the_live_slug() -> None:
    # `llms-full.txt` is a hosted-site document; the local library bundle
    # intentionally drops it, so only the README is checked here.
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    assert "gitlytics-action" not in text, readme
    assert "gitlytics-traffic-monitor-automation" not in text, readme


def test_ci01_release_is_built_from_the_declared_version() -> None:
    """Verify the release workflow rebuilds the declared version and matches static/index.html."""
    import re
    import tomllib

    import gitlytics

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]

    assert declared == gitlytics.__version__, (
        "pyproject.toml and gitlytics.__version__ disagree - the declared "
        "version is the single source of truth"
    )

    index_html = (ROOT / "src" / "gitlytics" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    match = re.search(r'"softwareVersion":\s*"([^"]+)"', index_html)
    assert match, "static/index.html has no softwareVersion"
    assert match.group(1) == declared, (
        "the shipped dashboard advertises a stale version"
    )

    text = PUBLISH.read_text(encoding="utf-8")
    assert "rm -rf build dist" in text
    assert "python -m build" in text
    assert "twine upload" not in text
