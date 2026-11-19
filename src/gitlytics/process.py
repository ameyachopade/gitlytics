# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Backward-compatibility re-exports for gitlytics.process."""

from .io.csv import _LEGACY_COLUMN_MAP, process_uploaded_csv
from .io.payloads import (
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

__all__ = [
    "build_json_payload",
    "build_react_payload",
    "process_uploaded_csv",
]
