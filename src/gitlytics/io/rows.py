# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Pure-Python (zero-pandas) CSV reader, row normalizer, and payload builders."""

from __future__ import annotations

import csv
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maps legacy display headers to canonical snake_case column names.
_LEGACY_COLUMN_MAP: dict[str, str] = {
    "repository": "repository",
    "repo_name": "repository",
    "total views": "views",
    "unique visitors": "unique_visitors",
    "total clones": "clones",
    "unique cloners": "unique_cloners",
    "stars": "stars",
    "forks": "forks",
}

# Columns that represent numeric metrics across tidy rows.
_NUMERIC_COLUMNS: tuple[str, ...] = (
    "views",
    "unique_visitors",
    "clones",
    "unique_cloners",
    "stars",
    "forks",
    "watchers_count",
    "open_issues_count",
)

_DEEP_KEYS: tuple[str, ...] = (
    "total_commits",
    "open_prs",
    "total_releases",
    "last_release_at",
    "has_readme",
    "has_license",
    "has_contributing",
    "has_code_of_conduct",
)

_NULLISH_STRINGS = frozenset(("", "nan", "none", "null", "<na>", "nat"))
_TRUE_STRINGS = frozenset(("true", "1", "yes", "y", "t"))


def normalize_field(name: str) -> str:
    """Strip BOMs, trim whitespace, and lower-case a single CSV column header."""
    if not isinstance(name, str):
        return ""
    return name.lstrip("\ufeff").strip().lower()


def merge_schema(existing_fields: list[str], new_fields: list[str]) -> list[str]:
    """Keep existing CSV column order while appending newly introduced columns."""
    norm_existing = {normalize_field(f) for f in existing_fields}
    merged_names = list(existing_fields)
    for col in new_fields:
        norm = normalize_field(col)
        if norm and norm not in norm_existing:
            logger.info("Schema upgrade: adding new column '%s' to existing CSV.", col)
            norm_existing.add(norm)
            merged_names.append(col)
    return merged_names


def is_nullish(val: Any) -> bool:
    """Return True for None, NaN, pandas NA/NaT, empty strings, and literal nulls."""
    if val is None:
        return True
    if isinstance(val, str):
        return val.strip().lower() in _NULLISH_STRINGS
    if isinstance(val, (bool, int)):
        return False
    if isinstance(val, float):
        return math.isnan(val)
    if isinstance(val, (list, tuple, dict, set)):
        return False
    # Handle numpy scalars, pandas NA/NaT, and foreign float-like objects without importing them.
    try:
        if val != val:
            return True
    except (TypeError, ValueError):
        pass
    return str(val).strip().lower() in _NULLISH_STRINGS


