"""
gitlytics/__init__.py
The public API for the gitlytics package.
"""
import os
import logging
import json
from pathlib import Path

# Single source of truth for the package version.
# Mirrors the version in pyproject.toml — keep them in sync.
__version__ = "0.7.0"

__all__ = [
    "fetch_traffic",
    "fetch_star_history",
    "sync",
    "serve_dashboard",
    "__version__",
    "GitHubRateLimitError",
    "StarHistoryFetchError",
    "StargazersRestrictedError",
]

# Import the internal building blocks — users never call these directly
from .core import (
    fetch_traffic_data,
    fetch_star_history,
    print_repo_table,
    GitHubRateLimitError,
    StarHistoryFetchError,
    StargazersRestrictedError,
)
from .automation import run_sync
from .process import build_json_payload

# Set up a silent logger so gitlytics never messes with your app's logging
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


_VALID_METRICS = {
    "views", "clones", "referrers", "paths", "stars", "forks",
    "language", "topics", "watchers_count", "pushed_at",
    "created_at", "open_issues_count", "releases",
}


def _coerce_metrics(metrics):
    # Accept list, tuple, or set. Reject strings, ints, dicts, etc.
    if metrics is None:
        return None
    if isinstance(metrics, str):
        raise ValueError(
            "metrics must be a list/tuple/set of strings, not a single string. "
            "Pass ['views', 'clones'] instead of 'views clones'."
        )
    if isinstance(metrics, (list, tuple, set, frozenset)):
        result = list(metrics)
        for m in result:
            if not isinstance(m, str):
                raise ValueError(f"metrics entries must be strings; got {type(m).__name__}.")
        # Drop unknown metric names with a warning — better than crashing mid-fetch.
        unknown = [m for m in result if m not in _VALID_METRICS]
        if unknown:
            logger.warning(f"Unknown metrics ignored: {unknown}")
            result = [m for m in result if m in _VALID_METRICS]
        return result
    raise ValueError(f"metrics must be a list/tuple/set; got {type(metrics).__name__}.")


def _write_json_safely(path: str, payload: dict) -> None:
    # Create the parent dir so the user can pass any nested path.
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def fetch_traffic(token: str, repo_name=None, print_table: bool = False, return_format: str = "dataframe", save_file: str = None, metrics: list = None):
    """
    Fetches the last 14 days of traffic data for one or all repositories.

    Args:
        token: GitHub Personal Access Token with `repo` scope.
        repo_name: Specific repo name (e.g. ``"user/repo"``) or list of names.
            If ``None``, fetches all repositories accessible by the token.
        print_table: If ``True``, prints an ASCII summary table to stdout.
        return_format: Shape of the returned data. One of:
            ``"dataframe"`` (default) — returns a ``pandas.DataFrame``.
            ``"timeseries"`` — returns a nested JSON-serialisable dict.
            ``"summary"`` — returns a per-repo totals dict.
        save_file: Optional path to save the output. Extension determines
            format: ``.json`` writes JSON, anything else writes CSV.
        metrics: Optional list of metrics to fetch (e.g., ``["views", "clones"]``).

    Returns:
        A ``pandas.DataFrame`` when ``return_format="dataframe"``, otherwise
        a ``dict`` matching the requested format.
    """
    # Strip surrounding whitespace from the token (matches api._get_token).
    token = (token or "").strip() if isinstance(token, str) else token
    metrics = _coerce_metrics(metrics)

    # Hit the GitHub API and get back a tidy DataFrame (one row per day per repo)
    df = fetch_traffic_data(token, repo_name, metrics)

    # Print the ASCII table to the console if the user asked for it
    if print_table:
        print_repo_table(df)

    # --- dataframe mode: just return the raw DataFrame, optionally save it ---
    if return_format == "dataframe":
        if save_file:
            # When persisting to disk, strip private repos by default (security firewall).
            export_df = df
            if "is_private" in df.columns:
                export_df = df[~df["is_private"]]
            if save_file.endswith(".json"):
                # Save as a chart-ready JSON file (always public-only when exported to disk)
                payload = build_json_payload(export_df, return_format="timeseries", export_public_only=True)
                _write_json_safely(save_file, payload)
            else:
                # Save as a standard CSV file
                p = Path(save_file)
                p.parent.mkdir(parents=True, exist_ok=True)
                export_df.to_csv(p, index=False)
        return df

    # Reject anything that isn't a known format before doing more work
    valid_formats = {"timeseries", "summary"}
    if return_format not in valid_formats:
        raise ValueError(
            f"Invalid return_format={return_format!r}. "
            f"Choose one of: 'dataframe', 'timeseries', 'summary'."
        )

    # Build the JSON-serialisable payload in the requested shape.
    # When persisting to disk, strip private repos by default (security firewall).
    export_public_only = bool(save_file)
    payload = build_json_payload(df, return_format=return_format, export_public_only=export_public_only)

    # Save to disk if the user gave us a file path
    if save_file:
        _write_json_safely(save_file, payload)

    return payload


