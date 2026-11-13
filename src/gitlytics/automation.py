"""
gitlytics/automation.py
Handles continuous database appending, cron jobs, and JSON exporting.
"""
import os
import csv
import re
import time
import json
import logging
import tempfile
import contextlib
from datetime import datetime, timezone
from pathlib import Path

from croniter import croniter
import numpy as np
import pandas as pd

from gitlytics.core import (
    fetch_traffic_data,
    validate_token,
    TokenExpiredError,
    GitHubRateLimitError,
)
from gitlytics.process import build_react_payload, _to_bool

logger = logging.getLogger(__name__)


# `TokenExpiredError` is defined once in gitlytics.core and re-exported here so
# existing `from gitlytics.automation import TokenExpiredError` imports keep
# working without creating a second, non-matching exception class.


_SYNC_CSV_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")


def get_csv_path(data_dir: str, mode: str, ref_date: datetime = None) -> str:
    # Use ref_date (actual data month) instead of today to avoid cross-month file naming bugs.
    data_dir_path = Path(data_dir).resolve()
    data_dir_path.mkdir(parents=True, exist_ok=True)

    d = ref_date or datetime.now(timezone.utc)
    if mode == "yearly":
        filename = f"traffic_{d.year}.csv"
    else:
        filename = f"traffic_{d.strftime('%Y-%m')}.csv"

    return str(data_dir_path / filename)



def export_json_database(data_dir: str, export_path: str, export_public_only: bool = True):
    # Read every CSV in the data folder and merge them into one big JSON file for the dashboard
    data_dir_path = Path(data_dir).resolve()
    if not data_dir_path.exists():
        return

    csv_files = sorted(
        (
            f for f in data_dir_path.glob("traffic_*.csv")
            if _SYNC_CSV_RE.match(f.name)
        ),
        key=lambda f: (len(f.stem), f.name),
    )
    if not csv_files:
        return

    # Load each CSV, normalising column names so mixed-case legacy headers
    # (Date, Repository) deduplicate and build payloads cleanly (AF-LIB-01).
    dfs = []
    for f in csv_files:
        try:
            frame = pd.read_csv(f)
            frame.columns = [_normalize_field(c) for c in frame.columns]
            frame = frame.loc[:, ~frame.columns.duplicated()]
            dfs.append(frame)
        except Exception as exc:
            logger.warning(f"Skipping corrupt or unreadable CSV file '{f}': {exc}")

    if not dfs:
        return

    # Combine all historical data into one DataFrame
    master_df = pd.concat(dfs, ignore_index=True)

    # Deduplicate — if both a monthly and yearly CSV exist they contain overlapping rows.
    # Keep the LAST occurrence (most recently written) so schema migrations win.
    if "date" in master_df.columns and "repository" in master_df.columns:
        master_df = master_df.drop_duplicates(subset=["date", "repository"], keep="last")

    # M-3: use build_react_payload (list format) so the export matches what the dashboard expects
    if export_public_only and "is_private" in master_df.columns:
        is_priv = master_df["is_private"].map(_to_bool).astype(bool)
        master_df = master_df[~is_priv]
    payload = build_react_payload(master_df)

    # Write the JSON to disk atomically, creating parent folders if needed
    export_file = Path(export_path).resolve()
    export_file.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_export_",
        suffix=".json",
        dir=export_file.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, export_file)
    except BaseException:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _normalize_field(name: str) -> str:
    # Strip BOM, lower-case, and trim so older CSVs match the new schema.
    if not isinstance(name, str):
        return ""
    return name.lstrip("﻿").strip().lower()