def to_bool(val: Any) -> bool:
    """Parse booleans strictly so strings like 'False' never become True."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    if isinstance(val, int):
        return bool(val)
    if isinstance(val, float):
        if math.isnan(val):
            return False
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in _TRUE_STRINGS
    # Handle numpy.bool_, numpy.integer, numpy.floating without importing numpy.
    type_mod = type(val).__module__
    if type_mod == "numpy" or type_mod.startswith("numpy."):
        try:
            return to_bool(val.item())
        except Exception:
            return False
    return False


def parse_raw(val: Any) -> list:
    """Decode a JSON-encoded list column safely, returning an empty list on bad input."""
    if isinstance(val, list):
        return val
    if is_nullish(val):
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        logger.warning(f"_parse_raw: could not decode value {val!r:.80}; returning []")
        return []


def safe_int(val: Any, fallback: int = 0) -> int:
    """Convert a scalar (including comma-formatted numbers) to int with a fallback."""
    if val is None or isinstance(val, bool):
        return fallback
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return fallback
        return int(val)
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned or cleaned.lower() in _NULLISH_STRINGS:
            return fallback
        try:
            num = float(cleaned.replace(",", ""))
            if math.isnan(num) or math.isinf(num):
                return fallback
            return int(num)
        except (TypeError, ValueError, OverflowError):
            return fallback
    if is_nullish(val):
        return fallback
    try:
        num = float(str(val).replace(",", "").strip())
        if math.isnan(num) or math.isinf(num):
            return fallback
        return int(num)
    except (TypeError, ValueError, OverflowError):
        return fallback


def safe_str(val: Any, fallback: str = "") -> str:
    """Convert a value to str while replacing nullish entries with the fallback."""
    if is_nullish(val):
        return fallback
    return str(val)


def read_csv_rows(path: str | Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a CSV file into normalized row dicts with BOM handling and alias coalescing."""
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return [], []

        canonical_order: list[str] = []
        indices_by_col: dict[str, list[int]] = {}
        for idx, raw_col in enumerate(raw_headers):
            norm = normalize_field(raw_col)
            canon = _LEGACY_COLUMN_MAP.get(norm, norm)
            if canon not in indices_by_col:
                canonical_order.append(canon)
                indices_by_col[canon] = [idx]
            else:
                indices_by_col[canon].append(idx)

        has_duplicates = any(len(idxs) > 1 for idxs in indices_by_col.values())
        rows: list[dict[str, Any]] = []

        if not has_duplicates:
            single_map = [(col, indices_by_col[col][0]) for col in canonical_order]
            for raw_row in reader:
                if not raw_row:
                    continue
                row_len = len(raw_row)
                rows.append(
                    {
                        col: raw_row[idx] if idx < row_len else ""
                        for col, idx in single_map
                    }
                )
        else:
            multi_map = [
                (col, tuple(indices_by_col[col])) for col in canonical_order
            ]
            for raw_row in reader:
                if not raw_row:
                    continue
                row_len = len(raw_row)
                record: dict[str, Any] = {}
                for col, idxs in multi_map:
                    if len(idxs) == 1:
                        idx = idxs[0]
                        record[col] = raw_row[idx] if idx < row_len else ""
                    else:
                        chosen = ""
                        for idx in idxs:
                            if idx < row_len:
                                candidate = raw_row[idx]
                                if not is_nullish(candidate):
                                    chosen = candidate
                                    break
                        record[col] = chosen
                rows.append(record)

    return canonical_order, rows