def sync(token: str, repo_name=None, data_dir: str = "./data", output_mode: str = "monthly", schedule_cron: str = None, export_json: str = None, export_public_only: bool = True, metrics: list = None):
    """
    Fetches data and appends it to a local CSV database, optionally running as a permanent background daemon.

    Args:
        token: GitHub Personal Access Token.
        repo_name: Specific repository name(s) to sync. If ``None``, syncs all.
        data_dir: Directory where CSV files are stored.
        output_mode: ``"monthly"`` (``traffic_YYYY-MM.csv``) or ``"yearly"`` (``traffic_YYYY.csv``).
        schedule_cron: Standard cron expression (e.g. ``"0 23 * * *"``). If set,
            runs an infinite scheduler loop.
        export_json: Path to export the merged historical database as a JSON file.
        export_public_only: If ``True`` (default), strips private repos from the
            exported JSON — acts as a security firewall.
        metrics: Optional list of metrics to fetch (e.g., ``["views", "clones"]``).
    """
    # Strip the token. Resolve the data_dir to an absolute path but NEVER redirect
    # to a sibling directory — respect the user's CWD.
    token = (token or "").strip() if isinstance(token, str) else token
    metrics = _coerce_metrics(metrics)
    if data_dir:
        data_dir = str(Path(data_dir).expanduser().resolve())

    # Hand off to the automation engine — it handles deduplication and schema migration
    run_sync(
        token=token,
        repo_names=repo_name,
        data_dir=data_dir,
        output_mode=output_mode,
        schedule_cron=schedule_cron,
        export_json=export_json,
        export_public_only=export_public_only,
        metrics=metrics
    )


def serve_dashboard(host: str = "127.0.0.1", port: int = 8000, token: str = None, data_dir: str = None):
    """
    Starts the React + FastAPI dashboard server.

    ``uvicorn`` and ``fastapi`` are optional dependencies installed via::

        pip install "gitlytics[dashboard]"

    Args:
        host: Host IP to bind. Use ``"0.0.0.0"`` to listen on all interfaces.
        port: Port number (default ``8000``).
        token: Optional GitHub token — pre-authenticates the dashboard session.
        data_dir: Optional path to the historical CSV database directory.
    """
    # Only import uvicorn when the user actually calls serve_dashboard,
    # so the base `pip install gitlytics` doesn't crash without it
    try:
        import uvicorn
    except ImportError:
        raise ImportError(
            "The dashboard requires additional dependencies. "
            "Install them with: pip install \"gitlytics[dashboard]\""
        )

    # M-7: save original values so they are restored when the server stops
    _orig_token = os.environ.get("GITLYTICS_TOKEN")
    _orig_data_dir = os.environ.get("GITLYTICS_DATA_DIR")
    try:
        if token:
            os.environ["GITLYTICS_TOKEN"] = (token or "").strip()
        if data_dir:
            abs_data_dir = str(Path(data_dir).expanduser().resolve())
            p = Path(abs_data_dir)
            if not p.exists():
                print(f"⚠️ Warning: The specified data directory '{data_dir}' (resolved to '{abs_data_dir}') does not exist.")
            elif not any(p.glob("traffic_*.csv")):
                print(f"⚠️ Warning: No traffic_*.csv database files found in '{data_dir}' (resolved to '{abs_data_dir}').")
            os.environ["GITLYTICS_DATA_DIR"] = abs_data_dir
        uvicorn.run("gitlytics.api:app", host=host, port=port, reload=False, log_level="warning")
    finally:
        if _orig_token is None:
            os.environ.pop("GITLYTICS_TOKEN", None)
        else:
            os.environ["GITLYTICS_TOKEN"] = _orig_token
        if _orig_data_dir is None:
            os.environ.pop("GITLYTICS_DATA_DIR", None)
        else:
            os.environ["GITLYTICS_DATA_DIR"] = _orig_data_dir
