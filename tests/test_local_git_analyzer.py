"""Tests for offline pure-Git local analyzer and security containment."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitlytics.core.local_git import analyze_local_repo


def _init_sample_repo(repo_dir: Path) -> None:
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Alice"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "alice@example.com"], check=True)
    (repo_dir / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], check=True, capture_output=True)
    (repo_dir / "app.py").write_text("print('v2')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "update"], check=True, capture_output=True)


def test_analyze_local_repo_computes_metrics_and_hotspots(tmp_path: Path):
    repo_dir = tmp_path / "demo-repo"
    repo_dir.mkdir()
    _init_sample_repo(repo_dir)

    report = analyze_local_repo(repo_dir, days=30)
    assert report["repo_name"] == "demo-repo"
    assert report["total_commits"] == 2
    assert report["active_authors"] == 1
    assert report["bus_factor"] == 1
    assert report["hotspots"][0]["file"] == "app.py"
    assert report["hotspots"][0]["touches"] == 2
    assert "app.py" in report["agent_prompt"]


def test_analyze_local_repo_blocks_flag_injection_and_traversal(tmp_path: Path):
    with pytest.raises(ValueError, match="must not start with '-'"):
        analyze_local_repo("--exec=calc")

    sub = tmp_path / "workspace"
    sub.mkdir()
    with pytest.raises(ValueError, match="outside the allowed"):
        analyze_local_repo("..", allowed_root=sub)
