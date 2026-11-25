# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Zero-dependency local MCP 2025-03-26 stdio server (`gitlytics mcp`).

Security invariants:
- No tool schema exposes a `token` or credential argument so secrets never
  enter LLM chat transcripts. GitHub tokens are read exclusively from
  GITLYTICS_TOKEN or GITHUB_TOKEN environment variables.
- All logging and accidental prints during tool execution are routed to
  sys.stderr so sys.stdout carries only newline-delimited JSON-RPC 2.0 frames.
- CSV vault queries read only `traffic_*.csv` files directly inside the
  configured `data_dir`.
"""
from __future__ import annotations

import contextlib
import html
import importlib
import json
import logging
import os
import re
import sys
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TextIO
from urllib.parse import urlencode, urlparse

from gitlytics._version import __version__
from gitlytics.io.rows import is_nullish, read_csv_rows, safe_int

logger = logging.getLogger(__name__)

_LAZY_MCP_DEPS: dict[str, tuple[str, str]] = {
    "fetch_traffic": ("gitlytics.sdk", "fetch_traffic"),
    "sync": ("gitlytics.sdk", "sync"),
    "fetch_star_history": ("gitlytics.core.stars", "fetch_star_history"),
    "validate_owner_repo": ("gitlytics.core.validation", "validate_owner_repo"),
}

# Bounded mtime/size-keyed cache for local CSV vault reads (zero pandas).
_VAULT_CSV_CACHE_MAX = 128
_VAULT_CSV_CACHE: "OrderedDict[str, tuple[int, int, list[str], list[dict[str, Any]]]]" = (
    OrderedDict()
)
_VAULT_CSV_CACHE_LOCK = threading.Lock()


def __getattr__(name: str):
    """Lazily resolve SDK/core helpers so `gitlytics mcp` never eagerly imports pandas."""
    target = _LAZY_MCP_DEPS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    return getattr(importlib.import_module(mod_name), attr_name)


def _resolve_dep(name: str):
    """Return a monkeypatched callable from module globals if present, else import lazily."""
    patched = globals().get(name)
    if patched is not None:
        return patched
    mod_name, attr_name = _LAZY_MCP_DEPS[name]
    return getattr(importlib.import_module(mod_name), attr_name)


def _read_vault_csv_cached(entry: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a CSV vault archive via pure-Python rows, caching by (path, mtime_ns, size)."""
    try:
        st = entry.stat()
    except OSError:
        return read_csv_rows(entry)

    key = str(entry.resolve())
    with _VAULT_CSV_CACHE_LOCK:
        cached = _VAULT_CSV_CACHE.get(key)
        if cached is not None:
            c_ns, c_size, c_cols, c_rows = cached
            if c_ns == st.st_mtime_ns and c_size == st.st_size:
                _VAULT_CSV_CACHE.move_to_end(key)
                return c_cols, c_rows

    cols, rows = read_csv_rows(entry)
    with _VAULT_CSV_CACHE_LOCK:
        _VAULT_CSV_CACHE[key] = (st.st_mtime_ns, st.st_size, cols, rows)
        _VAULT_CSV_CACHE.move_to_end(key)
        while len(_VAULT_CSV_CACHE) > _VAULT_CSV_CACHE_MAX:
            _VAULT_CSV_CACHE.popitem(last=False)
    return cols, rows

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"
_UNSAFE_TEXT_RE = re.compile(r"[<>`\"']|javascript:|data:", re.IGNORECASE)
_GITHUB_USER_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$")
_PYPI_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_NPM_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,127}$")
_SKILLS_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}$")
_HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{3,8}$")
_NAMED_COLOR_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,23}$")
_CSV_FILE_RE = re.compile(r"^traffic_(\d{4})(?:-(\d{2}))?\.csv$")
_VALID_KINDS = frozenset(
    {
        "card",
        "tech_badge",
        "static_badge",
        "live_badge",
        "pypi_badge",
        "npm_badge",
        "skills_badge",
        "logo",
        "chart",
        "star_history",
    }
)
_VALID_GRANULARITIES = ("daily", "weekly", "summary")
_VALID_OUTPUT_FORMATS = frozenset({"all", "markdown", "html", "picture", "url"})
_VALID_BATCH_LAYOUTS = frozenset({"none", "inline_markdown", "centered_html"})
_VALID_BATCH_ERROR_MODES = frozenset({"partial", "fail_fast"})
MAX_LOCAL_WIDGET_BATCH_SIZE = 30
_LOCAL_BATCH_DEFAULT_KEYS = frozenset(
    {
        "username",
        "owner",
        "repo",
        "theme",
        "dark_theme",
        "light_theme",
        "variant",
        "shape",
        "style",
        "color",
        "label_color",
        "value_color",
        "hide_border",
    }
)
_LOCAL_EMBED_PARAM_KEYS = frozenset(
    {
        "kind",
        "username",
        "card_type",
        "owner",
        "repo",
        "metric",
        "package_name",
        "skills_path",
        "slug",
        "label",
        "message",
        "value",
        "theme",
        "dark_theme",
        "light_theme",
        "variant",
        "shape",
        "style",
        "color",
        "label_color",
        "value_color",
        "hide_border",
        "alt_text",
        "href",
        "format",
    }
)


