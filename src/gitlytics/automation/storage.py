# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Historical CSV database storage, locked merging, and JSON export."""
from __future__ import annotations

import csv
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from gitlytics.io.atomic import _csv_lock, _write_json_safely
from gitlytics.io.csv import _merge_schema, _normalize_field
from gitlytics.io.payloads import _filter_public_only_df, build_react_payload

logger = logging.getLogger(__name__)

# Matches canonical monthly (traffic_YYYY-MM.csv) and yearly (traffic_YYYY.csv) archives.
_SYNC_CSV_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")


def get_csv_path(data_dir: str, mode: str, ref_date: datetime = None) -> str:
    """Resolve the monthly or yearly archive CSV path and create data_dir if needed."""
    data_dir_path = Path(data_dir).resolve()
    data_dir_path.mkdir(parents=True, exist_ok=True)

    ref = ref_date or datetime.now(timezone.utc)
    filename = f"traffic_{ref.year}.csv" if mode == "yearly" else f"traffic_{ref.strftime('%Y-%m')}.csv"
    return str(data_dir_path / filename)


def export_json_database(data_dir: str, export_path: str, export_public_only: bool = True) -> None:
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

    dfs = []
    for path in csv_files:
        try:
            frame = pd.read_csv(path)
            frame.columns = [_normalize_field(c) for c in frame.columns]
            frame = frame.loc[:, ~frame.columns.duplicated()]
            dfs.append(frame)
        except Exception as exc:
            logger.warning(f"Skipping corrupt or unreadable CSV file '{path}': {exc}")

    if not dfs:
        return

    master_df = pd.concat(dfs, ignore_index=True)

    # Deduplicate overlapping monthly/yearly rows, keeping the newest schema revision.
    if "date" in master_df.columns and "repository" in master_df.columns:
        master_df = master_df.drop_duplicates(subset=["date", "repository"], keep="last")

    if export_public_only:
        master_df = _filter_public_only_df(master_df)

    payload = build_react_payload(master_df)
    _write_json_safely(export_path, payload)


def _write_df_to_csv(df: pd.DataFrame, csv_path: str) -> int:
    """Serialize concurrent CSV merges with an advisory file lock."""
    with _csv_lock(csv_path):
        return _merge_csv_locked(df, csv_path)


def _merge_csv_locked(df: pd.DataFrame, csv_path: str) -> int:
    """Merge incoming rows into csv_path atomically and return the new-record count."""
    file_exists = os.path.exists(csv_path)
    existing_fields = None
    existing_data = {}

    if file_exists:
        with open(csv_path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                existing_fields = next(reader)
            except StopIteration:
                pass

        try:
            existing_df = pd.read_csv(csv_path)
            existing_df.columns = [_normalize_field(c) for c in existing_df.columns]
            existing_df = existing_df.loc[:, ~existing_df.columns.duplicated()]
            for record in existing_df.to_dict("records"):
                key = (str(record.get("repository", "")), str(record.get("date", "")))
                existing_data[key] = record
        except Exception as exc:
            logger.warning(f"Could not read existing CSV '{csv_path}': {exc}. Starting fresh.")

    new_fields = list(df.columns)
    existing_fields = _merge_schema(existing_fields, new_fields) if existing_fields else new_fields

    # Guard cumulative daily counters so a zero glitch never wipes real history.
    monotonic_fields = {"views", "unique_visitors", "clones", "unique_cloners"}
    new_records_added = 0
    for raw_record in df.to_dict("records"):
        record = {_normalize_field(k): v for k, v in raw_record.items()}
        key = (str(record.get("repository", "")), str(record.get("date", "")))
        if key not in existing_data:
            new_records_added += 1
            existing_data[key] = record
            continue

        prev = existing_data[key]
        merged_rec = dict(prev)
        for nk, val in record.items():
            if nk in monotonic_fields:
                try:
                    prev_val = int(float(prev.get(nk, 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    prev_val = 0
                try:
                    new_val = int(float(val or 0))
                except (TypeError, ValueError, OverflowError):
                    new_val = 0
                if new_val == 0 and prev_val > 0:
                    continue
            merged_rec[nk] = val
        existing_data[key] = merged_rec

    final_rows = []
    for record in existing_data.values():
        clean_row = {}
        for col in existing_fields:
            nk = _normalize_field(col)
            if nk in record:
                val = record[nk]
            else:
                match = next((orig for orig in record.keys() if _normalize_field(orig) == nk), None)
                val = record[match] if match is not None else ""
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = ""
            clean_row[col] = val
        final_rows.append(clean_row)

    def _row_sort_key(row: dict) -> tuple[str, str]:
        date_val = next((v for k, v in row.items() if _normalize_field(k) == "date"), "")
        repo_val = next((v for k, v in row.items() if _normalize_field(k) == "repository"), "")
        return (str(date_val), str(repo_val))

    final_rows.sort(key=_row_sort_key)

    # Write into a sibling temp file then swap atomically with os.replace.
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(csv_path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(os.path.abspath(csv_path)),
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=existing_fields, extrasaction="ignore")
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
