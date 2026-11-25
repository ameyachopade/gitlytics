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
from urllib.parse import urlencode

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
_CSV_FILE_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")
_VALID_KINDS = frozenset(
    {"card", "tech_badge", "static_badge", "live_badge", "pypi_badge", "chart", "star_history"}
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


LOCAL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "local_query_csv_vault",
        "title": "Query Local CSV Traffic Vault",
        "description": (
            "Read historical GitHub traffic metrics from local traffic_*.csv archive files "
            "inside data_dir. Works 100% offline with no GitHub token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_name": {"type": "string", "description": "Optional 'owner/repo' filter."},
                "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 90},
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
        "description": "Fetch cumulative star trajectory for a GitHub repository.",
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
        "description": "Validate and build hosted Gitlytics SVG card, badge, or chart embed URLs and Markdown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["card", "tech_badge", "static_badge", "live_badge", "pypi_badge", "chart", "star_history"],
                },
                "username": {"type": "string"},
                "card_type": {"type": "string"},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "metric": {"type": "string", "enum": ["views", "clones", "stars"]},
                "package_name": {"type": "string"},
                "slug": {"type": "string"},
                "label": {"type": "string"},
                "message": {"type": "string"},
                "value": {"type": "string"},
                "theme": {"type": "string"},
                "variant": {"type": "string"},
                "shape": {"type": "string"},
                "style": {"type": "string", "enum": ["icon", "icon_label", "icon_label_value"]},
                "alt_text": {"type": "string"},
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
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

    def _query_csv_vault(self, repo_name: Optional[str] = None, days: int = 90) -> dict[str, Any]:
        safe_days = max(1, min(int(days), 3650))
        if repo_name:
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, _, repo = repo_name.partition("/")
            clean_owner, clean_repo = validate_owner_repo_fn(owner, repo)
            repo_filter = f"{clean_owner}/{clean_repo}".lower()
        else:
            repo_filter = None

        if not self.data_dir.exists() or not self.data_dir.is_dir():
            return {
                "data_dir": str(self.data_dir),
                "files_read": 0,
                "row_count": 0,
                "repositories": [],
                "rows": [],
            }

        all_rows: list[dict[str, Any]] = []
        all_cols: set[str] = set()
        files_read = 0
        for entry in sorted(self.data_dir.iterdir()):
            if not entry.is_file() or not _CSV_FILE_RE.match(entry.name):
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
            return {
                "data_dir": str(self.data_dir),
                "files_read": files_read,
                "row_count": 0,
                "repositories": [],
                "rows": [],
            }

        repo_col = "repo" if "repo" in all_cols else ("repository" if "repository" in all_cols else None)
        has_date = "date" in all_cols
        cutoff = (
            (datetime.now(timezone.utc) - timedelta(days=safe_days)).strftime("%Y-%m-%d")
            if has_date
            else None
        )

        filtered: list[dict[str, Any]] = []
        for row in all_rows:
            if repo_filter and repo_col:
                if str(row.get(repo_col, "")).lower() != repo_filter:
                    continue
            if cutoff is not None:
                if str(row.get("date", "")) < cutoff:
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

        repos_set: set[str] = set()
        total_views = 0
        total_uv = 0
        total_clones = 0
        total_uc = 0
        for row in deduped:
            if repo_col:
                rv = row.get(repo_col)
                if not is_nullish(rv):
                    repos_set.add(str(rv))
            total_views += safe_int(row.get("views"))
            total_uv += safe_int(row.get("unique_visitors"))
            total_clones += safe_int(row.get("clones"))
            total_uc += safe_int(row.get("unique_cloners"))

        repos_found = sorted(repos_set)
        summary = {
            "total_views": total_views,
            "total_unique_visitors": total_uv,
            "total_clones": total_clones,
            "total_unique_cloners": total_uc,
        }
        tail_rows = [
            {k: ("" if is_nullish(v) else v) for k, v in row.items()}
            for row in deduped[-365:]
        ]
        return {
            "data_dir": str(self.data_dir),
            "files_read": files_read,
            "row_count": len(deduped),
            "repositories": repos_found,
            "summary": summary,
            "rows": tail_rows,
        }

    def _build_widget_embed(self, **kwargs: Any) -> dict[str, Any]:
        kind = str(kwargs.get("kind") or "").strip().lower()
        if kind not in _VALID_KINDS:
            raise ValueError(f"Unknown widget kind {kind!r}. Allowed: {sorted(_VALID_KINDS)}.")

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
            path = "/api/badge/static.svg"
            default_alt = f"{lbl}: {msg}"
        elif kind in ("live_badge", "chart"):
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, repo = validate_owner_repo_fn(str(kwargs.get("owner") or ""), str(kwargs.get("repo") or ""))
            metric = str(kwargs.get("metric") or "views").strip().lower()
            if metric not in ("views", "clones", "stars"):
                raise ValueError("metric must be 'views', 'clones', or 'stars'.")
            prefix = "/api/badge" if kind == "live_badge" else "/api/chart"
            path = f"{prefix}/{owner}/{repo}/{metric}.svg"
            default_alt = f"{owner}/{repo} {metric}"
        elif kind == "pypi_badge":
            pkg = str(kwargs.get("package_name") or "").strip()
            if not _PYPI_RE.match(pkg):
                raise ValueError("Invalid PyPI package_name.")
            path = f"/api/badge/pypi/{pkg}.svg"
            default_alt = f"PyPI {pkg}"
        elif kind == "star_history":
            validate_owner_repo_fn = _resolve_dep("validate_owner_repo")
            owner, repo = validate_owner_repo_fn(str(kwargs.get("owner") or ""), str(kwargs.get("repo") or ""))
            params["repos"] = f"{owner}/{repo}"
            path = "/api/star-history-image"
            default_alt = f"{owner}/{repo} star history"

        alt = _validate_safe_text("alt_text", kwargs.get("alt_text"), 100) or default_alt
        qs = urlencode(params)
        url = f"{self.api_base}{path}?{qs}" if qs else f"{self.api_base}{path}"
        return {
            "kind": kind,
            "url": url,
            "markdown": f"![{alt}]({url})",
            "html": f'<img src="{html.escape(url, quote=True)}" alt="{html.escape(alt, quote=True)}" />',
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return _tool_result({"error": "Tool arguments must be a JSON object."}, is_error=True)

        # Route any incidental prints inside SDK calls to stderr so stdout stays pure JSON-RPC.
        with contextlib.redirect_stdout(sys.stderr):
            try:
                if name == "local_query_csv_vault":
                    return _tool_result(
                        self._query_csv_vault(
                            repo_name=arguments.get("repo_name"),
                            days=arguments.get("days", 90),
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
                    points = fetch_stars_fn(owner, repo, token=_resolve_env_token())
                    return _tool_result({"repo": f"{owner}/{repo}", "point_count": len(points), "points": points[-120:]})
                if name == "local_build_widget_embed":
                    return _tool_result(self._build_widget_embed(**arguments))
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
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "gitlytics", "version": "0.9.0"},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": LOCAL_TOOL_DEFINITIONS}}
        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") or {}
            known = {t["name"] for t in LOCAL_TOOL_DEFINITIONS}
            if tool_name not in known:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown tool: {tool_name}"}}
            return {"jsonrpc": "2.0", "id": req_id, "result": self.call_tool(tool_name, tool_args)}

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

        response = server.handle_message(msg)
        if response is not None:
            out_stream.write(json.dumps(response, default=str) + "\n")
            out_stream.flush()
