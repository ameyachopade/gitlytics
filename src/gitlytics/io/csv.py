# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""CSV header normalization, schema evolution, upload parsing, and date parsing."""
from __future__ import annotations

import logging

import pandas as pd

from .payloads import _coerce_numeric_columns, _to_bool

logger = logging.getLogger(__name__)

# Maps legacy display headers to canonical snake_case column names.
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


def _normalize_field(name: str) -> str:
    """Strip BOMs, trim whitespace, and lower-case a single CSV column header."""
    if not isinstance(name, str):
        return ""
    return name.lstrip("\ufeff").strip().lower()


def _merge_schema(existing_fields: list, new_fields: list) -> list:
    """Keep existing CSV column order while appending newly introduced columns."""
    norm_existing = [_normalize_field(f) for f in existing_fields]
    merged_norm = list(norm_existing)
    merged_names = list(existing_fields)
    for col in new_fields:
        norm = _normalize_field(col)
        if norm and norm not in merged_norm:
            logger.info(f"Schema upgrade: adding new column '{col}' to existing CSV.")
            merged_norm.append(norm)
            merged_names.append(col)
    return merged_names


def _parse_date_column(series: pd.Series) -> pd.Series:
    """Parse mixed date formats safely so valid rows are never turned into NaT."""
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return pd.to_datetime(series, errors="coerce")


def normalize_csv_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize CSV headers to canonical lowercase names and coalesce duplicates."""
    frame = df.copy()
    frame.columns = [_normalize_field(str(c)) for c in frame.columns]
    frame.columns = [_LEGACY_COLUMN_MAP.get(c, c) for c in frame.columns]

    # Backfill duplicate alias columns so df[col] is always a single Series.
    if frame.columns.duplicated().any():
        deduped = {}
        for col_name in dict.fromkeys(frame.columns):
            matches = frame.loc[:, frame.columns == col_name]
            if matches.shape[1] == 1:
                deduped[col_name] = matches.iloc[:, 0]
            else:
                deduped[col_name] = matches.bfill(axis=1).iloc[:, 0]
        frame = pd.DataFrame(deduped, index=frame.index)

    return frame


# Caps uploaded CSV size in rows and unique repositories to bound memory/CPU work.
_MAX_UPLOAD_ROWS = 50_000
_MAX_UPLOAD_REPOS = 2_000


def process_uploaded_csv(uploaded_file) -> pd.DataFrame:
    """Read an uploaded CSV file and normalize its headers, booleans, and numeric columns."""
    raw_df = normalize_csv_dataframe(pd.read_csv(uploaded_file, nrows=_MAX_UPLOAD_ROWS + 1))

    if len(raw_df) > _MAX_UPLOAD_ROWS:
        raise ValueError(f"CSV exceeds maximum allowed row count ({_MAX_UPLOAD_ROWS}).")
    if "repository" not in raw_df.columns:
        raise ValueError("Invalid CSV format: missing 'repository' column")
    if "date" not in raw_df.columns:
        raise ValueError("Invalid CSV format: missing required 'date' column")
    if raw_df["repository"].nunique(dropna=True) > _MAX_UPLOAD_REPOS:
        raise ValueError(f"CSV exceeds maximum allowed unique repositories ({_MAX_UPLOAD_REPOS}).")

    if "is_private" in raw_df.columns:
        raw_df["is_private"] = raw_df["is_private"].map(_to_bool).astype(bool)

    return _coerce_numeric_columns(raw_df)
