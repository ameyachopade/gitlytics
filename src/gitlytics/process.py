"""
gitlytics/process.py
Handles processing Tidy DataFrames into final JSON formats for the dashboard.
"""
import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _is_nullish(val) -> bool:
    # True for None, NaN, pd.NA, pd.NaT, empty string, and literal nulls.
    if val is None:
        return True
    if not isinstance(val, (list, tuple, dict, set)):
        try:
            if pd.isna(val):
                return True
        except (TypeError, ValueError):
            pass
    s = str(val).strip().lower()
    return s in ("", "nan", "none", "null", "<na>", "nat")


def _to_bool(val) -> bool:
    """Parse a value as a strict boolean.

    `bool("False")` returns True because every non-empty string is truthy,
    which silently corrupts CSV-roundtripped `is_private` columns. This
    helper accepts bool / numeric / common string spellings and returns
    False for anything ambiguous.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, np.bool_):
        # numpy.bool_ is not a subclass of Python bool; without this check a
        # private repo can be misclassified as public in export filtering.
        return bool(val)
    if val is None:
        return False
    if isinstance(val, (int, float, np.integer, np.floating)):
        # Catch NaN explicitly so a missing-cell numeric doesn't propagate.
        try:
            if np.isnan(val):
                return False
        except (TypeError, ValueError):
            pass
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "y", "t")
    return False


def build_json_payload(df: pd.DataFrame, return_format: str = "timeseries", export_public_only: bool = True) -> dict:
    """Transforms the Tidy Data DataFrame into the nested JSON structure."""
    if df.empty:
        return {"account_totals": {}, "repositories": {}}

    if export_public_only and "is_private" in df.columns:
        is_priv = df["is_private"].map(_to_bool).astype(bool)
        df = df[~is_priv]

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
                "forks": r_forks
            }
        else:
            timeseries = []
            for record in group.to_dict("records"):
                timeseries.append({
                    "date": str(record.get("date", "")),
                    "views": _safe_int(record.get("views", 0)),
                    "unique_visitors": _safe_int(record.get("unique_visitors", 0)),
                    "clones": _safe_int(record.get("clones", 0)),
                    "unique_cloners": _safe_int(record.get("unique_cloners", 0))
                })

            repos_dict[repo] = {
                "timeseries": timeseries,
                "totals": {
                    "is_private": r_is_private,
                    "stars": r_stars,
                    "forks": r_forks,
                    "top_referrer": top_ref,
                    "top_path": top_path
                }
            }

    account_totals = {
        "total_views": account_views,
        "total_clones": account_clones,
        "unique_visitors": account_uniques,
        "unique_cloners": account_unique_cloners,
        "total_stars": account_stars,
        "total_forks": account_forks
    }

    return {
        "account_totals": account_totals,
        "repositories": repos_dict
    }


_LEGACY_COLUMN_MAP = {
    "repository": "repository",
    "repo_name": "repository",
    "total views": "views",
    "unique visitors": "unique_visitors",
    "total clones": "clones",
    "unique cloners": "unique_cloners",
    "stars": "stars",
    "forks": "forks",
}


def process_uploaded_csv(uploaded_file) -> pd.DataFrame:
    """Reads a user-uploaded CSV and normalises column names to match our tidy schema."""
    raw_df = pd.read_csv(uploaded_file)

    # Strip BOMs and lower-case every header up front so any common title-case
    # or mixed-case export (e.g. hand-edited CSVs, older versions, third-party tools)
    # is accepted.
    raw_df.columns = [str(c).lstrip("\ufeff").strip().lower() for c in raw_df.columns]

    # Map legacy display names (e.g. "Total Views", "Unique Visitors") to the
    # canonical schema columns that build_react_payload / build_json_payload expect.
    raw_df.columns = [_LEGACY_COLUMN_MAP.get(c, c) for c in raw_df.columns]

    # Coalesce duplicate column names caused by case/alias collisions ("Views" +
    # "views") so df[col] is always a Series, never a 2-column DataFrame (AF-LIB-11).
    if raw_df.columns.duplicated().any():
        deduped_cols = {}
        for col_name in dict.fromkeys(raw_df.columns):
            matches = raw_df.loc[:, raw_df.columns == col_name]
            if matches.shape[1] == 1:
                deduped_cols[col_name] = matches.iloc[:, 0]
            else:
                deduped_cols[col_name] = matches.bfill(axis=1).iloc[:, 0]
        raw_df = pd.DataFrame(deduped_cols, index=raw_df.index)

    if "repository" not in raw_df.columns:
        raise ValueError("Invalid CSV format: missing 'repository' column")

    if "date" not in raw_df.columns:
        raise ValueError("Invalid CSV format: missing required 'date' column")

    # Coerce `is_private` to a real bool. CSVs from any external tool tend
    # to write True/False as strings ("True"/"False") or as 1/0, and a
    # missing column concatenated later widens the dtype to object. Doing
    # this here keeps every downstream consumer safe.
    if "is_private" in raw_df.columns:
        raw_df["is_private"] = raw_df["is_private"].map(_to_bool).astype(bool)

    # Normalise numeric metric columns so downstream sums add numbers rather
    # than concatenating object-dtype strings ("10" + "5" -> "105").
    raw_df = _coerce_numeric_columns(raw_df)
    return raw_df


def _parse_raw(val) -> list:
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


def _safe_int(val, fallback=0) -> int:
    if _is_nullish(val):
        return fallback
    try:
        # Tolerate thousands separators and surrounding whitespace, which are
        # common in hand-edited/third-party CSV exports ("1,234" -> 1234).
        return int(float(str(val).replace(",", "").strip()))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _safe_str(val, fallback="") -> str:
    if _is_nullish(val):
        return fallback
    return str(val)


# Metric columns that must always be numeric. A single string cell in an
# object-dtype column makes ``.sum()`` concatenate ("10" + "5" -> "105"),
# silently inflating totals, so coerce once before any aggregation.
_NUMERIC_COLUMNS = (
    "views", "unique_visitors", "clones", "unique_cloners",
    "stars", "forks", "watchers_count", "open_issues_count",
)


def _coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    for col in _NUMERIC_COLUMNS:
        if col in df.columns:
            series = df[col]
            if not pd.api.types.is_numeric_dtype(series):
                # Hand-edited/third-party exports use thousands separators
                # ("1,234"). Strip them before coercion so they are parsed as
                # numbers instead of collapsing to NaN -> 0. This also covers
                # pandas' dedicated string dtype, not just object.
                series = series.astype(str).str.replace(",", "", regex=False).str.strip()
            df[col] = pd.to_numeric(series, errors="coerce").fillna(0)
    return df


def _sum_metric(group: pd.DataFrame, col: str) -> int:
    if col not in group.columns:
        return 0
    return _safe_int(pd.to_numeric(group[col], errors="coerce").fillna(0).sum())


def build_react_payload(df: pd.DataFrame, deep_stats: dict = None) -> list:
    """
    Transforms the Tidy DataFrame into the RepoTraffic array the React dashboard expects.
    Optionally merges in deep stats (commits, PRs, releases, README) for the top 20 repos.
    """
    if df.empty:
        return []

    # A frame without the grouping key would raise KeyError and take the
    # whole dashboard down with an HTTP 500. Return an empty payload instead
    # (AF2-LIB-02).
    if "repository" not in df.columns:
        logger.warning("Cannot build payload: the frame has no 'repository' column.")
        return []

    df = _coerce_numeric_columns(df.copy())

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repos = []

    # Deep-stat keys we copy from the deep_stats dict into each repo object.
    _DEEP_KEYS = (
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

        # Repo-level metadata from the CSV rows (populated by build_tidy_rows)
        language = _safe_str(group["language"].dropna().iloc[-1]) if "language" in group.columns and not group["language"].dropna().empty else None
        watchers_count = _safe_int(group["watchers_count"].dropna().iloc[-1]) if "watchers_count" in group.columns and not group["watchers_count"].dropna().empty else 0
        open_issues_count = _safe_int(group["open_issues_count"].dropna().iloc[-1]) if "open_issues_count" in group.columns and not group["open_issues_count"].dropna().empty else 0
        pushed_at = _safe_str(group["pushed_at"].dropna().iloc[-1]) if "pushed_at" in group.columns and not group["pushed_at"].dropna().empty else None
        created_at = _safe_str(group["created_at"].dropna().iloc[-1]) if "created_at" in group.columns and not group["created_at"].dropna().empty else None

        # Topics stored as JSON string in CSV
        topics_raw = group["topics"].dropna().iloc[-1] if "topics" in group.columns and not group["topics"].dropna().empty else None
        topics = _parse_raw(topics_raw) if topics_raw else []

        # Daily arrays for charts
        daily_views = []
        daily_clones = []
        for record in group.to_dict("records"):
            date_str = str(record.get("date", ""))
            daily_views.append({
                "timestamp": date_str,
                "count": _safe_int(record.get("views", 0)),
                "uniques": _safe_int(record.get("unique_visitors", 0))
            })
            daily_clones.append({
                "timestamp": date_str,
                "count": _safe_int(record.get("clones", 0)),
                "uniques": _safe_int(record.get("unique_cloners", 0))
            })

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

        # Start building the repo object
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
            # Repo metadata (available from GitHub API, None for CSV uploads)
            "language": language if language else None,
            "topics": topics if topics else [],
            "watchers_count": watchers_count,
            "open_issues_count": open_issues_count,
            "pushed_at": pushed_at,
            "created_at": created_at,
            "_daily_views": daily_views,
            "_daily_clones": daily_clones,
            "_referrers": full_refs,
            "_paths": full_paths
        }

        # Merge deep stats if available for this repo. Only include keys that
        # actually have a non-null value — keeps the dashboard payload tight.
        if deep_stats and repo in deep_stats:
            ds = deep_stats[repo]
            for key in _DEEP_KEYS:
                value = ds.get(key)
                if value is not None:
                    repo_obj[key] = value

        repos.append(repo_obj)

    return repos
