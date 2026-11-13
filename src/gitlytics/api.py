"""
gitlytics/api.py
Powers the FastAPI backend — serves traffic data and the React dashboard to the browser.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import tempfile
import threading as _threading
import time as _time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
from fastapi import FastAPI, HTTPException, Body, File, Header, Request, Response, UploadFile, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gitlytics.core import (
    validate_token,
    get_user_profile,
    get_public_user,
    get_public_repos,
    validate_github_username,
    fetch_traffic_data,
    fetch_deep_stats_for_top,
    fetch_star_history,
    GitHubRateLimitError,
    StarHistoryFetchError,
    StargazersRestrictedError,
)
from gitlytics.process import process_uploaded_csv, build_react_payload, _to_bool

logger = logging.getLogger(__name__)

app = FastAPI(title="GitHub Traffic API")

# Only allow requests from localhost — never deployed publicly.
# Vite's dev port (5173) is intentionally excluded from the production allowlist.
_ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:4173",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:4173",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    # `Authorization` is required for cross-origin deployments that pass the
    # GitHub PAT in a header. Same-origin requests are unaffected.
    allow_headers=["Content-Type", "Authorization"],
)


# In-process short-term cache for token validation. Cleared when the
# Python process exits — same lifetime as the CLI / FastAPI worker.
# OrderedDict + insert-order eviction gives us a bounded LRU without
# pulling in functools.lru_cache (which can't be cleared on TTL).
_auth_cache: "OrderedDict[str, tuple]" = OrderedDict()
_auth_cache_lock = _threading.Lock()
_AUTH_CACHE_TTL = 300  # 5 minutes — applied only to positive (valid=True) results.
_AUTH_NEG_TTL = 10  # short TTL for negative results so a transient 5xx doesn't lock the user out.
_AUTH_CACHE_MAX = 256  # cap entries so a long-running worker can't grow unbounded

# Hard cap on CSV upload size — prevents DoS via /api/upload-csv.
#
# AF2-XC-04: keep this in lock-step with the cloud backend
# (`gitlytics-backend/app/auth/token.py::MAX_UPLOAD_BYTES`) and the published
# docs ("up to 5 MB per upload"). A larger local-only cap let a backup that
# worked in `gitlytics dashboard` fail with HTTP 413 in the cloud.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Only canonical sync files belong in the historical read path. Uploaded
# previews must never be merged in — they would otherwise accumulate forever.
_SYNC_CSV_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")

# Short-lived cache for live GitHub traffic. Without this every dashboard POST
# burns ~5 GitHub calls per repo and exhausts the PAT quota in ~20 loads.
_TRAFFIC_CACHE: "OrderedDict[str, tuple[float, pd.DataFrame]]" = OrderedDict()
_TRAFFIC_CACHE_LOCK = _threading.Lock()
_TRAFFIC_CACHE_TTL = 120.0  # seconds
_TRAFFIC_CACHE_MAX = 16

_DEEP_STATS_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_DEEP_STATS_CACHE_LOCK = _threading.Lock()
_DEEP_STATS_CACHE_TTL = 300.0  # seconds
_DEEP_STATS_CACHE_MAX = 16


def _parse_static_security_headers(headers_file: Path) -> dict[str, str]:
    """Parse the packaged ``_headers`` file into a header map.

    ``_headers`` is a Netlify/Cloudflare-Pages file: a path pattern on one
    line, followed by indented ``Name: value`` lines. Only the global ``/*``
    block is used for the local server (the rest are cache rules for CDN
    asset paths).
    """
    parsed: dict[str, str] = {}
    if not headers_file.exists():
        return parsed
    in_global_block = False
    try:
        lines = headers_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return parsed
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line[0].isspace():
            # A new path pattern starts a new block.
            in_global_block = line.strip() == "/*"
            continue
        if not in_global_block:
            continue
        if ":" not in line:
            continue
        name, _, value = line.strip().partition(":")
        name = name.strip()
        value = value.strip()
        if not name or name.lower() == "cache-control":
            # Cache rules are CDN-specific — the local server manages its own
            # caching semantics and must not inherit them.
            continue
        parsed[name] = value
    return parsed


def _static_security_headers() -> list[tuple[bytes, bytes]]:
    """Security headers the local dashboard must emit on every response.

    The packaged ``_headers`` file carries the Content-Security-Policy that
    protects the hosted dashboard. A self-hosted ``gitlytics dashboard``
    serves exactly the same HTML/JS, so it must ship the same protection
    (AF2-LIB-17).
    """
    headers: dict[str, str] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    static_dir = Path(__file__).parent / "static"
    headers.update(_parse_static_security_headers(static_dir / "_headers"))
    return [
        (name.lower().encode("latin-1"), value.encode("latin-1", errors="replace"))
        for name, value in headers.items()
    ]


class _SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app
        self.extra_headers = _static_security_headers()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name for name, _ in headers}
                for name, value in self.extra_headers:
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(_SecurityHeadersMiddleware)


def _require_upload_access(
    request: Request,
    x_upload_key: str | None = Header(default=None),
) -> None:
    """Reject cross-origin uploads even from loopback.

    Browsers attach an ``Origin`` (or at least ``Referer``) header to POSTs.
    Same-host requests are accepted; non-browser clients must present the
    configured upload key. There is deliberately no unconditional loopback
    bypass: a malicious page can make the victim's browser POST to
    127.0.0.1, and that request is cross-origin for the attacker.
    """
    headers = getattr(request, "headers", None) or {}
    origin = headers.get("origin") or headers.get("referer")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            parsed = None
        host = (headers.get("host") or "").lower()
        if (
            parsed is not None
            and parsed.scheme in ("http", "https")
            and parsed.netloc
            and host
            and parsed.netloc.lower() == host
        ):
            return

    expected = (os.environ.get("GITLYTICS_UPLOAD_KEY") or "").strip()
    got = (x_upload_key or "").strip()
    if expected and got and secrets.compare_digest(got, expected):
        return
    raise HTTPException(
        status_code=403,
        detail="Upload requires a same-origin request or a valid upload key.",
    )


def _require_browser_same_origin(request: Request) -> None:
    """Block cross-origin browser requests (including simple POSTs) when an
    Origin/Referer is attached, protecting local GITLYTICS_TOKEN quota (AF-LIB-03).
    """
    headers = getattr(request, "headers", None) or {}
    origin = headers.get("origin") or headers.get("referer")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            parsed = None
        host = (headers.get("host") or "").lower()
        if (
            parsed is None
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or not host
            or parsed.netloc.lower() != host
        ):
            raise HTTPException(
                status_code=403,
                detail="Cross-origin request rejected.",
            )
        if request.method == "POST":
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and ctype != "application/json":
                raise HTTPException(
                    status_code=415,
                    detail="Unsupported Content-Type; application/json required.",
                )


def _env_token_allowed() -> bool:
    """Environment-token fallback is opt-in (set by serve_dashboard for loopback)."""
    return (os.environ.get("GITLYTICS_ALLOW_ENV_TOKEN") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _get_token(token: str = None) -> str:
    # Explicit request tokens always win. The operator's environment token is
    # only usable when the dashboard was started with an explicit --token on a
    # loopback bind; otherwise every caller must authenticate for themselves.
    if token and token.strip():
        return token.strip()
    if _env_token_allowed():
        return os.environ.get("GITLYTICS_TOKEN")
    return None


def _validate_token_cached(token: str):
    # M-1: cache validation results to avoid a double HTTP round-trip on every /api/traffic call
    key = hashlib.sha256(token.encode()).hexdigest()
    now = _time.time()
    # Lock guards the read-then-write so two threads can't both miss the
    # cache and double-fetch the same token (FastAPI runs sync routes in
    # a worker threadpool, so this race is reachable under load).
    with _auth_cache_lock:
        cached = _auth_cache.get(key)
        if cached is not None:
            valid, username, expires = cached
            if now < expires:
                # Refresh insertion order so an active key isn't evicted FIFO-style.
                _auth_cache.move_to_end(key)
                return valid, username
    valid, username = validate_token(token)
    # Negative results get a short TTL so a transient upstream 5xx doesn't
    # lock the user out for 5 minutes; positive results cache fully.
    ttl = _AUTH_CACHE_TTL if valid else _AUTH_NEG_TTL
    with _auth_cache_lock:
        _auth_cache[key] = (valid, username, now + ttl)
        _auth_cache.move_to_end(key)
        while len(_auth_cache) > _AUTH_CACHE_MAX:
            _auth_cache.popitem(last=False)
    return valid, username


@app.get("/api/config")
def get_config():
    return {
        "has_token": bool(os.environ.get("GITLYTICS_TOKEN")) and _env_token_allowed(),
        "has_data_dir": bool(os.environ.get("GITLYTICS_DATA_DIR"))
    }


@app.post("/api/auth")
def auth(request: Request, token: str = Body("", embed=True)):
    _require_browser_same_origin(request)
    # Validate the token and return the user's GitHub profile info
    active_token = _get_token(token)
    if not active_token:
        raise HTTPException(status_code=401, detail="No token provided and no environment token found.")

    ok, username = validate_token(active_token)
    if not ok:
        logger.warning("Authentication attempt failed for a provided token.")
        raise HTTPException(status_code=401, detail=username)

    profile = get_user_profile(active_token)

    return {
        "authenticated": True,
        "username": profile["login"] or username,
        "name": profile["name"] or username,
        "avatar_url": profile["avatar_url"],
        "bio": profile.get("bio"),
        "location": profile.get("location"),
        "followers": profile.get("followers", 0),
        "following": profile.get("following", 0),
    }


def _fetch_username_data(username: str, token: str | None = None):
    if not username or not username.strip():
        raise HTTPException(status_code=400, detail="Username is required.")
    try:
        clean = validate_github_username(username.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        active_token = _get_token(token)
        if active_token:
            profile = get_public_user(clean, token=active_token)
            repos = get_public_repos(clean, token=active_token)
        else:
            profile = get_public_user(clean)
            repos = get_public_repos(clean)
        return {"profile": profile, "repos": repos}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError:
        raise HTTPException(status_code=502, detail="GitHub is temporarily unavailable.")
    except Exception as exc:
        logger.warning("Username fetch failed for %s: %s", username, exc)
        raise HTTPException(status_code=500, detail="Failed to fetch GitHub data.")


@app.get("/api/username/{username}")
def get_username_data_by_path(
    request: Request,
    username: str,
    authorization: str | None = Header(default=None),
):
    _require_browser_same_origin(request)
    tok = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        tok = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()
    return _fetch_username_data(username, token=tok)


@app.post("/api/username")
def get_username_data(
    request: Request,
    username: str = Body("", embed=True),
    token: str = Body("", embed=True),
    authorization: str | None = Header(default=None),
):
    _require_browser_same_origin(request)
    tok = (token or "").strip() or None
    if not tok and authorization:
        parts = authorization.strip().split(None, 1)
        tok = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()
    return _fetch_username_data(username, token=tok)


_CSV_CACHE: "OrderedDict[tuple[str, float], tuple[int, int, pd.DataFrame]]" = OrderedDict()
_CSV_CACHE_LOCK = _threading.Lock()


def _normalize_csv_header(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a CSV header to the canonical lower-case schema.

    Legacy / hand-edited exports use title-case or mixed-case headers
    (``Date``, ``Repository``, ``Views``). Without normalisation the
    downstream ``groupby("repository")`` raises ``KeyError: 'repository'`` and
    turns the whole dashboard into an HTTP 500 (AF2-LIB-02).

    This mirrors what ``automation._normalize_field`` /
    ``process.process_uploaded_csv`` already do: strip a UTF-8 BOM, trim,
    lower-case, map the legacy display names onto the canonical columns and
    drop duplicate columns so ``df[col]`` is always a Series.
    """
    from gitlytics.process import _LEGACY_COLUMN_MAP

    frame = df.copy()
    frame.columns = [str(c).lstrip("\ufeff").strip().lower() for c in frame.columns]
    frame.columns = [_LEGACY_COLUMN_MAP.get(c, c) for c in frame.columns]
    if frame.columns.duplicated().any():
        # Coalesce duplicates ("Views" + "views") so df[col] stays a Series.
        deduped = {}
        for col_name in dict.fromkeys(frame.columns):
            matches = frame.loc[:, frame.columns == col_name]
            if matches.shape[1] == 1:
                deduped[col_name] = matches.iloc[:, 0]
            else:
                deduped[col_name] = matches.bfill(axis=1).iloc[:, 0]
        frame = pd.DataFrame(deduped, index=frame.index)
    return frame


def _read_csv_cached(p: Path) -> pd.DataFrame:
    try:
        st = p.stat()
    except OSError:
        return _normalize_csv_header(pd.read_csv(p))
    key = (str(p.resolve()), st.st_mtime)
    with _CSV_CACHE_LOCK:
        cached = _CSV_CACHE.get(key)
        if cached is not None:
            c_ns, c_size, c_df = cached
            if c_ns == st.st_mtime_ns and c_size == st.st_size:
                _CSV_CACHE.move_to_end(key)
                return c_df.copy()
    df = _normalize_csv_header(pd.read_csv(p))
    with _CSV_CACHE_LOCK:
        _CSV_CACHE[key] = (st.st_mtime_ns, st.st_size, df.copy())
        _CSV_CACHE.move_to_end(key)
        while len(_CSV_CACHE) > 128:
            _CSV_CACHE.popitem(last=False)
    return df


def _cached_live_traffic(token: str) -> pd.DataFrame:
    """Return live traffic for a token, cached for a short TTL.

    Keyed by a full SHA-256 hex digest so the raw token never sits in a dict key.
    Empty frames (outage/failure) are never cached so recovery is immediate.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    now = _time.time()
    with _TRAFFIC_CACHE_LOCK:
        hit = _TRAFFIC_CACHE.get(key)
        if hit is not None:
            expires, frame = hit
            if now < expires:
                _TRAFFIC_CACHE.move_to_end(key)
                return frame.copy()
    frame = fetch_traffic_data(token)
    if not frame.empty:
        with _TRAFFIC_CACHE_LOCK:
            _TRAFFIC_CACHE[key] = (now + _TRAFFIC_CACHE_TTL, frame.copy())
            _TRAFFIC_CACHE.move_to_end(key)
            while len(_TRAFFIC_CACHE) > _TRAFFIC_CACHE_MAX:
                _TRAFFIC_CACHE.popitem(last=False)
    return frame


def _cached_deep_stats(token: str, repos_with_views: list[dict], top_n: int = 20) -> dict:
    """Bounded TTL cache for fetch_deep_stats_for_top (~80 upstream calls)."""
    top_names = tuple(
        r["repository"]
        for r in sorted(repos_with_views, key=lambda x: x.get("total_views", 0), reverse=True)[:top_n]
    )
    key = (hashlib.sha256(token.encode()).hexdigest(), top_names)
    now = _time.time()
    with _DEEP_STATS_CACHE_LOCK:
        hit = _DEEP_STATS_CACHE.get(key)
        if hit is not None:
            expires, stats = hit
            if now < expires:
                _DEEP_STATS_CACHE.move_to_end(key)
                return dict(stats)
    stats = fetch_deep_stats_for_top(token, repos_with_views, top_n=top_n)
    if stats:
        with _DEEP_STATS_CACHE_LOCK:
            _DEEP_STATS_CACHE[key] = (now + _DEEP_STATS_CACHE_TTL, dict(stats))
            _DEEP_STATS_CACHE.move_to_end(key)
            while len(_DEEP_STATS_CACHE) > _DEEP_STATS_CACHE_MAX:
                _DEEP_STATS_CACHE.popitem(last=False)
    return stats


@app.post("/api/traffic")
def get_traffic(request: Request, token: str = Body("", embed=True)):
    _require_browser_same_origin(request)
    # Serve traffic data — either from the historical CSV database or live from GitHub
    active_token = _get_token(token)
    if not active_token:
        raise HTTPException(status_code=401, detail="No token provided")

    ok, _ = _validate_token_cached(active_token)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid token")
    data_dir = os.environ.get("GITLYTICS_DATA_DIR")
    dfs = []
    if data_dir:
        data_dir_path = Path(data_dir)
        if not data_dir_path.exists():
            logger.warning(f"Data directory '{data_dir}' does not exist.")
        else:
            csv_files = sorted(
                (
                    f for f in data_dir_path.glob("traffic_*.csv")
                    if _SYNC_CSV_RE.match(f.name)
                ),
                key=lambda f: (len(f.stem), f.name),
            )
            if not csv_files:
                logger.warning(f"No traffic_*.csv files found in '{data_dir}'.")
            for f in csv_files:
                try:
                    frame = _read_csv_cached(f)
                except Exception as exc:
                    logger.warning(f"Skipping unreadable CSV '{f}': {exc}")
                    continue
                # A CSV without the required keys would blow up the whole
                # dashboard with a KeyError. Skip it with a warning instead
                # (AF2-LIB-02).
                missing = [c for c in ("date", "repository") if c not in frame.columns]
                if missing:
                    logger.warning(
                        f"Skipping CSV '{f}': missing required column(s) {missing}."
                    )
                    continue
                dfs.append(frame)

    try:
        live_df = _cached_live_traffic(active_token)
    except Exception as exc:
        logger.warning(f"Failed to fetch live traffic: {exc}")
        live_df = pd.DataFrame()

    if dfs:
        csv_df = pd.concat(dfs, ignore_index=True)
        if not live_df.empty:
            df = pd.concat([csv_df, live_df], ignore_index=True)
            if "date" in df.columns and "repository" in df.columns:
                # Coalesce overlapping (date, repository) rows so a partial live
                # outage (NaN views when clones succeeded) never masks stored CSV data.
                df = df.groupby(["date", "repository"], as_index=False, sort=False).last()
        else:
            df = csv_df
            if "date" in df.columns and "repository" in df.columns:
                df = df.drop_duplicates(subset=["date", "repository"], keep="last")
    else:
        df = live_df
        if not df.empty and "date" in df.columns and "repository" in df.columns:
            df = df.drop_duplicates(subset=["date", "repository"], keep="last")

    # Normalize `is_private` to a real bool. CSV round-trip + pd.concat
    # widens the dtype to object (mix of bool, "True"/"False" strings, and
    # NaN); without this, downstream code calling `bool("False")` would
    # label every public repo as private.
    if not df.empty and "is_private" in df.columns:
        df["is_private"] = df["is_private"].map(_to_bool).astype(bool)

    df = df.replace([float('inf'), float('-inf')], None).where(pd.notnull(df), None)

    # Build a quick view-sum map to find the top 20 repos for deep fetching.
    # Guard against the subset-metrics case where `views` may be absent.
    repos_with_views = []
    if (
        not df.empty
        and "repository" in df.columns
        and "views" in df.columns
    ):
        for repo_name, group in df.groupby("repository"):
            s = group["views"]
            if not pd.api.types.is_numeric_dtype(s):
                s = s.astype(str).str.replace(",", "", regex=False).str.strip()
            views_total = int(
                pd.to_numeric(s, errors="coerce").fillna(0).sum()
            )
            repos_with_views.append({"repository": repo_name, "total_views": views_total})

    # Fetch deep stats concurrently for the top 20 most-viewed repos
    deep_stats = {}
    if repos_with_views and active_token:
        deep_stats = _cached_deep_stats(active_token, repos_with_views, top_n=20)

    payload = build_react_payload(df, deep_stats=deep_stats)
    return payload


# Star history endpoint — sampled star timeline for the SPA chart.
# Accepts the token via Authorization: Bearer header (PAT) so the chart
# can hit its own backend without exposing the token to the cloud.
# There is no environment-token fallback: anyone who can reach this endpoint
# must supply their own token or the operator's PAT quota is exposed.
@app.get("/api/star-history")
def get_star_history_endpoint(
    owner: str,
    repo: str,
    response: Response,
    authorization: str | None = Header(default=None),
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization bearer token required.")
    # If an Authorization header is present but malformed (wrong scheme, empty
    # token, etc.), error out rather than silently falling back to a server token.
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Malformed Authorization header.")
    token = parts[1].strip()
    try:
        points = fetch_star_history(owner, repo, token)
    except GitHubRateLimitError as exc:
        # Surface rate-limit hits as 429 so the SPA shows the chart's rate-limit UI.
        raise HTTPException(status_code=429, detail=str(exc))
    except StargazersRestrictedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except StarHistoryFetchError as exc:
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            raise HTTPException(status_code=401, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        # fetch_star_history raises ValueError for empty owner / repo / trailing slash;
        # returning 400 is more honest than letting Starlette surface a 500.
        raise HTTPException(status_code=400, detail=str(exc))
    response.headers["Cache-Control"] = "private, no-cache"
    return {"owner": owner, "repo": repo, "points": points}


@app.post("/api/upload-csv")
def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(_require_upload_access),
):
    # Uploads are previews: they are parsed for this response and discarded.
    # Persisting them under a ``traffic_*.csv`` name made every upload a
    # permanent part of every future /api/traffic response (unbounded growth,
    # stale/duplicate rows, schema widening). Use a system temp file so the
    # 5 MB cap still applies without buffering the whole body in memory.
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="gitlytics_upload_", suffix=".csv")
        total = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"CSV too large. Maximum size is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                    )
                out.write(chunk)
        df = process_uploaded_csv(tmp_path)
        df = df.replace([float('inf'), float('-inf')], None).where(pd.notnull(df), None)
        return build_react_payload(df, deep_stats=None)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("CSV upload failed: %s", e)
        raise HTTPException(status_code=400, detail="Failed to process CSV upload.")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# Static file serving
frontend_dir = Path(__file__).parent / "static"
assets_dir = frontend_dir / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/")
def serve_index():
    index_file = frontend_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        status_code=503,
        content={"error": "Dashboard assets not found in the package installation."}
    )


@app.get("/{full_path:path}")
def serve_spa_fallback(full_path: str):
    """SPA catch-all — returns index.html so React Router handles navigation."""
    # Any unhandled /api/* path is a real 404, not a SPA route.
    if full_path in ("api", "assets") or full_path.startswith(("api/", "assets/")):
        return JSONResponse(status_code=404, content={"error": "Not found."})

    # Confine the resolved path to frontend_dir — otherwise a request like
    # `GET /..%2F..%2Fapp.py` would let `asset_file` escape the static
    # directory and serve arbitrary files the worker can read.
    frontend_root = frontend_dir.resolve()
    try:
        asset_file = (frontend_dir / full_path).resolve()
    except (OSError, ValueError):
        # `resolve()` can raise on broken symlinks or odd encodings.
        return JSONResponse(status_code=404, content={"error": "Not found."})
    is_inside = asset_file == frontend_root or asset_file.is_relative_to(frontend_root)
    if is_inside and asset_file.is_file():
        return FileResponse(asset_file)

    index_file = frontend_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)

    return JSONResponse(status_code=404, content={"error": "Not found."})

