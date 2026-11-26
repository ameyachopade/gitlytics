# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Pure-Git local repository intelligence analyzer (100% offline, zero external APIs).

Runs read-only `git log` / `git rev-parse` subprocesses without a shell to compute
commit velocity, contributor share, bus factor, high-churn file hotspots, and a
copyable coding-agent remediation prompt for any local Git checkout.
"""
from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_COMMIT_MARKER = "---GITLYTICS-COMMIT---"
_GIT_TIMEOUT_SECONDS = 20


def _run_git(repo_dir: Path, args: list[str]) -> str:
    """Execute a read-only git command safely without invoking a shell."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("git executable not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("git command timed out while inspecting repository.") from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ValueError(err or f"git {' '.join(args)} failed with exit code {proc.returncode}.")
    return proc.stdout


def _compute_bus_factor(author_counts: list[tuple[str, int]], total_commits: int) -> int:
    """Minimum number of top contributors accounting for >= 50% of commits."""
    if total_commits <= 0 or not author_counts:
        return 0
    threshold = total_commits * 0.5
    acc = 0
    for idx, (_, count) in enumerate(author_counts, start=1):
        acc += count
        if acc >= threshold:
            return idx
    return len(author_counts)


def _build_agent_prompt(
    repo_name: str,
    days: int,
    total_commits: int,
    bus_factor: int,
    active_authors: int,
    hotspots: list[dict[str, Any]],
) -> str:
    """Synthesize a concise, concrete prompt that a coding agent can execute immediately."""
    lines = [
        f"Audit local Git repository `{repo_name}` (last {days} days: {total_commits} commits, {active_authors} contributor(s), bus factor {bus_factor}).",
    ]
    if hotspots:
        top_files = ", ".join(f"`{h['file']}` ({h['touches']} touches)" for h in hotspots[:3])
        lines.append(
            f"1. Inspect the highest-churn hotspot files ({top_files}) for tangled responsibilities, missing tests, or regression risks, and propose focused refactors."
        )
    if bus_factor <= 1 and total_commits >= 5:
        lines.append(
            "2. Document key architecture entrypoints and add automated smoke tests around critical modules to reduce single-maintainer bus-factor risk."
        )
    else:
        lines.append(
            "2. Summarize recent engineering velocity and flag any modules showing repeated bug-fix churn."
        )
    return "\n".join(lines)


def analyze_local_repo(
    repo_path: str | Path = ".",
    *,
    days: int = 90,
    max_hotspots: int = 10,
    allowed_root: Path | None = None,
) -> dict[str, Any]:
    """Analyze a local Git checkout offline and return structured engineering metrics."""
    raw = str(repo_path or ".").strip()
    if not raw or "\x00" in raw:
        raise ValueError("Invalid repository path.")
    if raw.startswith("-"):
        raise ValueError("Repository path must not start with '-'.")

    safe_days = max(1, min(int(days), 3650))
    safe_max_hotspots = max(1, min(int(max_hotspots), 50))

    if allowed_root is not None:
        root_resolved = allowed_root.expanduser().resolve()
        candidate = (root_resolved / raw).resolve() if not Path(raw).is_absolute() else Path(raw).expanduser().resolve()
        if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
            raise ValueError("Requested path is outside the allowed local workspace directory.")
    else:
        candidate = Path(raw).expanduser().resolve()

    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Directory does not exist: {candidate}")

    # Confirm the directory is inside a valid Git working tree.
    toplevel_raw = _run_git(candidate, ["rev-parse", "--show-toplevel"]).strip()
    toplevel = Path(toplevel_raw).resolve()
    branch = _run_git(toplevel, ["rev-parse", "--abbrev-ref", "HEAD"]).strip() or "HEAD"

    log_output = _run_git(
        toplevel,
        [
            "log",
            f"--since={safe_days}.days.ago",
            "--no-merges",
            f"--format={_COMMIT_MARKER}%n%H%x09%aN%x09%aI",
            "--name-only",
        ],
    )

    author_counter: Counter[str] = Counter()
    file_counter: Counter[str] = Counter()
    week_counter: Counter[str] = Counter()
    total_commits = 0

    blocks = log_output.split(_COMMIT_MARKER)
    for block in blocks[1:]:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        header_parts = lines[0].split("\t")
        if len(header_parts) < 3:
            continue
        _, author_name, iso_date = header_parts[0], header_parts[1].strip() or "Unknown", header_parts[2].strip()
        total_commits += 1
        author_counter[author_name] += 1

        try:
            dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
            iso = dt.astimezone(timezone.utc).isocalendar()
            week_counter[f"{iso.year}-W{iso.week:02d}"] += 1
        except ValueError:
            pass

        for rel_file in lines[1:]:
            if rel_file:
                file_counter[rel_file] += 1

    ranked_authors = author_counter.most_common()
    active_authors = len(ranked_authors)
    bus_factor = _compute_bus_factor(ranked_authors, total_commits)
    commits_per_week = round((total_commits / safe_days) * 7.0, 2) if safe_days > 0 else 0.0

    top_authors = [
        {
            "author": name,
            "commits": count,
            "share_pct": round((count / total_commits) * 100.0, 1) if total_commits > 0 else 0.0,
        }
        for name, count in ranked_authors[:10]
    ]
    hotspots = [
        {"file": file_path, "touches": touches}
        for file_path, touches in file_counter.most_common(safe_max_hotspots)
    ]
    weekly_commits = [
        {"week": wk, "commits": cnt}
        for wk, cnt in sorted(week_counter.items())
    ]

    repo_name = toplevel.name
    return {
        "path": str(toplevel),
        "repo_name": repo_name,
        "branch": branch,
        "days": safe_days,
        "total_commits": total_commits,
        "active_authors": active_authors,
        "bus_factor": bus_factor,
        "commits_per_week": commits_per_week,
        "top_authors": top_authors,
        "hotspots": hotspots,
        "weekly_commits": weekly_commits,
        "agent_prompt": _build_agent_prompt(
            repo_name=repo_name,
            days=safe_days,
            total_commits=total_commits,
            bus_factor=bus_factor,
            active_authors=active_authors,
            hotspots=hotspots,
        ),
    }
