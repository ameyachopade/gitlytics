# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""High-level public SDK entry points: fetch_traffic, sync, and serve_dashboard."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from gitlytics.automation.scheduler import run_sync
from gitlytics.core.traffic import fetch_traffic_data
from gitlytics.io.atomic import _write_csv_safely, _write_json_safely
from gitlytics.io.formatting import print_repo_table
from gitlytics.io.payloads import _filter_public_only_df, build_json_payload

logger = logging.getLogger(__name__)

_VALID_METRICS = {
    "views", "clones", "referrers", "paths", "stars", "forks",
    "language", "topics", "watchers_count", "pushed_at",
    "created_at", "open_issues_count", "releases",
}


def _coerce_metrics(metrics):
    """Validate and normalize the optional metrics sequence, dropping unknown names."""
    if metrics is None:
        return None
    if isinstance(metrics, str):
        raise ValueError(
            "metrics must be a list/tuple/set of strings, not a single string. "
            "Pass ['views', 'clones'] instead of 'views clones'."
        )
    if isinstance(metrics, (list, tuple, set, frozenset)):
        result = list(metrics)
        for item in result:
            if not isinstance(item, str):
                raise ValueError(f"metrics entries must be strings; got {type(item).__name__}.")
        unknown = [item for item in result if item not in _VALID_METRICS]
        if unknown:
            logger.warning(f"Unknown metric(s) ignored: {unknown}. Valid options: {sorted(_VALID_METRICS)}")
            result = [item for item in result if item in _VALID_METRICS]
            if not result:
                raise ValueError(
                    f"No valid metrics remain after ignoring unknown entries {unknown}. "
                    f"Valid metrics: {sorted(_VALID_METRICS)}"
                )
        return result
    raise ValueError(f"metrics must be a list/tuple/set; got {type(metrics).__name__}.")


def fetch_traffic(
    token: str,
    repo_name=None,
    print_table: bool = False,
    return_format: str = "dataframe",
    save_file: str = None,
    metrics: list = None,
    export_public_only: bool = True,
    allow_empty_save: bool = False,
):
    """
    Fetch the last 14 completed days (plus today) of GitHub traffic data.

    Args:
        token: GitHub Personal Access Token with ``repo`` scope.
        repo_name: Single ``"owner/repo"`` string, list of names, or ``None`` for all repos.
        print_table: If ``True``, print an ASCII summary table to stdout.
        return_format: ``"dataframe"``, ``"timeseries"``, or ``"summary"``.
        save_file: Optional output path (``.json`` writes JSON, anything else writes CSV).
        metrics: Optional list of metric names to include.
        export_public_only: Strip private repositories from saved files and JSON payloads.
        allow_empty_save: Allow overwriting an existing ``save_file`` when zero rows return.
    """
    token = (token or "").strip() if isinstance(token, str) else token
    metrics = _coerce_metrics(metrics)

    df = fetch_traffic_data(token, repo_name, metrics)

    if print_table:
        print_repo_table(df)

    if return_format == "dataframe":
        if save_file:
            export_df = _filter_public_only_df(df) if export_public_only else df
            # Refuse to overwrite an existing export with empty data unless opted in.
            if export_df.empty and not allow_empty_save:
                if Path(save_file).exists():
                    raise ValueError(
                        f"Refusing to overwrite existing file {str(save_file)!r} with an "
                        "empty result. Pass allow_empty_save=True to permit this."
                    )
            else:
                if Path(save_file).suffix.lower() == ".json":
                    payload = build_json_payload(
                        export_df, return_format="timeseries", export_public_only=export_public_only
                    )
                    _write_json_safely(str(save_file), payload)
                else:
                    _write_csv_safely(str(save_file), export_df)
        return df

    if return_format not in {"timeseries", "summary"}:
        raise ValueError(
            f"Invalid return_format={return_format!r}. "
            "Choose one of: 'dataframe', 'timeseries', 'summary'."
        )

    payload = build_json_payload(df, return_format=return_format, export_public_only=export_public_only)

    if save_file:
        if not payload.get("repositories") and not allow_empty_save:
            if Path(save_file).exists():
                raise ValueError(
                    f"Refusing to overwrite existing file {str(save_file)!r} with an "
                    "empty result. Pass allow_empty_save=True to permit this."
                )
        else:
            _write_json_safely(str(save_file), payload)

    return payload


