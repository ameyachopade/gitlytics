# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Historical CSV database storage, locked merging, and JSON export."""

from __future__ import annotations

import csv
import logging
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gitlytics.io.atomic import _csv_lock, _write_json_safely
from gitlytics.io.rows import (
    build_react_payload_rows,
    filter_public_only_rows,
    is_nullish,
    merge_schema as _merge_schema,
    normalize_field as _normalize_field,
    read_csv_rows,
    safe_int,
)

logger = logging.getLogger(__name__)

# Matches canonical monthly (traffic_YYYY-MM.csv) and yearly (traffic_YYYY.csv) archives.
_SYNC_CSV_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")
_MONOTONIC_FIELDS = frozenset(("views", "unique_visitors", "clones", "unique_cloners"))


def get_csv_path(data_dir: str, mode: str, ref_date: datetime = None) -> str:
    """Resolve the monthly or yearly archive CSV path and create data_dir if needed."""
    data_dir_path = Path(data_dir).resolve()
    data_dir_path.mkdir(parents=True, exist_ok=True)

    ref = ref_date or datetime.now(timezone.utc)
    filename = (
        f"traffic_{ref.year}.csv"
        if mode == "yearly"
        else f"traffic_{ref.strftime('%Y-%m')}.csv"
    )
    return str(data_dir_path / filename)


def export_json_database(
    data_dir: str, export_path: str, export_public_only: bool = True
) -> None:
    """Merge all archive CSVs in data_dir and write a React-compatible JSON export atomically."""
    data_dir_path = Path(data_dir).resolve()
    if not data_dir_path.exists():
        return

    csv_files = sorted(
        (f for f in data_dir_path.glob("traffic_*.csv") if _SYNC_CSV_RE.match(f.name)),
        key=lambda f: (len(f.stem), f.name),
    )
    if not csv_files:
        return

    all_rows: list[dict[str, Any]] = []
    has_date = False
    has_repo = False
    for path in csv_files:
        try:
            cols, rows = read_csv_rows(path)
            if not cols and not rows:
                continue
            if "date" in cols:
                has_date = True
            if "repository" in cols:
                has_repo = True
            all_rows.extend(rows)
        except Exception as exc:
            logger.warning(f"Skipping corrupt or unreadable CSV file '{path}': {exc}")

    if not all_rows:
        return

    # Deduplicate overlapping monthly/yearly rows, keeping the newest schema revision.
    if has_date and has_repo:
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for row in all_rows:
            key = (str(row.get("date", "")), str(row.get("repository", "")))
            deduped[key] = row
        merged_rows = list(deduped.values())
    else:
        merged_rows = all_rows

    if export_public_only:
        merged_rows = filter_public_only_rows(merged_rows)

    payload = build_react_payload_rows(merged_rows)
    _write_json_safely(export_path, payload)


def _extract_incoming_rows(df_or_rows) -> tuple[list[str], list[dict[str, Any]]]:
    """Extract ordered field names and plain row dicts from a DataFrame or list of dicts."""
    if isinstance(df_or_rows, (list, tuple)):
        records = list(df_or_rows)
        if not records:
            return [], []
        fields: list[str] = []
        seen: set[str] = set()
        for rec in records:
            for key in rec.keys():
                if key not in seen:
                    seen.add(key)
                    fields.append(key)
        return fields, records

    cols = list(df_or_rows.columns)
    if len(df_or_rows) == 0:
        return cols, []
    col_lists = [df_or_rows.iloc[:, i].tolist() for i in range(len(cols))]
    records = [dict(zip(cols, values)) for values in zip(*col_lists)]
    return cols, records


def _write_df_to_csv(df, csv_path: str) -> int:
    """Serialize concurrent CSV merges with an advisory file lock."""
    with _csv_lock(csv_path):
        return _merge_csv_locked(df, csv_path)


