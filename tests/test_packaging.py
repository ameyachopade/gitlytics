"""Packaging regression tests for DEP-01.

`requires-python` used to claim 3.9 while runtime dependencies (and the
dashboard extra) already required 3.11. These checks keep the declared floor,
README badge, homepage copy, and CI matrix in sync.
"""
import tomllib
import warnings
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"


def test_requires_python_matches_supported_runtime():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["requires-python"] == ">=3.11"


def test_classifiers_list_supported_python_versions():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    classifiers = set(data["project"]["classifiers"])
    for version in ("3.11", "3.12", "3.13"):
        assert f"Programming Language :: Python :: {version}" in classifiers


def test_readme_no_longer_claims_python_39():
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Python-3.11+" in readme
    assert "Python-3.9+" not in readme
    assert "requires-python = \">=3.9\"" not in readme


def test_ci_matrix_covers_every_supported_python_version():
    workflow = PACKAGE_ROOT / ".github" / "workflows" / "tests.yml"
    text = workflow.read_text(encoding="utf-8")
    for version in ("3.11", "3.12", "3.13"):
        assert f'"{version}"' in text