def _merge_schema(existing_fields: list, new_fields: list) -> list:
    """
    Merges old and new CSV column schemas without dropping new fields.
    Existing fields keep their original order; new fields are appended at the end
    so historical rows stay compatible with the updated schema.
    """
    # Normalize on the fly so 'Date' and 'date' are treated identically.
    norm_existing = [_normalize_field(f) for f in existing_fields]
    merged_norm = list(norm_existing)
    original_for_norm = list(existing_fields)
    for col in new_fields:
        n = _normalize_field(col)
        if n and n not in merged_norm:
            # A new column appeared in the API response — add it so it gets saved
            logger.info(f"Schema upgrade: adding new column '{col}' to existing CSV.")
            merged_norm.append(n)
            original_for_norm.append(col)
    # Return the original-cased names where possible so the CSV header stays human-readable.
    return original_for_norm


def _write_df_to_csv(df: pd.DataFrame, csv_path: str) -> int:
    # Serialize concurrent merges (cron + manual run, or two containers sharing
    # a volume) so one process cannot lose the other's rows.
    with _csv_lock(csv_path):
        return _merge_csv_locked(df, csv_path)


@contextlib.contextmanager
def _csv_lock(csv_path: str, timeout: float = 30.0):
    """Cross-platform advisory lock around a CSV read-modify-write cycle."""
    lock_path = os.path.abspath(csv_path) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for CSV lock: {lock_path}")
                time.sleep(0.05)
        yield
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _merge_csv_locked(df: pd.DataFrame, csv_path: str) -> int:
    # Merge incoming df rows into the target CSV file, returns count of new records added.
    file_exists = os.path.exists(csv_path)
    existing_fields = None
    existing_data = {}

    if file_exists:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
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
    if existing_fields:
        existing_fields = _merge_schema(existing_fields, new_fields)
    else:
        existing_fields = new_fields

    _MONOTONIC_FIELDS = {"views", "unique_visitors", "clones", "unique_cloners"}
    new_records_added = 0
    for raw_record in df.to_dict("records"):
        record = {_normalize_field(k): v for k, v in raw_record.items()}
        key = (str(record.get("repository", "")), str(record.get("date", "")))
        if key not in existing_data:
            new_records_added += 1
            existing_data[key] = record
        else:
            prev = existing_data[key]
            merged_rec = dict(prev)
            for nk, val in record.items():
                if nk in _MONOTONIC_FIELDS:
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
    for v in existing_data.values():
        clean_row = {}
        for k in existing_fields:
            nk = _normalize_field(k)
            if nk in v:
                val = v[nk]
            else:
                match = next((orig for orig in v.keys() if _normalize_field(orig) == nk), None)
                val = v[match] if match is not None else ""
            if val is None or (isinstance(val, float) and np.isnan(val)):
                val = ""
            clean_row[k] = val
        final_rows.append(clean_row)

    def _row_sort_key(row: dict) -> tuple[str, str]:
        date_val = next((v for k, v in row.items() if _normalize_field(k) == "date"), "")
        repo_val = next((v for k, v in row.items() if _normalize_field(k) == "repository"), "")
        return (str(date_val), str(repo_val))

    final_rows.sort(key=_row_sort_key)

    # Unique temp file in the same directory keeps os.replace atomic while
    # allowing concurrent writers to target the same CSV safely.
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(csv_path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(os.path.abspath(csv_path)),
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=existing_fields, extrasaction='ignore')
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


class NoTrafficDataError(RuntimeError):
    """Raised when a sync cycle fetched zero traffic rows.

    A scheduled sync that writes nothing must never look like a success
    (AF2-LIB-04). The CLI maps this to exit code 1 with a clean message.
    """


def _parse_date_column(series: pd.Series) -> pd.Series:
    """Parse a date column defensively, tolerating mixed formats.

    ``pd.to_datetime`` infers a single format from the first element, so a
    frame that mixes ``2026-11-01`` and ``01/02/2026`` turned every row
    except the first into ``NaT`` — and those rows were then silently dropped
    by the ``groupby`` (AF2-LIB-08).

    ``format="mixed"`` (pandas >= 2) parses each element with its own
    inferred format. Anything still unparseable is reported by the caller
    instead of being silently discarded.
    """
    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        # pandas < 2 has no "mixed" format — fall back to the classic path.
        return pd.to_datetime(series, errors="coerce")