def sync(
    token: str,
    repo_name=None,
    data_dir: str = "./data",
    output_mode: str = "monthly",
    schedule_cron: str = None,
    export_json: str = None,
    export_public_only: bool = True,
    metrics: list = None,
    public_only: bool = False,
):
    """
    Fetch GitHub traffic and merge it into a local CSV archive (or run on a cron schedule).

    Args:
        token: GitHub Personal Access Token.
        repo_name: Specific ``"owner/repo"`` name(s), or ``None`` for all accessible repos.
        data_dir: Directory where monthly or yearly CSV archives live.
        output_mode: ``"monthly"`` (``traffic_YYYY-MM.csv``) or ``"yearly"`` (``traffic_YYYY.csv``).
        schedule_cron: Optional 5-field cron expression to run as a continuous daemon.
        export_json: Optional path for a merged React-compatible JSON export.
        export_public_only: Strip private repositories from the JSON export.
        metrics: Optional subset of metric names to fetch.
        public_only: Sync only public repos when ``repo_name`` is ``None``.
    """
    token = (token or "").strip() if isinstance(token, str) else token
    metrics = _coerce_metrics(metrics)
    if data_dir is None or (isinstance(data_dir, str) and not data_dir.strip()):
        data_dir = "./data"
    data_dir = str(Path(data_dir).expanduser().resolve())

    return run_sync(
        token=token,
        repo_names=repo_name,
        data_dir=data_dir,
        output_mode=output_mode,
        schedule_cron=schedule_cron,
        export_json=export_json,
        export_public_only=export_public_only,
        metrics=metrics,
        public_only=public_only,
    )


def serve_dashboard(
    host: str = "127.0.0.1",
    port: int = 8000,
    token: str = None,
    data_dir: str = None,
    stream=None,
):
    """
    Start the local React + FastAPI dashboard server (requires ``gitlytics[dashboard]``).

    Args:
        host: Host IP to bind (defaults to loopback ``127.0.0.1``).
        port: Port number (default ``8000``).
        token: Optional GitHub token to pre-authenticate a loopback session.
        data_dir: Optional directory containing archived ``traffic_*.csv`` files.
        stream: Output stream for startup warnings (defaults to ``sys.stdout``).
    """
    # Import uvicorn lazily so the base install never requires dashboard extras.
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "The dashboard requires additional dependencies. "
            'Install them with: pip install "gitlytics[dashboard]"'
        )

    if stream is None:
        stream = sys.stdout

    def _warn(message: str) -> None:
        print(message, file=stream)

    # Snapshot environment vars so they are always restored when the server stops.
    orig_token = os.environ.get("GITLYTICS_TOKEN")
    orig_data_dir = os.environ.get("GITLYTICS_DATA_DIR")
    orig_allow_env_token = os.environ.get("GITLYTICS_ALLOW_ENV_TOKEN")
    try:
        loopback = host in ("127.0.0.1", "::1", "localhost")
        if token and loopback:
            os.environ["GITLYTICS_TOKEN"] = (token or "").strip()
            os.environ["GITLYTICS_ALLOW_ENV_TOKEN"] = "1"
        elif loopback:
            existing = (os.environ.get("GITLYTICS_TOKEN") or "").strip()
            if existing:
                os.environ["GITLYTICS_ALLOW_ENV_TOKEN"] = "1"
                _warn(
                    "⚠️  Warning: using the existing GITLYTICS_TOKEN from the "
                    "environment for this local dashboard."
                )
            else:
                os.environ.pop("GITLYTICS_TOKEN", None)
                os.environ.pop("GITLYTICS_ALLOW_ENV_TOKEN", None)
        else:
            dropped_token = (os.environ.get("GITLYTICS_TOKEN") or "").strip()
            os.environ.pop("GITLYTICS_TOKEN", None)
            os.environ.pop("GITLYTICS_ALLOW_ENV_TOKEN", None)
            if token and not loopback:
                _warn(
                    "⚠️  Warning: --token is disabled on non-loopback binds; "
                    "users must authenticate with their own GitHub token."
                )
            if dropped_token:
                _warn(
                    "⚠️  Warning: the existing GITLYTICS_TOKEN was dropped because "
                    "the dashboard is not bound to loopback."
                )
        if not loopback:
            _warn(
                "⚠️  Warning: the dashboard has no built-in authentication. "
                "Use it only behind a trusted reverse proxy or private network."
            )
        if data_dir:
            abs_data_dir = str(Path(data_dir).expanduser().resolve())
            path = Path(abs_data_dir)
            if not path.exists():
                _warn(f"⚠️ Warning: The specified data directory '{data_dir}' (resolved to '{abs_data_dir}') does not exist.")
            elif not any(path.glob("traffic_*.csv")):
                _warn(f"⚠️ Warning: No traffic_*.csv database files found in '{data_dir}' (resolved to '{abs_data_dir}').")
            os.environ["GITLYTICS_DATA_DIR"] = abs_data_dir

        uvicorn.run("gitlytics.api:app", host=host, port=port, reload=False, log_level="warning")
    finally:
        if orig_token is None:
            os.environ.pop("GITLYTICS_TOKEN", None)
        else:
            os.environ["GITLYTICS_TOKEN"] = orig_token
        if orig_allow_env_token is None:
            os.environ.pop("GITLYTICS_ALLOW_ENV_TOKEN", None)
        else:
            os.environ["GITLYTICS_ALLOW_ENV_TOKEN"] = orig_allow_env_token
        if orig_data_dir is None:
            os.environ.pop("GITLYTICS_DATA_DIR", None)
        else:
            os.environ["GITLYTICS_DATA_DIR"] = orig_data_dir