def filter_public_only_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude every row belonging to any repository marked private in any row."""
    if not rows:
        return []

    private_repos: set[str] = set()
    has_private = False
    for row in rows:
        if to_bool(row.get("is_private")):
            has_private = True
            repo = row.get("repository")
            if not is_nullish(repo):
                private_repos.add(str(repo))

    if not has_private:
        return list(rows)

    filtered: list[dict[str, Any]] = []
    for row in rows:
        if to_bool(row.get("is_private")):
            continue
        repo = row.get("repository")
        if private_repos and not is_nullish(repo) and str(repo) in private_repos:
            continue
        filtered.append(row)
    return filtered


def _group_rows_by_repo(
    rows: list[dict[str, Any]],
) -> tuple[bool, dict[str, list[dict[str, Any]]]]:
    """Group tidy rows by repository name in a single O(N) pass."""
    has_repo_key = False
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if "repository" in row:
            has_repo_key = True
            repo = row["repository"]
            if is_nullish(repo):
                continue
            repo_name = str(repo)
            bucket = grouped.get(repo_name)
            if bucket is None:
                grouped[repo_name] = [row]
            else:
                bucket.append(row)
    return has_repo_key, grouped


def build_json_payload_rows(
    rows: list[dict[str, Any]],
    return_format: str = "timeseries",
    export_public_only: bool = True,
) -> dict[str, Any]:
    """Transform tidy traffic row dicts into the nested JSON summary or timeseries dict."""
    if not rows:
        return {"account_totals": {}, "repositories": {}}

    working_rows = filter_public_only_rows(rows) if export_public_only else rows
    if not working_rows:
        return {"account_totals": {}, "repositories": {}}

    has_repo_key, grouped = _group_rows_by_repo(working_rows)
    if not has_repo_key:
        return {"account_totals": {}, "repositories": {}}

    account_views = 0
    account_clones = 0
    account_uniques = 0
    account_unique_cloners = 0
    account_stars = 0
    account_forks = 0
    repos_dict: dict[str, Any] = {}
    is_summary = return_format == "summary"

    for repo in sorted(grouped.keys()):
        repo_rows = grouped[repo]
        if len(repo_rows) > 1:
            repo_rows = sorted(repo_rows, key=lambda r: safe_str(r.get("date")))

        r_views = 0
        r_clones = 0
        r_unique_v = 0
        r_unique_c = 0
        r_stars = 0
        r_forks = 0
        r_is_private = False
        top_ref = ""
        top_path = ""
        timeseries: list[dict[str, Any]] | None = None if is_summary else []

        for row in repo_rows:
            v = safe_int(row.get("views"))
            uv = safe_int(row.get("unique_visitors"))
            c = safe_int(row.get("clones"))
            uc = safe_int(row.get("unique_cloners"))

            r_views += v
            r_unique_v += uv
            r_clones += c
            r_unique_c += uc

            stars_val = row.get("stars")
            if not is_nullish(stars_val):
                r_stars = safe_int(stars_val)
            forks_val = row.get("forks")
            if not is_nullish(forks_val):
                r_forks = safe_int(forks_val)
            priv_val = row.get("is_private")
            if not is_nullish(priv_val):
                r_is_private = to_bool(priv_val)
            ref_val = row.get("top_referrer")
            if not is_nullish(ref_val):
                top_ref = str(ref_val)
            path_val = row.get("top_path")
            if not is_nullish(path_val):
                top_path = str(path_val)

            if timeseries is not None:
                timeseries.append(
                    {
                        "date": safe_str(row.get("date")),
                        "views": v,
                        "unique_visitors": uv,
                        "clones": c,
                        "unique_cloners": uc,
                    }
                )

        account_views += r_views
        account_clones += r_clones
        account_uniques += r_unique_v
        account_unique_cloners += r_unique_c
        account_stars += r_stars
        account_forks += r_forks

        if is_summary:
            repos_dict[repo] = {
                "is_private": r_is_private,
                "total_views": r_views,
                "total_clones": r_clones,
                "unique_visitors": r_unique_v,
                "unique_cloners": r_unique_c,
                "stars": r_stars,
                "forks": r_forks,
            }
        else:
            repos_dict[repo] = {
                "timeseries": timeseries,
                "totals": {
                    "is_private": r_is_private,
                    "stars": r_stars,
                    "forks": r_forks,
                    "top_referrer": top_ref,
                    "top_path": top_path,
                },
            }

    return {
        "account_totals": {
            "total_views": account_views,
            "total_clones": account_clones,
            "unique_visitors": account_uniques,
            "unique_cloners": account_unique_cloners,
            "total_stars": account_stars,
            "total_forks": account_forks,
        },
        "repositories": repos_dict,
    }


def build_react_payload_rows(
    rows: list[dict[str, Any]],
    deep_stats: dict | None = None,
) -> list[dict[str, Any]]:
    """Transform tidy row dicts into the RepoTraffic list expected by the React dashboard."""
    if not rows:
        return []

    has_repo_key, grouped = _group_rows_by_repo(rows)
    if not has_repo_key:
        logger.warning("Cannot build payload: the frame has no 'repository' column.")
        return []

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos: list[dict[str, Any]] = []

    for repo in sorted(grouped.keys()):
        repo_rows = grouped[repo]
        if len(repo_rows) > 1:
            repo_rows = sorted(repo_rows, key=lambda r: safe_str(r.get("date")))

        r_views = 0
        r_clones = 0
        r_unique_v = 0
        r_unique_c = 0
        r_stars = 0
        r_forks = 0
        r_is_private = False

        top_ref = ""
        top_ref_views = 0
        top_ref_uniques = 0
        top_path = ""
        top_path_views = 0
        top_path_uniques = 0

        language: str | None = None
        watchers_count = 0
        open_issues_count = 0
        pushed_at: str | None = None
        created_at: str | None = None
        topics_raw: Any = None
        raw_refs_val: Any = None
        raw_paths_val: Any = None

        daily_views: list[dict[str, Any]] = []
        daily_clones: list[dict[str, Any]] = []

        for row in repo_rows:
            date_str = safe_str(row.get("date"))
            v = safe_int(row.get("views"))
            uv = safe_int(row.get("unique_visitors"))
            c = safe_int(row.get("clones"))
            uc = safe_int(row.get("unique_cloners"))

            r_views += v
            r_unique_v += uv
            r_clones += c
            r_unique_c += uc

            daily_views.append({"timestamp": date_str, "count": v, "uniques": uv})
            daily_clones.append({"timestamp": date_str, "count": c, "uniques": uc})

            val = row.get("stars")
            if not is_nullish(val):
                r_stars = safe_int(val)
            val = row.get("forks")
            if not is_nullish(val):
                r_forks = safe_int(val)
            val = row.get("is_private")
            if not is_nullish(val):
                r_is_private = to_bool(val)

            val = row.get("top_referrer")
            if not is_nullish(val):
                top_ref = str(val)
            val = row.get("top_referrer_views")
            if not is_nullish(val):
                top_ref_views = safe_int(val)
            val = row.get("top_referrer_uniques")
            if not is_nullish(val):
                top_ref_uniques = safe_int(val)

            val = row.get("top_path")
            if not is_nullish(val):
                top_path = str(val)
            val = row.get("top_path_views")
            if not is_nullish(val):
                top_path_views = safe_int(val)
            val = row.get("top_path_uniques")
            if not is_nullish(val):
                top_path_uniques = safe_int(val)

            val = row.get("language")
            if not is_nullish(val):
                language = str(val)
            val = row.get("watchers_count")
            if not is_nullish(val):
                watchers_count = safe_int(val)
            val = row.get("open_issues_count")
            if not is_nullish(val):
                open_issues_count = safe_int(val)
            val = row.get("pushed_at")
            if not is_nullish(val):
                pushed_at = str(val)
            val = row.get("created_at")
            if not is_nullish(val):
                created_at = str(val)

            val = row.get("topics")
            if not is_nullish(val):
                topics_raw = val
            val = row.get("_raw_referrers")
            if not is_nullish(val):
                raw_refs_val = val
            val = row.get("_raw_paths")
            if not is_nullish(val):
                raw_paths_val = val

        topics = parse_raw(topics_raw) if topics_raw is not None else []
        full_refs = parse_raw(raw_refs_val)
        full_paths = parse_raw(raw_paths_val)

        if not full_refs and top_ref:
            full_refs = [
                {
                    "referrer": top_ref,
                    "count": top_ref_views,
                    "uniques": top_ref_uniques,
                }
            ]
        if not full_paths and top_path:
            full_paths = [
                {
                    "path": top_path,
                    "title": top_path,
                    "count": top_path_views,
                    "uniques": top_path_uniques,
                }
            ]

        repo_obj: dict[str, Any] = {
            "repository": repo,
            "is_private": r_is_private,
            "stars": r_stars,
            "forks": r_forks,
            "views": r_views,
            "unique_visitors": r_unique_v,
            "clones": r_clones,
            "unique_cloners": r_unique_c,
            "top_referrer": top_ref,
            "top_referrer_views": top_ref_views,
            "top_referrer_uniques": top_ref_uniques,
            "top_path": top_path,
            "top_path_views": top_path_views,
            "top_path_uniques": top_path_uniques,
            "fetched_at": fetched_at,
            "language": language if language else None,
            "topics": topics if topics else [],
            "watchers_count": watchers_count,
            "open_issues_count": open_issues_count,
            "pushed_at": pushed_at,
            "created_at": created_at,
            "_daily_views": daily_views,
            "_daily_clones": daily_clones,
            "_referrers": full_refs,
            "_paths": full_paths,
        }

        if deep_stats and repo in deep_stats:
            ds = deep_stats[repo]
            for key in _DEEP_KEYS:
                value = ds.get(key)
                if value is not None:
                    repo_obj[key] = value

        repos.append(repo_obj)

    return repos