def run_sync_cycle(token: str, repo_names=None, data_dir="./data", output_mode="monthly", export_json=None, export_public_only=True, metrics: list = None, public_only: bool = False):
    """Run a single sync cycle and return the number of traffic rows fetched.

    A cycle that fetched zero rows returns ``0`` so the caller can fail
    loudly instead of reporting a silent success (AF2-LIB-04). The CLI maps
    that to exit code 1 with a clean message.
    """
    df = fetch_traffic_data(token, repo_names, metrics, public_only=public_only)

    if df.empty:
        if export_json:
            export_json_database(data_dir, export_json, export_public_only)
        logger.warning("No traffic data found to sync.")
        return 0

    total_new = 0

    if output_mode == "monthly" and "date" in df.columns:
        # Group rows by their actual data month so June data never lands in a July file.
        parsed = _parse_date_column(df["date"])
        df["_month"] = parsed.dt.strftime("%Y-%m")
        # Rows with unparseable dates would be dropped silently by
        # groupby(dropna=True), which can delete whole repositories from the
        # archive. Surface them as a hard error instead (AF2-LIB-08).
        bad_mask = df["_month"].isna()
        bad_count = int(bad_mask.sum())
        if bad_count:
            samples = df.loc[bad_mask, "date"].astype(str).unique()[:5].tolist()
            raise ValueError(
                f"Cannot sync: {bad_count} row(s) have unparseable date values "
                f"(e.g. {samples}). Fix the source data - no rows were written."
            )
        for month_key, group in df.groupby("_month"):
            group = group.drop(columns=["_month"])
            try:
                ref_dt = datetime.strptime(month_key, "%Y-%m").replace(tzinfo=timezone.utc)
            except ValueError:
                ref_dt = datetime.now(timezone.utc)
            csv_path = get_csv_path(data_dir, output_mode, ref_dt)
            added = _write_df_to_csv(group, csv_path)
            total_new += added
            logger.info(f"Wrote {added} new records for {month_key} → {csv_path}")
    elif output_mode == "yearly" and "date" in df.columns:
        # Group rows by their actual data year so December data never lands in a January file across year boundary.
        parsed = _parse_date_column(df["date"])
        df["_year"] = parsed.dt.strftime("%Y")
        bad_mask = df["_year"].isna()
        bad_count = int(bad_mask.sum())
        if bad_count:
            samples = df.loc[bad_mask, "date"].astype(str).unique()[:5].tolist()
            raise ValueError(
                f"Cannot sync: {bad_count} row(s) have unparseable date values "
                f"(e.g. {samples}). Fix the source data - no rows were written."
            )
        for year_key, group in df.groupby("_year"):
            group = group.drop(columns=["_year"])
            try:
                ref_dt = datetime.strptime(year_key, "%Y").replace(tzinfo=timezone.utc)
            except ValueError:
                ref_dt = datetime.now(timezone.utc)
            csv_path = get_csv_path(data_dir, output_mode, ref_dt)
            added = _write_df_to_csv(group, csv_path)
            total_new += added
            logger.info(f"Wrote {added} new records for {year_key} → {csv_path}")
    else:
        # No date column — fall back to a single file keyed by run date.
        csv_path = get_csv_path(data_dir, output_mode)
        total_new = _write_df_to_csv(df, csv_path)
        logger.info(f"Successfully processed traffic data. Added {total_new} new daily records to {csv_path}")

    if export_json:
        export_json_database(data_dir, export_json, export_public_only)

    logger.info(f"Sync complete. Total new records: {total_new}")
    return int(len(df))


