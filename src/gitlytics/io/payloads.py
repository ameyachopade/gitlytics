# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Type coercion and JSON/React payload builders for tidy traffic DataFrames."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Columns that must stay numeric so pandas sums numbers instead of joining strings.
_NUMERIC_COLUMNS = (
    "views", "unique_visitors", "clones", "unique_cloners",
    "stars", "forks", "watchers_count", "open_issues_count",
)


def _is_nullish(val) -> bool:
    """Return True for None, NaN, pandas NA/NaT, empty strings, and literal nulls."""
    if val is None:
        return True
    if not isinstance(val, (list, tuple, dict, set)):
        try:
            if pd.isna(val):
                return True
        except (TypeError, ValueError):
            pass
    return str(val).strip().lower() in ("", "nan", "none", "null", "<na>", "nat")


def _to_bool(val) -> bool:
    """Parse booleans strictly so strings like 'False' never become True."""
    if isinstance(val, bool):
        return val
    if isinstance(val, np.bool_):
        return bool(val)
    if val is None:
        return False
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            if np.isnan(val):
                return False
        except (TypeError, ValueError):
            pass
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "y", "t")
    return False


def _parse_raw(val) -> list:
    """Decode a JSON-encoded list column safely, returning an empty list on bad input."""
    if _is_nullish(val):
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        logger.warning(f"_parse_raw: could not decode value {val!r:.80}; returning []")
        return []


def _safe_int(val, fallback: int = 0) -> int:
    """Convert a scalar (including comma-formatted numbers) to int with a fallback."""
    if _is_nullish(val):
        return fallback
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _safe_str(val, fallback: str = "") -> str:
    """Convert a value to str while replacing nullish entries with the fallback."""
    if _is_nullish(val):
        return fallback
    return str(val)


