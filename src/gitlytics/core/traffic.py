# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""GitHub traffic API fetching, 15-day zero-padding, and tidy row/DataFrame building."""

from __future__ import annotations

import contextvars
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from gitlytics.exceptions import GitHubRateLimitError, TokenExpiredError

from . import client
from .repos import _validate_full_name, get_all_repos, get_single_repo

logger = logging.getLogger(__name__)


def get_repo_traffic(token: str, full_name: str, metrics: list = None) -> dict:
    """Fetch views, clones, referrers, paths, and release counts concurrently for one repo."""
    full_name = _validate_full_name(full_name)
    headers = client.make_headers(token)

    wants_views = metrics is None or "views" in metrics
    wants_clones = metrics is None or "clones" in metrics
    wants_refs = metrics is None or "referrers" in metrics
    wants_paths = metrics is None or "paths" in metrics
    wants_releases = metrics is None or "releases" in metrics

    views: Any = {}
    clones: Any = {}
    refs: Any = []
    paths: Any = []
    views_ok = True
    clones_ok = True
    releases_count = 0
    v_status: int | None = None
    c_status: int | None = None

    def _fetch_releases() -> int:
        try:
            return client._count_via_link_header(
                f"{client.BASE}/repos/{full_name}/releases", headers
            )
        except Exception as exc:
            logger.warning("Could not fetch releases count for %s: %s", full_name, exc)
            return 0

    tasks: list[tuple[str, Any]] = []
    if wants_views:
        tasks.append(
            (
                "views",
                lambda: client._safe_get(
                    f"{client.BASE}/repos/{full_name}/traffic/views", headers
                ),
            )
        )
    if wants_clones:
        tasks.append(
            (
                "clones",
                lambda: client._safe_get(
                    f"{client.BASE}/repos/{full_name}/traffic/clones", headers
                ),
            )
        )
    if wants_refs:
        tasks.append(
            (
                "referrers",
                lambda: client._safe_get(
                    f"{client.BASE}/repos/{full_name}/traffic/popular/referrers",
                    headers,
                ),
            )
        )
    if wants_paths:
        tasks.append(
            (
                "paths",
                lambda: client._safe_get(
                    f"{client.BASE}/repos/{full_name}/traffic/popular/paths", headers
                ),
            )
        )
    if wants_releases:
        tasks.append(("releases", _fetch_releases))

    results: dict[str, Any] = {}
    with client._operation_deadline():
        if len(tasks) == 1:
            name, fn = tasks[0]
            results[name] = fn()
        elif len(tasks) > 1:
            with ThreadPoolExecutor(max_workers=min(5, len(tasks))) as pool:
                future_map = {
                    pool.submit(contextvars.copy_context().run, fn): name
                    for name, fn in tasks
                }
                for fut in as_completed(future_map):
                    name = future_map[fut]
                    try:
                        results[name] = fut.result()
                    except Exception as exc:
                        logger.warning(
                            "Traffic endpoint '%s' failed for %s: %s",
                            name,
                            full_name,
                            exc,
                        )

    if wants_views:
        views, v_status = results.get("views", ({}, -1))
        views_ok = v_status == 200 and isinstance(views, dict) and "views" in views
    if wants_clones:
        clones, c_status = results.get("clones", ({}, -1))
        clones_ok = c_status == 200 and isinstance(clones, dict) and "clones" in clones
    if wants_refs:
        refs, _ = results.get("referrers", ([], -1))
    if wants_paths:
        paths, _ = results.get("paths", ([], -1))
    if wants_releases:
        releases_count = int(results.get("releases", 0) or 0)

    traffic_statuses: list[int] = []
    if v_status is not None:
        traffic_statuses.append(v_status)
    if c_status is not None:
        traffic_statuses.append(c_status)

    # Mark the fetch usable only when at least one requested traffic endpoint succeeded.
    fetch_ok = (
        (not wants_views and not wants_clones)
        or (wants_views and views_ok)
        or (wants_clones and clones_ok)
    )

    return {
        "views": views if isinstance(views, dict) else {},
        "clones": clones if isinstance(clones, dict) else {},
        "referrers": refs if isinstance(refs, list) else [],
        "paths": paths if isinstance(paths, list) else [],
        "releases": releases_count,
        "_fetch_ok": fetch_ok,
        "_views_ok": views_ok,
        "_clones_ok": clones_ok,
        "_traffic_statuses": traffic_statuses,
    }


