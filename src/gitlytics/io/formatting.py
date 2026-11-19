# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Terminal table formatting for repository traffic summaries."""
from __future__ import annotations

import pandas as pd


def _col_sum(frame: pd.DataFrame, col: str) -> int:
    """Sum a column cleanly even when values arrive as formatted strings or inf/NaN."""
    if col not in frame.columns:
        return 0
    series = frame[col]
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype(str).str.replace(",", "", regex=False).str.strip()
    numeric = pd.to_numeric(series, errors="coerce").replace([float("inf"), float("-inf")], 0).fillna(0)
    return int(numeric.sum())


def _col_last(frame: pd.DataFrame, col: str) -> int:
    """Return the latest finite integer value from a column, or 0 if empty."""
    if col not in frame.columns:
        return 0
    series = frame[col]
    if not pd.api.types.is_numeric_dtype(series):
        series = series.astype(str).str.replace(",", "", regex=False).str.strip()
    numeric = pd.to_numeric(series, errors="coerce").replace([float("inf"), float("-inf")], float("nan")).dropna()
    return int(numeric.iloc[-1]) if not numeric.empty else 0


def print_repo_table(df: pd.DataFrame) -> None:
    """Print an aligned ASCII summary table of repository traffic to stdout."""
    if df.empty:
        print("No data to display.")
        return

    # Convert to object first so replacing NaN with "" avoids pandas dtype warnings.
    safe_df = df.astype(object).where(df.notna(), "")

    repo_width = max(30, safe_df["repository"].astype(str).str.len().max() + 2)
    ref_width = max(15, safe_df["top_referrer"].astype(str).str.len().max() + 2) if "top_referrer" in safe_df.columns else 15
    header = (
        f"{'REPOSITORY':<{repo_width}} "
        f"{'VIEWS':<8} {'U.VIEWS':<8} {'CLONES':<8} {'U.CLONES':<8} "
        f"{'STARS':<8} {'FORKS':<8} {'TOP REFERRER':<{ref_width}}"
    )
    print(header)
    print("-" * len(header))

    total_account_views = 0
    total_account_uniques = 0
    total_account_clones = 0

    for repo in df["repository"].unique():
        repo_df = df[df["repository"] == repo]
        views = _col_sum(repo_df, "views")
        u_views = _col_sum(repo_df, "unique_visitors")
        clones = _col_sum(repo_df, "clones")
        u_clones = _col_sum(repo_df, "unique_cloners")
        stars = _col_last(repo_df, "stars")
        forks = _col_last(repo_df, "forks")

        top_ref_series = (
            repo_df["top_referrer"].dropna().astype(str)
            if "top_referrer" in repo_df.columns
            else pd.Series(dtype=str)
        )
        top_ref = top_ref_series.iloc[-1] if not top_ref_series.empty and top_ref_series.iloc[-1] != "nan" else ""

        total_account_views += views
        total_account_uniques += u_views
        total_account_clones += clones

        print(
            f"{repo:<{repo_width}} "
            f"{views:<8} {u_views:<8} {clones:<8} {u_clones:<8} "
            f"{stars:<8} {forks:<8} {str(top_ref):<{ref_width}}"
        )

    print("-" * len(header))
    print(f"Total Account Views:  {total_account_views} (Unique: {total_account_uniques})")
    print(f"Total Account Clones: {total_account_clones}")
