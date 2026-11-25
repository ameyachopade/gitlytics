# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Data I/O, CSV normalization, payload builders, and terminal formatting."""

from .atomic import _csv_lock, _write_csv_safely, _write_json_safely
from .csv import (
    _LEGACY_COLUMN_MAP,
    _merge_schema,
    _normalize_field,
    _parse_date_column,
    normalize_csv_dataframe,
    process_uploaded_csv,
)
from .formatting import print_repo_table
from .payloads import (
    _NUMERIC_COLUMNS,
    _coerce_numeric_columns,
    _is_nullish,
    _parse_raw,
    _safe_int,
    _safe_str,
    _sum_metric,
    _to_bool,
    build_json_payload,
    build_react_payload,
)
from .rows import (
    build_json_payload_rows,
    build_react_payload_rows,
    filter_public_only_rows,
    normalize_field,
    read_csv_rows,
)

__all__ = [
    "build_json_payload",
    "build_json_payload_rows",
    "build_react_payload",
    "build_react_payload_rows",
    "filter_public_only_rows",
    "normalize_csv_dataframe",
    "normalize_field",
    "print_repo_table",
    "process_uploaded_csv",
    "read_csv_rows",
]
