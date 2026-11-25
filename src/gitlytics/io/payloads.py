# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Type coercion and JSON/React payload builders for tidy traffic DataFrames and row lists."""

from __future__ import annotations

import logging
from typing import Any

from .rows import (
    _NUMERIC_COLUMNS,
    build_json_payload_rows,
    build_react_payload_rows,
    is_nullish as _is_nullish,
    parse_raw as _parse_raw,
    safe_int as _safe_int,
    safe_str as _safe_str,
    to_bool as _to_bool,
)

logger = logging.getLogger(__name__)


def _df_to_row_dicts(df: Any) -> list[dict[str, Any]]:
    """Convert a pandas DataFrame into plain Python row dicts in one fast pass."""
    cols = list(df.columns)
    if not cols or len(df) == 0:
        return []
    col_lists = [df.iloc[:, i].tolist() for i in range(len(cols))]
    return [dict(zip(cols, values)) for values in zip(*col_lists)]


def _coerce_numeric_columns(df):
    """Strip commas and coerce metric columns to numeric values before aggregation."""
    import pandas as pd

    for col in _NUMERIC_COLUMNS:
        if col in df.columns:
            series = df[col]
            if not pd.api.types.is_numeric_dtype(series):
                series = (
                    series.astype(str).str.replace(",", "", regex=False).str.strip()
                )
            df[col] = pd.to_numeric(series, errors="coerce").fillna(0)
    return df


def _sum_metric(group, col: str) -> int:
    """Sum a metric column safely, returning 0 when the column is absent."""
    if col not in group.columns:
        return 0
    import pandas as pd

    return _safe_int(pd.to_numeric(group[col], errors="coerce").fillna(0).sum())


def _filter_public_only_df(df):
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
    df,
    return_format: str = "timeseries",
    export_public_only: bool = True,
) -> dict:
    """Transform a tidy traffic DataFrame or row list into the nested JSON payload."""
    if isinstance(df, (list, tuple)):
        return build_json_payload_rows(
            list(df),
            return_format=return_format,
            export_public_only=export_public_only,
        )

    if df is None or df.empty:
        return {"account_totals": {}, "repositories": {}}

    rows = _df_to_row_dicts(df)
    return build_json_payload_rows(
        rows,
        return_format=return_format,
        export_public_only=export_public_only,
    )


def build_react_payload(df, deep_stats: dict | None = None) -> list:
    """Transform a tidy DataFrame or row list into the RepoTraffic list for React."""
    if isinstance(df, (list, tuple)):
        return build_react_payload_rows(list(df), deep_stats=deep_stats)

    if df is None or df.empty:
        return []

    if "repository" not in df.columns:
        logger.warning("Cannot build payload: the frame has no 'repository' column.")
        return []

    rows = _df_to_row_dicts(df)
    return build_react_payload_rows(rows, deep_stats=deep_stats)