def _coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Strip commas and coerce metric columns to numeric values before aggregation."""
    for col in _NUMERIC_COLUMNS:
        if col in df.columns:
            series = df[col]
            if not pd.api.types.is_numeric_dtype(series):
                series = series.astype(str).str.replace(",", "", regex=False).str.strip()
            df[col] = pd.to_numeric(series, errors="coerce").fillna(0)
    return df


def _sum_metric(group: pd.DataFrame, col: str) -> int:
    """Sum a metric column safely, returning 0 when the column is absent."""
    if col not in group.columns:
        return 0
    return _safe_int(pd.to_numeric(group[col], errors="coerce").fillna(0).sum())


def _filter_public_only_df(df: pd.DataFrame) -> pd.DataFrame:
    """Exclude every row belonging to any repository marked private in any row."""
    if df.empty or "is_private" not in df.columns:
        return df
    is_priv = df["is_private"].map(_to_bool).astype(bool)
    if "repository" in df.columns:
        private_repos = set(df.loc[is_priv, "repository"].dropna().astype(str))
        if private_repos:
            return df[~df["repository"].astype(str).isin(private_repos)]
    return df[~is_priv]


def build_json_payload(
    df: pd.DataFrame,
    return_format: str = "timeseries",
    export_public_only: bool = True,
) -> dict:
    """Transform a tidy traffic DataFrame into the nested JSON summary or timeseries dict."""
    if df.empty:
        return {"account_totals": {}, "repositories": {}}

    # Filter out all rows for private repositories when public-only export is enabled.
    if export_public_only:
        df = _filter_public_only_df(df)

    if df.empty:
        return {"account_totals": {}, "repositories": {}}

    df = _coerce_numeric_columns(df.copy())

    account_views = 0
    account_clones = 0
    account_uniques = 0
    account_unique_cloners = 0
    account_stars = 0
    account_forks = 0
    repos_dict = {}

    for repo, group in df.groupby("repository"):
        group = group.sort_values("date")

        r_views = _sum_metric(group, "views")
        r_clones = _sum_metric(group, "clones")
        r_unique_v = _sum_metric(group, "unique_visitors")
        r_unique_c = _sum_metric(group, "unique_cloners")
        r_stars = _safe_int(group["stars"].dropna().iloc[-1]) if "stars" in group.columns and not group["stars"].dropna().empty else 0
        r_forks = _safe_int(group["forks"].dropna().iloc[-1]) if "forks" in group.columns and not group["forks"].dropna().empty else 0
        r_is_private = _to_bool(group["is_private"].dropna().iloc[-1]) if "is_private" in group.columns and not group["is_private"].dropna().empty else False
        top_ref = _safe_str(group["top_referrer"].dropna().iloc[-1]) if "top_referrer" in group.columns and not group["top_referrer"].dropna().empty else ""
        top_path = _safe_str(group["top_path"].dropna().iloc[-1]) if "top_path" in group.columns and not group["top_path"].dropna().empty else ""

        account_views += r_views
        account_clones += r_clones
        account_uniques += r_unique_v
        account_unique_cloners += r_unique_c
        account_stars += r_stars
        account_forks += r_forks

        if return_format == "summary":
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
            timeseries = [
                {
                    "date": str(record.get("date", "")),
                    "views": _safe_int(record.get("views", 0)),
                    "unique_visitors": _safe_int(record.get("unique_visitors", 0)),
                    "clones": _safe_int(record.get("clones", 0)),
                    "unique_cloners": _safe_int(record.get("unique_cloners", 0)),
                }
                for record in group.to_dict("records")
            ]
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


def build_react_payload(df: pd.DataFrame, deep_stats: dict = None) -> list:
    """Transform a tidy DataFrame into the RepoTraffic list expected by the React dashboard."""
    if df.empty:
        return []

    if "repository" not in df.columns:
        logger.warning("Cannot build payload: the frame has no 'repository' column.")
        return []

    df = _coerce_numeric_columns(df.copy())
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = []

    deep_keys = (
        "total_commits", "open_prs", "total_releases", "last_release_at",
        "has_readme", "has_license", "has_contributing", "has_code_of_conduct",
    )

    for repo, group in df.groupby("repository"):
        group = group.sort_values("date")

        r_views = _sum_metric(group, "views")
        r_clones = _sum_metric(group, "clones")
        r_unique_v = _sum_metric(group, "unique_visitors")
        r_unique_c = _sum_metric(group, "unique_cloners")
        r_stars = _safe_int(group["stars"].dropna().iloc[-1]) if "stars" in group.columns and not group["stars"].dropna().empty else 0
        r_forks = _safe_int(group["forks"].dropna().iloc[-1]) if "forks" in group.columns and not group["forks"].dropna().empty else 0
        r_is_private = _to_bool(group["is_private"].dropna().iloc[-1]) if "is_private" in group.columns and not group["is_private"].dropna().empty else False

        top_ref = _safe_str(group["top_referrer"].dropna().iloc[-1]) if "top_referrer" in group.columns and not group["top_referrer"].dropna().empty else ""
        top_ref_views = _safe_int(group["top_referrer_views"].dropna().iloc[-1]) if "top_referrer_views" in group.columns and not group["top_referrer_views"].dropna().empty else 0
        top_ref_uniques = _safe_int(group["top_referrer_uniques"].dropna().iloc[-1]) if "top_referrer_uniques" in group.columns and not group["top_referrer_uniques"].dropna().empty else 0
        top_path = _safe_str(group["top_path"].dropna().iloc[-1]) if "top_path" in group.columns and not group["top_path"].dropna().empty else ""
        top_path_views = _safe_int(group["top_path_views"].dropna().iloc[-1]) if "top_path_views" in group.columns and not group["top_path_views"].dropna().empty else 0
        top_path_uniques = _safe_int(group["top_path_uniques"].dropna().iloc[-1]) if "top_path_uniques" in group.columns and not group["top_path_uniques"].dropna().empty else 0

        # Pick the latest non-null repo metadata values from the tidy rows.
        language = _safe_str(group["language"].dropna().iloc[-1]) if "language" in group.columns and not group["language"].dropna().empty else None
        watchers_count = _safe_int(group["watchers_count"].dropna().iloc[-1]) if "watchers_count" in group.columns and not group["watchers_count"].dropna().empty else 0
        open_issues_count = _safe_int(group["open_issues_count"].dropna().iloc[-1]) if "open_issues_count" in group.columns and not group["open_issues_count"].dropna().empty else 0
        pushed_at = _safe_str(group["pushed_at"].dropna().iloc[-1]) if "pushed_at" in group.columns and not group["pushed_at"].dropna().empty else None
        created_at = _safe_str(group["created_at"].dropna().iloc[-1]) if "created_at" in group.columns and not group["created_at"].dropna().empty else None

        topics_raw = group["topics"].dropna().iloc[-1] if "topics" in group.columns and not group["topics"].dropna().empty else None
        topics = _parse_raw(topics_raw) if topics_raw else []

        daily_views = []
        daily_clones = []
        for record in group.to_dict("records"):
            date_str = str(record.get("date", ""))
            daily_views.append({
                "timestamp": date_str,
                "count": _safe_int(record.get("views", 0)),
                "uniques": _safe_int(record.get("unique_visitors", 0)),
            })
            daily_clones.append({
                "timestamp": date_str,
                "count": _safe_int(record.get("clones", 0)),
                "uniques": _safe_int(record.get("unique_cloners", 0)),
            })

        # Keep the newest non-null referrer/path snapshots even if today's row is empty.
        raw_refs_val = (
            group["_raw_referrers"].dropna().iloc[-1]
            if "_raw_referrers" in group.columns and not group["_raw_referrers"].dropna().empty
            else None
        )
        raw_paths_val = (
            group["_raw_paths"].dropna().iloc[-1]
            if "_raw_paths" in group.columns and not group["_raw_paths"].dropna().empty
            else None
        )
        full_refs = _parse_raw(raw_refs_val)
        full_paths = _parse_raw(raw_paths_val)

        if not full_refs and top_ref:
            full_refs = [{"referrer": top_ref, "count": top_ref_views, "uniques": top_ref_uniques}]
        if not full_paths and top_path:
            full_paths = [{"path": top_path, "title": top_path, "count": top_path_views, "uniques": top_path_uniques}]

        repo_obj = {
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

        # Attach deep stats when fetched, omitting null fields to keep payloads compact.
        if deep_stats and repo in deep_stats:
            ds = deep_stats[repo]
            for key in deep_keys:
                value = ds.get(key)
                if value is not None:
                    repo_obj[key] = value

        repos.append(repo_obj)

    return repos