def pad_traffic_data(traffic: dict) -> list[dict]:
    """Pad GitHub's sparse traffic response to a continuous 15-day window ending today."""
    views_obj = traffic.get("views") if isinstance(traffic, dict) else {}
    clones_obj = traffic.get("clones") if isinstance(traffic, dict) else {}
    views_list = [
        item
        for item in (
            (views_obj.get("views") if isinstance(views_obj, dict) else None) or []
        )
        if isinstance(item, dict)
    ]
    clones_list = [
        item
        for item in (
            (clones_obj.get("clones") if isinstance(clones_obj, dict) else None) or []
        )
        if isinstance(item, dict)
    ]

    latest_date_str = None
    for item in views_list + clones_list:
        ts = item.get("timestamp", "")
        date_str = ts[:10] if isinstance(ts, str) else ""
        if date_str and (latest_date_str is None or date_str > latest_date_str):
            latest_date_str = date_str

    now_utc = datetime.now(timezone.utc)
    if latest_date_str:
        try:
            parsed_date = datetime.strptime(latest_date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            end_date = (
                now_utc
                if 0 <= (now_utc.date() - parsed_date.date()).days <= 14
                else parsed_date
            )
        except ValueError:
            logger.warning(
                f"Could not parse traffic date {latest_date_str!r}; falling back to today."
            )
            end_date = now_utc
    else:
        end_date = now_utc

    dates = [
        (end_date - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14, -1, -1)
    ]
    views_map = {
        v.get("timestamp", "")[:10]: v
        for v in views_list
        if isinstance(v, dict) and v.get("timestamp")
    }
    clones_map = {
        c.get("timestamp", "")[:10]: c
        for c in clones_list
        if isinstance(c, dict) and c.get("timestamp")
    }

    padded = []
    for d in dates:
        v = views_map.get(d, {}) or {}
        c = clones_map.get(d, {}) or {}
        padded.append(
            {
                "date": d,
                "views": int(v.get("count", 0) or 0),
                "unique_visitors": int(v.get("uniques", 0) or 0),
                "clones": int(c.get("count", 0) or 0),
                "unique_cloners": int(c.get("uniques", 0) or 0),
            }
        )
    return padded


def build_tidy_rows(repo: dict, traffic: dict, metrics: list = None) -> list[dict]:
    """Expand one repository's traffic snapshot into daily tidy-data row dicts."""
    if isinstance(traffic, dict) and traffic.get("_fetch_ok") is False:
        logger.warning(
            "Skipping tidy row generation for %s because traffic endpoints failed.",
            repo.get("full_name", "unknown"),
        )
        return []

    views_ok = traffic.get("_views_ok", True) if isinstance(traffic, dict) else True
    clones_ok = traffic.get("_clones_ok", True) if isinstance(traffic, dict) else True
    padded = pad_traffic_data(traffic)
    refs = traffic.get("referrers", [])
    paths = traffic.get("paths", [])

    top_ref = refs[0].get("referrer", "") if refs else ""
    top_ref_views = refs[0].get("count", 0) if refs else 0
    top_ref_uniques = refs[0].get("uniques", 0) if refs else 0
    top_path = paths[0].get("path", "") if paths else ""
    top_path_views = paths[0].get("count", 0) if paths else 0
    top_path_uniques = paths[0].get("uniques", 0) if paths else 0

    # Hoist metric checks, repo metadata, and JSON serialization out of the 15-day loop.
    full_name = repo["full_name"]
    is_private = repo.get("private", False)
    want_views = (metrics is None or "views" in metrics) and views_ok
    want_clones = (metrics is None or "clones" in metrics) and clones_ok
    want_stars = metrics is None or "stars" in metrics
    want_forks = metrics is None or "forks" in metrics
    want_releases = metrics is None or "releases" in metrics
    want_language = metrics is None or "language" in metrics
    want_topics = metrics is None or "topics" in metrics
    want_watchers = metrics is None or "watchers_count" in metrics
    want_pushed = metrics is None or "pushed_at" in metrics
    want_created = metrics is None or "created_at" in metrics
    want_issues = metrics is None or "open_issues_count" in metrics
    want_refs = metrics is None or "referrers" in metrics
    want_paths = metrics is None or "paths" in metrics

    stars = repo.get("stargazers_count", 0) if want_stars else 0
    forks = repo.get("forks_count", 0) if want_forks else 0
    releases = traffic.get("releases", 0) if want_releases else 0
    language = repo.get("language") if want_language else None
    topics_json = json.dumps(repo.get("topics", [])) if want_topics else None
    watchers = repo.get("watchers_count", 0) if want_watchers else 0
    pushed_at = repo.get("pushed_at", "") if want_pushed else ""
    created_at = repo.get("created_at", "") if want_created else ""
    open_issues = repo.get("open_issues_count", 0) if want_issues else 0
    refs_json = json.dumps(refs) if want_refs else None
    paths_json = json.dumps(paths) if want_paths else None

    rows = []
    for day in padded:
        row: dict[str, Any] = {
            "date": day["date"],
            "repository": full_name,
            "is_private": is_private,
        }
        if want_views:
            row["views"] = day["views"]
            row["unique_visitors"] = day["unique_visitors"]
        if want_clones:
            row["clones"] = day["clones"]
            row["unique_cloners"] = day["unique_cloners"]
        if want_stars:
            row["stars"] = stars
        if want_forks:
            row["forks"] = forks
        if want_releases:
            row["releases"] = releases
        if want_language:
            row["language"] = language
        if want_topics:
            row["topics"] = topics_json
        if want_watchers:
            row["watchers_count"] = watchers
        if want_pushed:
            row["pushed_at"] = pushed_at
        if want_created:
            row["created_at"] = created_at
        if want_issues:
            row["open_issues_count"] = open_issues

        if want_refs:
            row["top_referrer"] = top_ref
            row["top_referrer_views"] = top_ref_views
            row["top_referrer_uniques"] = top_ref_uniques
            row["_raw_referrers"] = refs_json

        if want_paths:
            row["top_path"] = top_path
            row["top_path_views"] = top_path_views
            row["top_path_uniques"] = top_path_uniques
            row["_raw_paths"] = paths_json

        rows.append(row)
    return rows


def fetch_traffic_rows(
    token: str,
    repo_names=None,
    metrics: list | None = None,
    public_only: bool = False,
) -> list[dict]:
    """Fetch traffic across one, selected, or all repos into plain tidy row dicts (zero pandas)."""
    # Share one operation deadline across repo discovery and all concurrent workers.
    with client._operation_deadline():
        if repo_names is not None:
            if isinstance(repo_names, str):
                repo_names = [repo_names]
            else:
                repo_names = list(repo_names)
            if len(repo_names) == 0:
                return []
            if len(repo_names) == 1:
                repos = []
                try:
                    repos.append(get_single_repo(token, repo_names[0]))
                except ValueError as exc:
                    logger.warning("Skipping unknown repo '%s': %s", repo_names[0], exc)
            else:
                def _lookup_one(idx_name: tuple[int, str]):
                    idx, name = idx_name
                    try:
                        return idx, get_single_repo(token, name)
                    except ValueError as exc:
                        logger.warning("Skipping unknown repo '%s': %s", name, exc)
                        return idx, None

                ordered_repos: list[tuple[int, dict | None]] = []
                with ThreadPoolExecutor(max_workers=min(5, len(repo_names))) as pool:
                    futures = [
                        pool.submit(
                            contextvars.copy_context().run, _lookup_one, (i, n)
                        )
                        for i, n in enumerate(repo_names)
                    ]
                    for fut in as_completed(futures):
                        ordered_repos.append(fut.result())
                repos = [
                    r
                    for _, r in sorted(ordered_repos, key=lambda x: x[0])
                    if r is not None
                ]
        else:
            repos = get_all_repos(token)
            # Drop private repos before fetching traffic when public_only is enabled.
            if public_only:
                repos = [r for r in repos if not r.get("private", False)]

        if not repos:
            return []

        all_rows: list[dict] = []
        traffic_results: list[dict] = []

        if len(repos) <= 1:
            for repo in repos:
                traffic = get_repo_traffic(token, repo["full_name"], metrics)
                if isinstance(traffic, dict):
                    traffic_results.append(traffic)
                all_rows.extend(build_tidy_rows(repo, traffic, metrics))
        else:
            def _fetch_one(idx_repo: tuple[int, dict]):
                idx, repo = idx_repo
                traffic = get_repo_traffic(token, repo["full_name"], metrics)
                return idx, traffic, build_tidy_rows(repo, traffic, metrics)

            ordered_results = []
            # Cap outer pool to 4 workers so nested per-repo 5-endpoint fan-outs stay bounded.
            with ThreadPoolExecutor(max_workers=min(4, len(repos))) as pool:
                futures = {
                    pool.submit(contextvars.copy_context().run, _fetch_one, (i, r)): i
                    for i, r in enumerate(repos)
                }
                for fut in as_completed(futures):
                    try:
                        ordered_results.append(fut.result())
                    except Exception as exc:
                        logger.warning("Repo traffic worker failed: %s", exc)
            for _, traffic, rows in sorted(ordered_results, key=lambda x: x[0]):
                if isinstance(traffic, dict):
                    traffic_results.append(traffic)
                all_rows.extend(rows)

    # Surface auth or rate-limit errors if every single repository failed.
    if (
        not all_rows
        and traffic_results
        and all(t.get("_fetch_ok") is False for t in traffic_results)
    ):
        statuses = [
            s for t in traffic_results for s in (t.get("_traffic_statuses") or [])
        ]
        if 429 in statuses:
            raise GitHubRateLimitError(
                "GitHub API rate limit exceeded while fetching traffic data (HTTP 429)."
            )
        if any(s in (401, 403) for s in statuses):
            raise TokenExpiredError(
                "GitHub token is invalid or lacks repository traffic permissions (HTTP 401/403)."
            )

    return all_rows


def fetch_traffic_data(
    token: str,
    repo_names=None,
    metrics: list = None,
    public_only: bool = False,
):
    """Fetch traffic across one, selected, or all repositories into a tidy DataFrame."""
    rows = fetch_traffic_rows(
        token,
        repo_names=repo_names,
        metrics=metrics,
        public_only=public_only,
    )
    import pandas as pd

    return pd.DataFrame(rows) if rows else pd.DataFrame()