def _merge_csv_locked(df, csv_path: str) -> int:
    """Merge incoming rows (DataFrame or list of dicts) into csv_path atomically."""
    file_exists = os.path.exists(csv_path)
    existing_fields: list[str] | None = None
    existing_data: dict[tuple[str, str], dict[str, Any]] = {}

    if file_exists:
        try:
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                try:
                    header_row = next(reader)
                except StopIteration:
                    header_row = None

                if header_row:
                    existing_fields = header_row
                    # Precompute normalized column map once, coalescing duplicate headers.
                    norm_indices: dict[str, list[int]] = {}
                    for idx, col_name in enumerate(header_row):
                        nk = _normalize_field(col_name)
                        if nk not in norm_indices:
                            norm_indices[nk] = [idx]
                        else:
                            norm_indices[nk].append(idx)

                    has_dupes = any(len(v) > 1 for v in norm_indices.values())
                    if not has_dupes:
                        col_map = [(nk, idxs[0]) for nk, idxs in norm_indices.items()]
                        for raw_row in reader:
                            if not raw_row:
                                continue
                            rlen = len(raw_row)
                            rec = {
                                nk: raw_row[idx] if idx < rlen else ""
                                for nk, idx in col_map
                            }
                            key = (
                                str(rec.get("repository", "")),
                                str(rec.get("date", "")),
                            )
                            existing_data[key] = rec
                    else:
                        col_map_multi = [
                            (nk, tuple(idxs)) for nk, idxs in norm_indices.items()
                        ]
                        for raw_row in reader:
                            if not raw_row:
                                continue
                            rlen = len(raw_row)
                            rec = {}
                            for nk, idxs in col_map_multi:
                                chosen = ""
                                for idx in idxs:
                                    if idx < rlen and not is_nullish(raw_row[idx]):
                                        chosen = raw_row[idx]
                                        break
                                rec[nk] = chosen
                            key = (
                                str(rec.get("repository", "")),
                                str(rec.get("date", "")),
                            )
                            existing_data[key] = rec
        except Exception as exc:
            logger.warning(
                f"Could not read existing CSV '{csv_path}': {exc}. Starting fresh."
            )
            existing_fields = None
            existing_data = {}

    new_fields, incoming_records = _extract_incoming_rows(df)
    existing_fields = (
        _merge_schema(existing_fields, new_fields) if existing_fields else new_fields
    )

    # Precompute normalized key map for incoming columns once before row iteration.
    incoming_norm_map = {col: _normalize_field(col) for col in new_fields}

    new_records_added = 0
    for raw_record in incoming_records:
        record = {
            incoming_norm_map.get(k) or _normalize_field(k): v
            for k, v in raw_record.items()
        }
        key = (str(record.get("repository", "")), str(record.get("date", "")))
        if key not in existing_data:
            new_records_added += 1
            existing_data[key] = record
            continue

        prev = existing_data[key]
        merged_rec = dict(prev)
        for nk, val in record.items():
            if nk in _MONOTONIC_FIELDS:
                prev_val = safe_int(prev.get(nk, 0), fallback=0)
                new_val = safe_int(val, fallback=0)
                if new_val == 0 and prev_val > 0:
                    continue
            merged_rec[nk] = val
        existing_data[key] = merged_rec

    # Precompute output column -> normalized key mapping and sort columns once.
    field_pairs = [(col, _normalize_field(col)) for col in existing_fields]
    date_col = next((col for col, nk in field_pairs if nk == "date"), None)
    repo_col = next((col for col, nk in field_pairs if nk == "repository"), None)

    final_rows: list[dict[str, Any]] = []
    for record in existing_data.values():
        clean_row: dict[str, Any] = {}
        for col, nk in field_pairs:
            val = record.get(nk, "")
            if val is None or (isinstance(val, float) and math.isnan(val)):
                val = ""
            elif not isinstance(val, (str, bool, int, float)) and is_nullish(val):
                val = ""
            clean_row[col] = val
        final_rows.append(clean_row)

    if date_col is not None or repo_col is not None:
        final_rows.sort(
            key=lambda row: (
                str(row.get(date_col, "")) if date_col is not None else "",
                str(row.get(repo_col, "")) if repo_col is not None else "",
            )
        )

    # Write into a sibling temp file then swap atomically with os.replace.
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(csv_path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(os.path.abspath(csv_path)),
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=existing_fields, extrasaction="ignore"
            )
            writer.writeheader()
            writer.writerows(final_rows)
        os.replace(tmp_path, csv_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return new_records_added
