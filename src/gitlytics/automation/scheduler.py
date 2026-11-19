# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""One-shot sync execution and continuous cron daemon loop."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from croniter import croniter

from gitlytics.core.traffic import fetch_traffic_data
from gitlytics.core.users import validate_token
from gitlytics.exceptions import (
    GitHubRateLimitError,
    NoTrafficDataError,
    TokenExpiredError,
)
from gitlytics.io.csv import _parse_date_column

from .storage import _write_df_to_csv, export_json_database, get_csv_path

logger = logging.getLogger(__name__)


def run_sync_cycle(
    token: str,
    repo_names=None,
    data_dir: str = "./data",
    output_mode: str = "monthly",
    export_json: str = None,
    export_public_only: bool = True,
    metrics: list = None,
    public_only: bool = False,
) -> int:
    """Fetch traffic once, merge it into archive CSVs, and return rows fetched."""
    df = fetch_traffic_data(token, repo_names, metrics, public_only=public_only)

    if df.empty:
        if export_json:
            export_json_database(data_dir, export_json, export_public_only)
        logger.warning("No traffic data found to sync.")
        return 0

    total_new = 0

    if output_mode == "monthly" and "date" in df.columns:
        # Partition rows by their actual data month so cross-month snapshots split cleanly.
        parsed = _parse_date_column(df["date"])
        df["_month"] = parsed.dt.strftime("%Y-%m")
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
                ref_dt = datetime.strptime(month_key, "%Y-%m").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                ref_dt = datetime.now(timezone.utc)
            csv_path = get_csv_path(data_dir, output_mode, ref_dt)
            added = _write_df_to_csv(group, csv_path)
            total_new += added
            logger.info(f"Wrote {added} new records for {month_key} → {csv_path}")
    elif output_mode == "yearly" and "date" in df.columns:
        # Partition rows by their actual data year across year boundaries.
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
        csv_path = get_csv_path(data_dir, output_mode)
        total_new = _write_df_to_csv(df, csv_path)
        logger.info(
            f"Successfully processed traffic data. Added {total_new} new daily records to {csv_path}"
        )

    if export_json:
        export_json_database(data_dir, export_json, export_public_only)

    logger.info(f"Sync complete. Total new records: {total_new}")
    return int(len(df))


def _interruptible_sleep(total_seconds: float) -> None:
    """Sleep in 1-second monotonic chunks so Ctrl-C interrupts cleanly without drift."""
    chunk = 1.0
    deadline = time.monotonic() + max(0.0, float(total_seconds))
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(chunk, remaining))


def run_sync(
    token: str,
    repo_names=None,
    data_dir: str = "./data",
    output_mode: str = "monthly",
    schedule_cron: str = None,
    export_json: str = None,
    export_public_only: bool = True,
    metrics: list = None,
    public_only: bool = False,
):
    """Run one sync cycle or stay alive on a cron schedule until interrupted."""
    if not schedule_cron:
        rows = run_sync_cycle(
            token,
            repo_names,
            data_dir,
            output_mode,
            export_json,
            export_public_only,
            metrics,
            public_only=public_only,
        )
        if rows == 0:
            raise NoTrafficDataError(
                "No traffic data was returned by GitHub - nothing was synced."
            )
        return rows

    logger.info("Starting Background Cron Job...")
    try:
        croniter(schedule_cron, datetime.now(timezone.utc))
    except (ValueError, KeyError, TypeError) as exc:
        logger.error(f"Invalid cron expression: {exc}")
        raise ValueError(f"Invalid cron expression {schedule_cron!r}: {exc}") from exc

    while True:
        now_utc = datetime.now(timezone.utc)
        cron_iter = croniter(schedule_cron, now_utc)
        next_run = cron_iter.get_next(datetime)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)

        sleep_secs = max(1.0, (next_run - now_utc).total_seconds())
        logger.info(
            f"Scheduled next sync for {next_run.strftime('%Y-%m-%d %H:%M:%S UTC')}. Sleeping {sleep_secs:.0f}s..."
        )
        _interruptible_sleep(sleep_secs)

        try:
            # Re-validate the token before each run so expired tokens stop the daemon cleanly.
            is_valid, msg = validate_token(token)
            if not is_valid:
                msg_lower = msg.lower()
                if "rate limit" in msg_lower or "429" in msg_lower:
                    logger.warning(
                        f"GitHub rate limit reached: {msg}. Retrying next cycle."
                    )
                    continue
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
                logger.warning(
                    f"Network drop or temporary error: {msg}. Retrying next cycle."
                )
                continue

            rows = run_sync_cycle(
                token,
                repo_names,
                data_dir,
                output_mode,
                export_json,
                export_public_only,
                metrics,
                public_only=public_only,
            )
            if rows == 0:
                logger.error(
                    "No traffic data was returned by GitHub - nothing was synced. "
                    "Retrying next cycle."
                )
        except TokenExpiredError:
            raise
        except NoTrafficDataError as exc:
            logger.error(f"{exc} Retrying next cycle.")
            continue
        except GitHubRateLimitError as exc:
            logger.warning(f"GitHub rate limit reached: {exc}. Retrying next cycle.")
            continue
        except KeyboardInterrupt:
            logger.info("Daemon interrupted by user. Exiting.")
            raise
        except Exception as exc:
            logger.error(
                f"Daemon encountered unexpected error: {exc}. Recovering for next cycle."
            )