def _resolve_env_token() -> Optional[str]:
    raw = os.environ.get("GITLYTICS_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    cleaned = raw.strip()
    return cleaned or None


def _require_env_token() -> str:
    token = _resolve_env_token()
    if not token:
        raise ValueError(
            "No GitHub token found in environment. Set GITLYTICS_TOKEN or GITHUB_TOKEN "
            "in your MCP client env config (never paste tokens into chat)."
        )
    return token


def _validate_safe_text(field: str, value: Optional[str], max_len: int = 64) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in cleaned):
        raise ValueError(f"{field} must not contain control characters.")
    if _UNSAFE_TEXT_RE.search(cleaned):
        raise ValueError(f"{field} contains disallowed characters.")
    return cleaned


def _validate_href(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if len(cleaned) > 512:
        raise ValueError("href must be at most 512 characters.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in cleaned):
        raise ValueError("href must not contain whitespace or control characters.")
    if _UNSAFE_TEXT_RE.search(cleaned):
        raise ValueError("href contains disallowed characters.")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("href must be a valid http:// or https:// URL.")
    return cleaned


def _validate_color(field: str, value: Optional[str]) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip()
    if _HEX_COLOR_RE.match(cleaned):
        return cleaned.lstrip("#")
    if _NAMED_COLOR_RE.match(cleaned):
        return cleaned.lower()
    raise ValueError(f"Invalid {field} {value!r}.")


def _csv_archive_may_overlap_cutoff(filename: str, cutoff_date: str) -> bool:
    """Skip reading older monthly/yearly CSV archives whose entire period precedes cutoff."""
    m = _CSV_FILE_RE.match(filename)
    if not m:
        return False
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else None
    cutoff_year = int(cutoff_date[:4])
    if year < cutoff_year:
        return False
    if month is not None and year == cutoff_year:
        cutoff_month = int(cutoff_date[5:7])
        if month < cutoff_month:
            return False
    return True


def _aggregate_weekly_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Roll daily vault rows into ISO-week buckets to keep long windows compact."""
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_date = str(row.get("date") or "")[:10]
        try:
            iso = datetime.strptime(raw_date, "%Y-%m-%d").isocalendar()
            week_key = f"{iso.year}-W{iso.week:02d}"
        except ValueError:
            week_key = raw_date or "unknown"
        bucket = buckets.get(week_key)
        if bucket is None:
            bucket = {
                "week": week_key,
                "start_date": raw_date,
                "end_date": raw_date,
                "days": 0,
                "views": 0,
                "unique_visitors": 0,
                "clones": 0,
                "unique_cloners": 0,
            }
            buckets[week_key] = bucket
        bucket["end_date"] = raw_date
        bucket["days"] += 1
        bucket["views"] += safe_int(row.get("views"))
        bucket["unique_visitors"] += safe_int(row.get("unique_visitors"))
        bucket["clones"] += safe_int(row.get("clones"))
        bucket["unique_cloners"] += safe_int(row.get("unique_cloners"))
    return list(buckets.values())


_LOCAL_WIDGET_KINDS_ENUM = sorted(_VALID_KINDS)

_LOCAL_WIDGET_EMBED_PROPERTIES: dict[str, Any] = {
    "kind": {"type": "string", "enum": _LOCAL_WIDGET_KINDS_ENUM},
    "username": {"type": "string"},
    "card_type": {"type": "string"},
    "owner": {"type": "string"},
    "repo": {"type": "string"},
    "metric": {"type": "string", "enum": ["views", "clones", "stars"]},
    "package_name": {"type": "string"},
    "skills_path": {"type": "string"},
    "slug": {"type": "string"},
    "label": {"type": "string"},
    "message": {"type": "string"},
    "value": {"type": "string"},
    "theme": {"type": "string"},
    "dark_theme": {"type": "string"},
    "light_theme": {"type": "string"},
    "variant": {"type": "string"},
    "shape": {"type": "string"},
    "style": {"type": "string", "enum": ["icon", "icon_label", "icon_label_value"]},
    "color": {"type": "string"},
    "label_color": {"type": "string"},
    "value_color": {"type": "string"},
    "hide_border": {"type": "boolean"},
    "alt_text": {"type": "string"},
    "href": {"type": "string"},
    "format": {"type": "string", "enum": ["all", "markdown", "html", "picture", "url"]},
}

LOCAL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "local_vault_summary",
        "title": "Summarize Local CSV Traffic Vault",
        "description": (
            "Return a compact per-repository overview, date coverage, and file inventory "
            "for all traffic_*.csv archives inside data_dir (100% offline, zero raw row dump)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 90},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "local_query_csv_vault",
        "title": "Query Local CSV Traffic Vault",
        "description": (
            "Read historical GitHub traffic metrics from local traffic_*.csv archive files "
            "inside data_dir. Works 100% offline with no GitHub token. Use granularity='weekly' "
            "or 'summary' for long windows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "Optional 'owner/repo' filter."},
                "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 90},
                "granularity": {
                    "type": "string",
                    "enum": ["daily", "weekly", "summary"],
                    "default": "daily",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "local_fetch_traffic",
        "title": "Fetch Live 14-Day Traffic via Local SDK",
        "description": (
            "Fetch live 14-day GitHub traffic using GITLYTICS_TOKEN or GITHUB_TOKEN "
            "from the local environment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "Optional 'owner/repo' target."},
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional subset of metric names.",
                },
                "include_private": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "local_sync_archive",
        "title": "Sync Traffic Snapshot into Local CSV Vault",
        "description": (
            "Snapshot live 14-day GitHub traffic and merge into local traffic_*.csv archives "
            "using GITLYTICS_TOKEN or GITHUB_TOKEN from the local environment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_name": {"type": "string"},
                "output_mode": {"type": "string", "enum": ["monthly", "yearly"], "default": "monthly"},
                "public_only": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "local_star_history",
        "title": "Fetch Star History via Local SDK",
        "description": "Fetch cumulative star trajectory and next milestone projection for a GitHub repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repo": {"type": "string"},
            },
            "required": ["owner", "repo"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    },
    {
        "name": "local_build_widget_embed",
        "title": "Build Gitlytics Widget Embed URL",
        "description": (
            "Validate and build hosted Gitlytics SVG card, badge, logo, or chart embeds "
            "(supports dual light/dark <picture> and clickable href). For 2+ badges, prefer local_build_widget_batch."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _LOCAL_WIDGET_EMBED_PROPERTIES,
            "required": ["kind"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "local_build_widget_batch",
        "title": "Build Batch of Gitlytics Widget Embeds",
        "description": (
            "Validate and generate up to 30 Gitlytics badges, cards, logos, or charts in 1 call "
            "with shared defaults, layout assembly, and partial error recovery."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "defaults": {"type": "object", "additionalProperties": True},
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_LOCAL_WIDGET_BATCH_SIZE,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "kind": {"type": "string", "enum": _LOCAL_WIDGET_KINDS_ENUM},
                        },
                        "required": ["kind"],
                        "additionalProperties": True,
                    },
                },
                "format": {"type": "string", "enum": ["all", "markdown", "html", "picture", "url"], "default": "markdown"},
                "layout": {"type": "string", "enum": ["none", "inline_markdown", "centered_html"], "default": "none"},
                "on_error": {"type": "string", "enum": ["partial", "fail_fast"], "default": "partial"},
                "include_items": {"type": "boolean", "default": True},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]

LOCAL_TOOL_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in LOCAL_TOOL_DEFINITIONS}
LOCAL_TOOL_ALLOWED_ARGS: dict[str, frozenset[str]] = {
    t["name"]: frozenset(t["inputSchema"].get("properties", {}).keys()) for t in LOCAL_TOOL_DEFINITIONS
}

LOCAL_RESOURCE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "uri": "gitlytics://local/vault-summary",
        "name": "Local CSV Vault Summary",
        "description": "Offline summary of tracked repositories, date coverage, and 90-day traffic totals in data_dir.",
        "mimeType": "application/json",
    },
    {
        "uri": "gitlytics://local/files",
        "name": "Local CSV Archive Files",
        "description": "List of traffic_*.csv archive files inside data_dir with byte sizes.",
        "mimeType": "application/json",
    },
]

LOCAL_PROMPT_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "audit_local_vault",
        "title": "Audit Local CSV Traffic Vault",
        "description": "Inspect archived local CSV traffic trends, repo rankings, and coverage gaps without network access.",
        "arguments": [
            {"name": "days", "description": "Lookback window in days (default 90).", "required": False},
        ],
    },
    {
        "name": "create_badge_row",
        "title": "Create README Badge Row",
        "description": "Generate a cohesive row of Gitlytics badges in a single local_build_widget_batch call.",
        "arguments": [
            {"name": "owner", "description": "GitHub repository owner.", "required": True},
            {"name": "repo", "description": "GitHub repository name.", "required": True},
            {"name": "theme", "description": "Gitlytics theme ID (default 'gitlytics-dark').", "required": False},
        ],
    },
]


def _tool_result(
    payload: Any,
    *,
    is_error: bool = False,
    text_override: Optional[str] = None,
) -> dict[str, Any]:
    if text_override is not None:
        text = text_override
    elif isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, separators=(",", ":"), default=str)
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if isinstance(payload, dict):
        out["structuredContent"] = payload
    return out


class LocalMcpServer:
    """In-process JSON-RPC 2.0 dispatcher for local stdio `gitlytics mcp`."""

    def __init__(self, data_dir: str = "./data", api_base: str = "https://api.gitlytics.dev"):
        self.data_dir = Path(data_dir).expanduser().resolve()
        cleaned_base = (api_base or "https://api.gitlytics.dev").strip().rstrip("/")
        if not cleaned_base.startswith(("https://", "http://")):
            raise ValueError("api_base must start with https:// or http://")
        self.api_base = cleaned_base

    def _list_vault_files(self) -> list[dict[str, Any]]:
        if not self.data_dir.exists() or not self.data_dir.is_dir():
            return []
        files = []
        for entry in sorted(self.data_dir.iterdir()):
            if entry.is_file() and _CSV_FILE_RE.match(entry.name):
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                files.append({"name": entry.name, "size_bytes": size})
        return files

    def _query_csv_vault(
        self,
        repo_name: Optional[str] = None,
        days: int = 90,
        granularity: str = "daily",
    ) -> dict[str, Any]:
        safe_days = max(1, min(int(days), 3650))
        clean_granularity = (granularity or "daily").strip().lower()
        if clean_granularity not in _VALID_GRANULARITIES:
            raise ValueError(f"granularity must be one of {list(_VALID_GRANULARITIES)}.")

        if repo_name:
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, _, repo = repo_name.partition("/")
            clean_owner, clean_repo = validate_owner_repo_fn(owner, repo)
            repo_filter = f"{clean_owner}/{clean_repo}".lower()
        else:
            repo_filter = None

        if not self.data_dir.exists() or not self.data_dir.is_dir():
            empty: dict[str, Any] = {
                "data_dir": str(self.data_dir),
                "files_read": 0,
                "row_count": 0,
                "granularity": clean_granularity,
                "repositories": [],
                "by_repo": [],
            }
            if clean_granularity == "daily":
                empty["rows"] = []
            elif clean_granularity == "weekly":
                empty["weekly"] = []
            return empty

        cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).strftime("%Y-%m-%d")
        all_rows: list[dict[str, Any]] = []
        all_cols: set[str] = set()
        files_read = 0
        files_skipped = 0
        for entry in sorted(self.data_dir.iterdir()):
            if not entry.is_file() or not _CSV_FILE_RE.match(entry.name):
                continue
            if not _csv_archive_may_overlap_cutoff(entry.name, cutoff):
                files_skipped += 1
                continue
            try:
                cols, rows = _read_vault_csv_cached(entry)
                if rows:
                    all_cols.update(cols)
                    all_rows.extend(rows)
                    files_read += 1
            except Exception as exc:
                logger.warning("Skipping unreadable CSV %s: %s", entry.name, exc)

        if not all_rows:
            empty_res: dict[str, Any] = {
                "data_dir": str(self.data_dir),
                "files_read": files_read,
                "files_skipped": files_skipped,
                "row_count": 0,
                "granularity": clean_granularity,
                "repositories": [],
                "by_repo": [],
            }
            if clean_granularity == "daily":
                empty_res["rows"] = []
            elif clean_granularity == "weekly":
                empty_res["weekly"] = []
            return empty_res

        repo_col = "repo" if "repo" in all_cols else ("repository" if "repository" in all_cols else None)
        has_date = "date" in all_cols

        filtered: list[dict[str, Any]] = []
        for row in all_rows:
            if repo_filter and repo_col:
                if str(row.get(repo_col, "")).lower() != repo_filter:
                    continue
            if has_date and str(row.get("date", "")) < cutoff:
                continue
            filtered.append(row)

        if has_date and len(filtered) > 1:
            filtered.sort(key=lambda r: str(r.get("date", "")))

        if repo_col and has_date:
            deduped_map: dict[tuple[str, str], dict[str, Any]] = {}
            for row in filtered:
                key = (str(row.get(repo_col, "")), str(row.get("date", "")))
                deduped_map[key] = row
            deduped = list(deduped_map.values())
        else:
            deduped = filtered

        per_repo_totals: dict[str, dict[str, Any]] = {}
        total_views = 0
        total_uv = 0
        total_clones = 0
        total_uc = 0
        for row in deduped:
            rv = str(row.get(repo_col)) if (repo_col and not is_nullish(row.get(repo_col))) else None
            views = safe_int(row.get("views"))
            uv = safe_int(row.get("unique_visitors"))
            clones = safe_int(row.get("clones"))
            uc = safe_int(row.get("unique_cloners"))
            total_views += views
            total_uv += uv
            total_clones += clones
            total_uc += uc
            if rv:
                bucket = per_repo_totals.get(rv)
                if bucket is None:
                    bucket = {
                        "repo": rv,
                        "days_with_data": 0,
                        "views": 0,
                        "unique_visitors": 0,
                        "clones": 0,
                        "unique_cloners": 0,
                    }
                    per_repo_totals[rv] = bucket
                bucket["days_with_data"] += 1
                bucket["views"] += views
                bucket["unique_visitors"] += uv
                bucket["clones"] += clones
                bucket["unique_cloners"] += uc

        repos_found = sorted(per_repo_totals.keys())
        by_repo = sorted(per_repo_totals.values(), key=lambda x: (x["views"], x["repo"]), reverse=True)
        clone_ratio = round(total_uc / total_uv, 4) if total_uv > 0 else 0.0
        summary = {
            "total_views": total_views,
            "total_unique_visitors": total_uv,
            "total_clones": total_clones,
            "total_unique_cloners": total_uc,
            "clone_to_visitor_ratio": clone_ratio,
            "earliest_date": str(deduped[0].get("date", "")) if (deduped and has_date) else None,
            "latest_date": str(deduped[-1].get("date", "")) if (deduped and has_date) else None,
        }
        out: dict[str, Any] = {
            "data_dir": str(self.data_dir),
            "files_read": files_read,
            "files_skipped": files_skipped,
            "row_count": len(deduped),
            "granularity": clean_granularity,
            "repositories": repos_found,
            "summary": summary,
            "by_repo": by_repo,
        }
        if clean_granularity == "daily":
            out["rows"] = [
                {k: ("" if is_nullish(v) else v) for k, v in row.items()}
                for row in deduped[-365:]
            ]
        elif clean_granularity == "weekly":
            out["weekly"] = _aggregate_weekly_rows(deduped)
        return out

    def _build_widget_embed(self, **kwargs: Any) -> dict[str, Any]:
        unknown = set(kwargs.keys()) - _LOCAL_EMBED_PARAM_KEYS
        if unknown:
            raise ValueError(f"Unsupported argument(s): {sorted(unknown)}")

        kind = str(kwargs.get("kind") or "").strip().lower()
        if kind not in _VALID_KINDS:
            raise ValueError(f"Unknown widget kind {kind!r}. Allowed: {sorted(_VALID_KINDS)}.")

        clean_format = str(kwargs.get("format") or "all").strip().lower()
        if clean_format not in _VALID_OUTPUT_FORMATS:
            raise ValueError(f"Invalid format {clean_format!r}. Allowed: {sorted(_VALID_OUTPUT_FORMATS)}.")

        safe_href = _validate_href(kwargs.get("href"))
        params: dict[str, Any] = {}
        theme = _validate_safe_text("theme", kwargs.get("theme"), 48)
        variant = _validate_safe_text("variant", kwargs.get("variant"), 48)
        shape = _validate_safe_text("shape", kwargs.get("shape"), 32)
        if theme:
            params["theme"] = theme.lower()
        if variant:
            params["variant"] = variant.lower()
        if shape:
            params["shape"] = shape.lower()
        if kwargs.get("hide_border"):
            params["hide_border"] = "true"

        for color_field in ("color", "label_color", "value_color"):
            cval = _validate_color(color_field, kwargs.get(color_field))
            if cval:
                params[color_field] = cval

        path = ""
        default_alt = "Gitlytics Widget"
        if kind == "card":
            username = str(kwargs.get("username") or "").strip()
            if not _GITHUB_USER_RE.match(username):
                raise ValueError("Invalid GitHub username.")
            card_type = _validate_safe_text("card_type", kwargs.get("card_type") or "profile-card", 48)
            path = f"/api/cards/{card_type}/{username}.svg"
            default_alt = f"{username} {card_type}"
        elif kind == "tech_badge":
            slug = _validate_safe_text("slug", kwargs.get("slug"), 48)
            if not slug:
                raise ValueError("slug is required for tech_badge.")
            params["slug"] = slug.lower()
            params["style"] = _validate_safe_text("style", kwargs.get("style") or "icon_label_value", 32)
            for key in ("label", "value"):
                val = _validate_safe_text(key, kwargs.get(key), 64)
                if val:
                    params[key] = val
            path = "/api/badge/tech.svg"
            default_alt = params.get("label") or slug
        elif kind == "static_badge":
            lbl = _validate_safe_text("label", kwargs.get("label"), 64)
            msg = _validate_safe_text("message", kwargs.get("message") or kwargs.get("value"), 64)
            if not lbl or not msg:
                raise ValueError("label and message are required for static_badge.")
            params["label"] = lbl
            params["message"] = msg
            logo_slug = _validate_safe_text("slug", kwargs.get("slug"), 48)
            if logo_slug:
                params["logo"] = logo_slug.lower()
            path = "/api/badge/static.svg"
            default_alt = f"{lbl}: {msg}"
        elif kind in ("live_badge", "chart"):
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, repo = validate_owner_repo_fn(str(kwargs.get("owner") or ""), str(kwargs.get("repo") or ""))
            metric = str(kwargs.get("metric") or "views").strip().lower()
            if metric not in ("views", "clones", "stars"):
                raise ValueError("metric must be 'views', 'clones', or 'stars'.")
            lbl = _validate_safe_text("label", kwargs.get("label"), 64)
            if lbl and kind == "live_badge":
                params["label"] = lbl
            prefix = "/api/badge" if kind == "live_badge" else "/api/chart"
            path = f"{prefix}/{owner}/{repo}/{metric}.svg"
            default_alt = f"{owner}/{repo} {metric}"
        elif kind == "pypi_badge":
            pkg = str(kwargs.get("package_name") or "").strip()
            if not _PYPI_RE.match(pkg):
                raise ValueError("Invalid PyPI package_name.")
            lbl = _validate_safe_text("label", kwargs.get("label"), 64)
            if lbl:
                params["label"] = lbl
            path = f"/api/badge/pypi/{pkg}.svg"
            default_alt = f"PyPI {pkg}"
        elif kind == "npm_badge":
            pkg = str(kwargs.get("package_name") or "").strip()
            if not pkg or ".." in pkg or not _NPM_RE.match(pkg):
                raise ValueError("Invalid npm package_name.")
            lbl = _validate_safe_text("label", kwargs.get("label"), 64)
            if lbl:
                params["label"] = lbl
            path = f"/api/badge/npm/{pkg}.svg"
            default_alt = f"npm {pkg}"
        elif kind == "skills_badge":
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, repo = validate_owner_repo_fn(str(kwargs.get("owner") or ""), str(kwargs.get("repo") or ""))
            sp = kwargs.get("skills_path")
            if sp:
                cleaned_sp = str(sp).strip("/")
                if not cleaned_sp or ".." in cleaned_sp or not _SKILLS_PATH_RE.match(cleaned_sp):
                    raise ValueError("Invalid skills_path.")
                params["path"] = cleaned_sp
            lbl = _validate_safe_text("label", kwargs.get("label"), 64)
            if lbl:
                params["label"] = lbl
            path = f"/api/badge/skills/{owner}/{repo}.svg"
            default_alt = f"{owner}/{repo} skills"
        elif kind == "logo":
            slug = _validate_safe_text("slug", kwargs.get("slug"), 48)
            if not slug:
                raise ValueError("slug is required for logo.")
            if "color" in params:
                params["icon_color"] = params.pop("color")
            if "label_color" in params:
                params["bg_color"] = params.pop("label_color")
            path = f"/api/logos/{slug.lower()}.svg"
            default_alt = slug
        elif kind == "star_history":
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, repo = validate_owner_repo_fn(str(kwargs.get("owner") or ""), str(kwargs.get("repo") or ""))
            params["owner"] = owner
            params["repo"] = repo
            path = "/api/star-history-image"
            default_alt = f"{owner}/{repo} star history"

        alt = _validate_safe_text("alt_text", kwargs.get("alt_text"), 100) or default_alt
        qs = urlencode(params)
        url = f"{self.api_base}{path}?{qs}" if qs else f"{self.api_base}{path}"
        markdown = f"![{alt}]({url})"
        img_html = f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}" />'
        if safe_href:
            escaped_href = html.escape(safe_href, quote=True)
            markdown = f"[{markdown}]({safe_href})"
            img_html = f'<a href="{escaped_href}">{img_html}</a>'

        dark_theme = _validate_safe_text("dark_theme", kwargs.get("dark_theme"), 48)
        light_theme = _validate_safe_text("light_theme", kwargs.get("light_theme"), 48)
        dual_picture_html: Optional[str] = None
        if dark_theme or light_theme or clean_format == "picture":
            d_theme = (dark_theme or params.get("theme") or "gitlytics-dark").lower()
            l_theme = (light_theme or "gitlytics-light").lower()
            dark_url = f"{self.api_base}{path}?{urlencode({**params, 'theme': d_theme})}"
            light_url = f"{self.api_base}{path}?{urlencode({**params, 'theme': l_theme})}"
            picture = (
                "<picture>\n"
                f'  <source media="(prefers-color-scheme: dark)" srcset="{html.escape(dark_url, quote=True)}" />\n'
                f'  <source media="(prefers-color-scheme: light)" srcset="{html.escape(light_url, quote=True)}" />\n'
                f'  <img alt="{html.escape(alt, quote=True)}" src="{html.escape(dark_url, quote=True)}" />\n'
                "</picture>"
            )
            dual_picture_html = (
                f'<a href="{html.escape(safe_href, quote=True)}">\n{picture}\n</a>'
                if safe_href
                else picture
            )

        if clean_format == "url":
            out: dict[str, Any] = {"kind": kind, "url": url}
        elif clean_format == "markdown":
            out = {"kind": kind, "url": url, "markdown": markdown}
        elif clean_format == "html":
            out = {"kind": kind, "url": url, "html": img_html}
        elif clean_format == "picture":
            out = {"kind": kind, "url": url, "dual_theme_picture_html": dual_picture_html}
        else:
            out = {
                "kind": kind,
                "url": url,
                "markdown": markdown,
                "html": img_html,
                "dual_theme_picture_html": dual_picture_html,
            }
        if safe_href:
            out["href"] = safe_href
        return out

    def _build_widget_batch(
        self,
        *,
        items: list[dict[str, Any]],
        defaults: Optional[dict[str, Any]] = None,
        format: str = "markdown",
        layout: str = "none",
        on_error: str = "partial",
        include_items: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list of widget specifications.")
        if len(items) > MAX_LOCAL_WIDGET_BATCH_SIZE:
            raise ValueError(f"Batch may contain at most {MAX_LOCAL_WIDGET_BATCH_SIZE} items.")
        clean_format = (format or "markdown").strip().lower()
        if clean_format not in _VALID_OUTPUT_FORMATS:
            raise ValueError(f"Invalid format {format!r}.")
        clean_layout = (layout or "none").strip().lower()
        if clean_layout not in _VALID_BATCH_LAYOUTS:
            raise ValueError(f"Invalid layout {layout!r}.")
        clean_on_error = (on_error or "partial").strip().lower()
        if clean_on_error not in _VALID_BATCH_ERROR_MODES:
            raise ValueError(f"Invalid on_error {on_error!r}.")

        clean_defaults: dict[str, Any] = {}
        if defaults is not None:
            if not isinstance(defaults, dict):
                raise ValueError("defaults must be a JSON object.")
            unknown_defaults = set(defaults.keys()) - _LOCAL_BATCH_DEFAULT_KEYS
            if unknown_defaults:
                raise ValueError(f"Unsupported defaults key(s): {sorted(unknown_defaults)}")
            clean_defaults = {k: v for k, v in defaults.items() if v is not None}

        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        layout_parts: list[str] = []

        for idx, raw_item in enumerate(items):
            item_id: Optional[str] = None
            try:
                if not isinstance(raw_item, dict):
                    raise ValueError("Each batch item must be a JSON object.")
                merged = dict(clean_defaults)
                merged.update(raw_item)
                raw_id = merged.pop("id", None)
                if raw_id is not None:
                    item_id = _validate_safe_text("id", str(raw_id), 64)
                item_format = str(merged.pop("format", clean_format) or clean_format).strip().lower()
                full = self._build_widget_embed(**merged, format="all")

                if clean_layout == "inline_markdown":
                    layout_parts.append(full["markdown"])
                elif clean_layout == "centered_html":
                    layout_parts.append(full.get("dual_theme_picture_html") or full["html"])

                if item_format == "url":
                    projected: dict[str, Any] = {"kind": full["kind"], "url": full["url"]}
                elif item_format == "markdown":
                    projected = {"kind": full["kind"], "url": full["url"], "markdown": full["markdown"]}
                elif item_format == "html":
                    projected = {"kind": full["kind"], "url": full["url"], "html": full["html"]}
                elif item_format == "picture":
                    pic = full.get("dual_theme_picture_html") or self._build_widget_embed(**merged, format="picture")["dual_theme_picture_html"]
                    projected = {"kind": full["kind"], "url": full["url"], "dual_theme_picture_html": pic}
                else:
                    projected = full

                if "href" in full and "href" not in projected:
                    projected["href"] = full["href"]
                entry: dict[str, Any] = {"index": idx, **projected}
                if item_id is not None:
                    entry["id"] = item_id
                results.append(entry)
            except Exception as exc:
                if clean_on_error == "fail_fast":
                    raise ValueError(f"Batch item {idx} failed: {exc}") from exc
                err_entry: dict[str, Any] = {"index": idx, "error": str(exc)}
                if item_id is not None:
                    err_entry["id"] = item_id
                errors.append(err_entry)

        combined_snippet: Optional[str] = None
        if clean_layout == "inline_markdown" and layout_parts:
            combined_snippet = " ".join(layout_parts)
        elif clean_layout == "centered_html" and layout_parts:
            indented = "\n".join(f"  {part}" for part in layout_parts)
            combined_snippet = f'<p align="center">\n{indented}\n</p>'

        out: dict[str, Any] = {
            "requested": len(items),
            "succeeded": len(results),
            "failed": len(errors),
            "format": clean_format,
            "layout": clean_layout,
            "errors": errors,
        }
        if include_items or clean_layout == "none":
            out["items"] = results
        if combined_snippet is not None:
            out["combined_snippet"] = combined_snippet
        return out

    def _read_resource(self, uri: str) -> Optional[dict[str, Any]]:
        if uri == "gitlytics://local/vault-summary":
            data = self._query_csv_vault(days=90, granularity="summary")
            return {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": json.dumps(data, separators=(",", ":"))}
                ]
            }
        if uri == "gitlytics://local/files":
            data = {"data_dir": str(self.data_dir), "files": self._list_vault_files()}
            return {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": json.dumps(data, separators=(",", ":"))}
                ]
            }
        return None

    def _get_prompt(self, name: str, arguments: dict[str, Any]) -> Optional[dict[str, Any]]:
        args = arguments if isinstance(arguments, dict) else {}
        if name == "audit_local_vault":
            days = int(args.get("days") or 90)
            text = (
                f"Audit the local Gitlytics CSV vault over the last {days} days.\n"
                f"1. Call local_vault_summary(days={days}) to inspect tracked repos and date coverage.\n"
                f"2. Call local_query_csv_vault(days={days}, granularity='weekly') for trend velocity.\n"
                "3. Summarize views, clones, clone-to-visitor ratio, and any missing archive days."
            )
            return {
                "description": LOCAL_PROMPT_DEFINITIONS[0]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        if name == "create_badge_row":
            owner = str(args.get("owner") or "").strip()
            repo = str(args.get("repo") or "").strip()
            if not owner or not repo:
                raise ValueError("owner and repo are required for prompt 'create_badge_row'.")
            theme = str(args.get("theme") or "gitlytics-dark").strip()
            text = (
                f"Build a unified Gitlytics badge row for {owner}/{repo}.\n"
                f"Call local_build_widget_batch once with defaults={{owner: '{owner}', repo: '{repo}', theme: '{theme}', variant: 'clay'}}, "
                "layout='inline_markdown', and items for live_badge (views, clones, stars) plus stack tech_badges."
            )
            return {
                "description": LOCAL_PROMPT_DEFINITIONS[1]["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
            }
        return None

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _tool_result({"error": "Tool arguments must be a JSON object."}, is_error=True)

        allowed = LOCAL_TOOL_ALLOWED_ARGS.get(name)
        if allowed is not None:
            unknown = sorted(set(arguments.keys()) - allowed)
            if unknown:
                return _tool_result(
                    {"error": f"Unsupported argument(s) for {name}: {unknown}."},
                    is_error=True,
                )

        # Route any incidental prints inside SDK calls to stderr so stdout stays pure JSON-RPC.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                if name == "local_vault_summary":
                    summary_data = self._query_csv_vault(
                        days=arguments.get("days", 90),
                        granularity="summary",
                    )
                    summary_data["files"] = self._list_vault_files()
                    return _tool_result(summary_data)
                if name == "local_query_csv_vault":
                    return _tool_result(
                        self._query_csv_vault(
                            repo_name=arguments.get("repo_name"),
                            days=arguments.get("days", 90),
                            granularity=arguments.get("granularity", "daily"),
                        )
                    )
                if name == "local_fetch_traffic":
                    token = _require_env_token()
                    repo_name = arguments.get("repo_name")
                    if repo_name:
                        validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
                        owner, _, repo = str(repo_name).partition("/")
                        co, cr = validate_owner_repo_fn(owner, repo)
                        repo_name = f"{co}/{cr}"
                    fetch_traffic_fn = _resolve_dep("fetch_traffic")
                    payload = fetch_traffic_fn(
                        token=token,
                        repo_name=repo_name,
                        print_table=False,
                        return_format="summary",
                        metrics=arguments.get("metrics"),
                        export_public_only=not bool(arguments.get("include_private", False)),
                    )
                    return _tool_result(payload)
                if name == "local_sync_archive":
                    token = _require_env_token()
                    repo_name = arguments.get("repo_name")
                    if repo_name:
                        validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
                        owner, _, repo = str(repo_name).partition("/")
                        co, cr = validate_owner_repo_fn(owner, repo)
                        repo_name = f"{co}/{cr}"
                    sync_fn = _resolve_dep("sync")
                    rows = sync_fn(
                        token=token,
                        repo_name=repo_name,
                        data_dir=str(self.data_dir),
                        output_mode=str(arguments.get("output_mode") or "monthly"),
                        public_only=bool(arguments.get("public_only", True)),
                    )
                    return _tool_result({"synced_rows": rows, "data_dir": str(self.data_dir)})
                if name == "local_star_history":
                    validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
                    owner, repo = validate_owner_repo_fn(
                        str(arguments.get("owner") or ""),
                        str(arguments.get("repo") or ""),
                    )
                    fetch_stars_fn = _resolve_dep("fetch_star_history")
                    points = list(fetch_stars_fn(owner, repo, token=_resolve_env_token()) or [])
                    last_pt = points[-1] if points else {}
                    current_stars = safe_int(last_pt.get("stars", last_pt.get("total", 0)))
                    milestones = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000]
                    next_milestone = next((m for m in milestones if m > current_stars), current_stars + 10000)
                    return _tool_result(
                        {
                            "repo": f"{owner}/{repo}",
                            "current_stars": current_stars,
                            "next_milestone": next_milestone,
                            "stars_to_next_milestone": max(0, next_milestone - current_stars),
                            "point_count": len(points),
                            "points": points[-120:],
                        }
                    )
                if name == "local_build_widget_embed":
                    return _tool_result(self._build_widget_embed(**arguments))
                if name == "local_build_widget_batch":
                    batch_res = self._build_widget_batch(**arguments)
                    text_override: Optional[str] = None
                    if batch_res.get("combined_snippet"):
                        lines = [
                            f"Built {batch_res['succeeded']}/{batch_res['requested']} widgets.",
                            batch_res["combined_snippet"],
                        ]
                        if batch_res.get("errors"):
                            lines.append("Errors: " + json.dumps(batch_res["errors"], separators=(",", ":")))
                        text_override = "\n".join(lines)
                    return _tool_result(batch_res, text_override=text_override)
            except Exception as exc:
                return _tool_result({"error": str(exc)}, is_error=True)

        return _tool_result({"error": f"Unknown tool: {name}"}, is_error=True)

    def handle_message(self, msg: Any) -> Optional[dict[str, Any]]:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}

        req_id = msg.get("id")
        method = str(msg.get("method"))
        params = msg.get("params") or {}

        if req_id is None:
            return None

        if method == "initialize":
            req_ver = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            negotiated = req_ver if req_ver in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": negotiated,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": "gitlytics", "version": __version__},
                    "instructions": (
                        "Local Gitlytics MCP server: query offline CSV vaults with local_vault_summary "
                        "or local_query_csv_vault, and generate multiple README badges in 1 call via "
                        "local_build_widget_batch."
                    ),
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": LOCAL_TOOL_DEFINITIONS}}
        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") or {}
            if tool_name not in LOCAL_TOOL_BY_NAME:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": self.call_tool(tool_name, tool_args)}
        if method == "resources/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": LOCAL_RESOURCE_DEFINITIONS}}
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            res = self._read_resource(uri)
            if res is None:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32002, "message": f"Resource not found: {uri}"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": res}
        if method == "prompts/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": LOCAL_PROMPT_DEFINITIONS}}
        if method == "prompts/get":
            prompt_name = str(params.get("name") or "")
            try:
                prompt_res = self._get_prompt(prompt_name, params.get("arguments") or {})
            except ValueError as exc:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": str(exc)}}
            if prompt_res is None:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown prompt: {prompt_name}"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": prompt_res}

        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def run_stdio_server(
    data_dir: str = "./data",
    api_base: str = "https://api.gitlytics.dev",
    *,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
) -> None:
    """Run the local Gitlytics MCP server over newline-delimited JSON-RPC stdio."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    in_stream = stdin or sys.stdin
    out_stream = stdout or sys.stdout
    server = LocalMcpServer(data_dir=data_dir, api_base=api_base)

    for raw_line in in_stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            out_stream.write(json.dumps(resp) + "\n")
            out_stream.flush()
            continue

        if isinstance(msg, list):
            if not msg or len(msg) > 20:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Batch must contain 1 to 20 requests"}}
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()
                continue
            responses = []
            for item in msg:
                r = server.handle_message(item)
                if r is not None:
                    responses.append(r)
            if responses:
                out_stream.write(json.dumps(responses, default=str) + "\n")
                out_stream.flush()
            continue

        response = server.handle_message(msg)
        if response is not None:
            out_stream.write(json.dumps(response, default=str) + "\n")
            out_stream.flush()
