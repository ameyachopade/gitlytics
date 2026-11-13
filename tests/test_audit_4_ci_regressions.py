"""
tests/test_audit_4_ci_regressions.py
Regression tests for the release-workflow and distribution findings from
Audit 4 (A4-CI-01, A4-CI-02, A4-CI-03, A4-CI-04).

Everything here is offline and inspects the checked-in workflow/documentation
surfaces only.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"
TESTS = ROOT / ".github" / "workflows" / "tests.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)


def _uses(path: Path) -> list[str]:
    return USES.findall(path.read_text(encoding="utf-8"))


def test_a4_ci_03_every_action_is_pinned_to_a_commit() -> None:
    for workflow in (PUBLISH, TESTS):
        for reference in _uses(workflow):
            if reference.startswith("./"):
                continue
            assert "@" in reference, reference
            _, revision = reference.rsplit("@", 1)
            assert SHA.fullmatch(revision), (
                f"{workflow.name}: {reference} must be pinned to a 40-character SHA"
            )


def test_a4_ci_03_publish_pins_the_frontend_checkout_and_enables_attestations() -> None:
    text = PUBLISH.read_text(encoding="utf-8")

    assert "attestations: true" in text
    assert re.search(r"^\s*id-token:\s*write", text, re.MULTILINE)


def test_a4_ci_04_publish_has_concurrency_and_tests_the_released_commit() -> None:
    text = PUBLISH.read_text(encoding="utf-8")

    assert re.search(r"^concurrency:", text, re.MULTILINE)
    assert "group: pypi-${{ github.event.release.tag_name }}" in text
    assert "cancel-in-progress: false" in text
    assert re.search(r"^  pypi-publish:", text, re.MULTILINE)
    assert "needs: test" in text

    test_job = text.split("  test:", 1)[1].split("  pypi-publish:", 1)[0]
    assert "ref: ${{ github.event.release.tag_name }}" in test_job
    assert "pytest" in test_job

    publish_checkout = text.split("  pypi-publish:", 1)[1]
    first_checkout = publish_checkout.split("steps:", 1)[1].split("- name:", 2)[1]
    assert "ref: ${{ github.event.release.tag_name }}" in first_checkout


def test_a4_ci_02_public_surfaces_use_the_live_slug_and_a_real_tag() -> None:
    canonical = "ameyachopade/gitlytics-github-traffic-automation"
    surfaces = [
        ROOT / "README.md",
        ROOT / "src" / "gitlytics" / "static" / "llms-full.txt",
    ]

    for surface in surfaces:
        text = surface.read_text(encoding="utf-8")
        assert "gitlytics-action" not in text, surface
        assert "gitlytics-traffic-monitor-automation" not in text, surface

    llms = surfaces[1].read_text(encoding="utf-8")
    # AF2-LIB-15: the shipped guide must reference the action by an
    # *immutable* revision. A 40-character commit SHA is preferred; the
    # released tag is accepted as well so the two spellings can coexist
    # while the static bundle is being updated. A bare mutable major
    # version (`@v1`) is never acceptable.
    assert not re.search(re.escape(canonical) + r"@v1(?!\.)", llms)
    refs = re.findall(re.escape(canonical) + r"@([^\s`'\")]+)", llms)
    assert refs, "llms-full.txt no longer references the traffic action"
    for ref in refs:
        assert SHA.fullmatch(ref) or re.fullmatch(r"v\d+\.\d+\.\d+.*", ref), (
            f"the traffic action must be pinned to a commit SHA or an exact "
            f"release tag, got {ref!r}"
        )


def test_ci01_release_is_built_from_the_declared_version() -> None:
    """The manifest/lock equality check lives in the backend suite; here we
    assert the release pipeline rebuilds (never reuses) the declared version.

    The version is compared dynamically against ``gitlytics.__version__`` and
    the version advertised by the shipped dashboard bundle — never a
    hard-coded literal, so a version bump does not break the release
    (AF2-LIB-12).
    """
    import re
    import tomllib

    import gitlytics

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = pyproject["project"]["version"]

    assert declared == gitlytics.__version__, (
        "pyproject.toml and gitlytics.__version__ disagree — the declared "
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