def _interruptible_sleep(total_seconds: float) -> None:
    # Sleep in short chunks anchored to a monotonic clock to avoid drift.
    chunk = 1.0
    deadline = time.monotonic() + max(0.0, float(total_seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(chunk, remaining))


def run_sync(token: str, repo_names=None, data_dir="./data", output_mode="monthly", schedule_cron=None, export_json=None, export_public_only=True, metrics: list = None, public_only: bool = False):
    # No cron expression = run once and exit
    if not schedule_cron:
        # The row count lets the caller detect an empty sync cycle, which is
        # a failure and never a silent success (AF2-LIB-04).
        rows = run_sync_cycle(token, repo_names, data_dir, output_mode, export_json, export_public_only, metrics, public_only=public_only)
        if rows == 0:
            raise NoTrafficDataError(
                "No traffic data was returned by GitHub - nothing was synced."
            )
        return rows

    logger.info("Starting Background Cron Job...")
    # Parse the cron expression — fail early if the format is wrong
    try:
        croniter(schedule_cron, datetime.now(timezone.utc))  # validate only
    except (ValueError, KeyError, TypeError) as e:
        logger.error(f"Invalid cron expression: {e}")
        raise ValueError(f"Invalid cron expression {schedule_cron!r}: {e}") from e

    # Infinite loop — keeps running until the process is killed or the token dies
    while True:
        now_utc = datetime.now(timezone.utc)
        # M-2+L-3: re-anchor each iteration to prevent clock-jump skips; rename from 'iter'
        cron_iter = croniter(schedule_cron, now_utc)
        next_run = cron_iter.get_next(datetime)

        # croniter may return naive datetimes on older versions — make it timezone-aware
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)

        sleep_secs = (next_run - now_utc).total_seconds()

        # Guard against negative/zero sleep (clock skew): minimum 1s to avoid hot-looping.
        if sleep_secs <= 0:
            sleep_secs = 1.0

        logger.info(f"Scheduled next sync for {next_run.strftime('%Y-%m-%d %H:%M:%S UTC')}. Sleeping {sleep_secs:.0f}s...")
        _interruptible_sleep(sleep_secs)

        try:
            # Always re-validate the token before fetching to catch expiry early
            is_valid, msg = validate_token(token)
            if not is_valid:
                msg_lower = msg.lower()
                if "rate limit" in msg_lower or "429" in msg_lower:
                    logger.warning(f"GitHub rate limit reached: {msg}. Retrying next cycle.")
                    continue
                # Distinguish between a dead token (stop forever) and a network
                # blip (retry next cycle). Both 401 and 403 are terminal —
                # 401 means the token is gone; 403 means the token is fine but
                # lacks permissions, which is also a "never coming back" state.
                is_auth_failure = (
                    "401" in msg
                    or "403" in msg
                    or "authentication failed" in msg_lower
                    or "invalid token" in msg_lower
                    or "revoked" in msg_lower
                    or "bad credentials" in msg_lower
                    or "insufficient permissions" in msg_lower
                )
                if is_auth_failure:
                    logger.critical(
                        "FATAL: Token is expired, revoked, or lacks permissions "
                        "(401/403). Stopping the daemon to prevent a zombie process."
                    )
                    raise TokenExpiredError(
                        "GitHub token is expired, revoked, or lacks the required permissions"
                    )
                else:
                    logger.warning(f"Network drop or temporary error: {msg}. Retrying next cycle.")
                    continue

            rows = run_sync_cycle(token, repo_names, data_dir, output_mode, export_json, export_public_only, metrics, public_only=public_only)
            if rows == 0:
                logger.error(
                    "No traffic data was returned by GitHub - nothing was synced. "
                    "Retrying next cycle."
                )
        except TokenExpiredError:
            raise
        except NoTrafficDataError as e:
            # An empty cycle is a real failure (AF2-LIB-04) but it is not
            # fatal for the daemon — log it loudly and try again next cycle.
            logger.error(f"{e} Retrying next cycle.")
            continue
        except GitHubRateLimitError as e:
            # Rate limits are transient: keep the daemon alive and try again
            # on the next scheduled cycle instead of logging an "unexpected error".
            logger.warning(f"GitHub rate limit reached: {e}. Retrying next cycle.")
            continue
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user. Exiting.")
            raise
        except Exception as e:
            # Don't let a single bad cycle kill the daemon — just log and carry on
            logger.error(f"Daemon encountered unexpected error: {e}. Recovering for next cycle.")
