"""
gitlytics/api.py
Powers the FastAPI backend — serves traffic data and the React dashboard to the browser.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import threading as _threading
import time as _time
import uuid
from collections import OrderedDict
from pathlib import Path

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
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class _SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                ])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(_SecurityHeadersMiddleware)


def _require_upload_access(
    request: Request,
    x_upload_key: str | None = Header(default=None),
) -> None:
    client_host = (request.client.host if request.client else "").lower()
    if client_host in ("127.0.0.1", "::1", "testclient", "testserver"):
        return
    expected = (os.environ.get("GITLYTICS_UPLOAD_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="Upload not allowed from this host.")
    got = (x_upload_key or "").strip()
    if got and secrets.compare_digest(got, expected):
        return
    raise HTTPException(status_code=403, detail="Upload not allowed from this host.")


def _get_token(token: str = None) -> str:
    # C-2: explicit empty string must not fall through to the env token
    if token and token.strip():
        return token.strip()
    return os.environ.get("GITLYTICS_TOKEN")


def _validate_token_cached(token: str):
    # M-1: cache validation results to avoid a double HTTP round-trip on every /api/traffic call
    key = hashlib.sha256(token.encode()).hexdigest()[:16]
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
        "has_token": bool(os.environ.get("GITLYTICS_TOKEN")),
        "has_data_dir": bool(os.environ.get("GITLYTICS_DATA_DIR"))
    }


@app.post("/api/auth")
def auth(token: str = Body("", embed=True)):
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


def _fetch_username_data(username: str):
    if not username or not username.strip():
        raise HTTPException(status_code=400, detail="Username is required.")
    try:
        clean = validate_github_username(username.strip())
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
def get_username_data_by_path(username: str):
    return _fetch_username_data(username)


@app.post("/api/username")
def get_username_data(username: str = Body("", embed=True)):
    return _fetch_username_data(username)


@app.post("/api/traffic")
def get_traffic(token: str = Body("", embed=True)):
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
            csv_files = list(data_dir_path.glob("traffic_*.csv"))
            if not csv_files:
                logger.warning(f"No traffic_*.csv files found in '{data_dir}'.")
            for f in csv_files:
                try:
                    dfs.append(pd.read_csv(f))
                except Exception as exc:
                    logger.warning(f"Skipping unreadable CSV '{f}': {exc}")

    try:
        live_df = fetch_traffic_data(active_token)
    except Exception as exc:
        logger.warning(f"Failed to fetch live traffic: {exc}")
        live_df = pd.DataFrame()

    if dfs:
        csv_df = pd.concat(dfs, ignore_index=True)
        if not live_df.empty:
            df = pd.concat([csv_df, live_df], ignore_index=True)
        else:
            df = csv_df
    else:
        df = live_df

    if not df.empty:
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
            repos_with_views.append({"repository": repo_name, "total_views": int(group["views"].sum())})

    # Fetch deep stats concurrently for the top 20 most-viewed repos
    deep_stats = {}
    if repos_with_views and active_token:
        deep_stats = fetch_deep_stats_for_top(active_token, repos_with_views, top_n=20)

    payload = build_react_payload(df, deep_stats=deep_stats)
    return payload


# Star history endpoint — sampled star timeline for the SPA chart.
# Accepts the token via Authorization: Bearer header (PAT) so the chart
# can hit its own backend without exposing the token to the cloud.
# Falls back to GITLYTICS_TOKEN env so the local widget still works
# for users who stored their PAT in env instead of the LoginView.
@app.get("/api/star-history")
def get_star_history_endpoint(
    owner: str,
    repo: str,
    response: Response,
    authorization: str | None = Header(default=None),
):
    # If an Authorization header is present but malformed (wrong scheme, empty
    # token, etc.), error out rather than silently falling back to the server's
    # own GITLYTICS_TOKEN — anyone who can reach this endpoint must not be able
    # to burn the operator's PAT quota with a spoofed header.
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise HTTPException(status_code=401, detail="Malformed Authorization header.")
        token = parts[1].strip()
    else:
        token = os.environ.get("GITLYTICS_TOKEN")
    try:
        points = fetch_star_history(owner, repo, token)
    except GitHubRateLimitError as exc:
        # Surface rate-limit hits as 429 so the SPA shows the chart's rate-limit UI.
        raise HTTPException(status_code=429, detail=str(exc))
    except StargazersRestrictedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except StarHistoryFetchError as exc:
        # Validation / metadata / network failures are caller errors — 400 makes more
        # sense than 429 here so the SPA can distinguish "GitHub is throttling us"
        # from "your request was malformed".
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        # fetch_star_history raises ValueError for empty owner / repo / trailing slash;
        # returning 400 is more honest than letting Starlette surface a 500.
        raise HTTPException(status_code=400, detail=str(exc))
    response.headers["Cache-Control"] = "public, max-age=86400"
    return {"owner": owner, "repo": repo, "points": points}


@app.post("/api/upload-csv")
def upload_csv(
    request: Request,
    file: UploadFile = File(...),
    _: None = Depends(_require_upload_access),
):
    try:
        data_dir = os.environ.get("GITLYTICS_DATA_DIR")
        if data_dir:
            data_dir_path = Path(data_dir)
            data_dir_path.mkdir(parents=True, exist_ok=True)
            # Stream the upload to disk so we don't buffer the whole file in
            # memory. Enforce the size cap on the way in.
            dest = data_dir_path / f"traffic_uploaded_{uuid.uuid4().hex}.csv"
            total = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = file.file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_UPLOAD_BYTES:
                        out.close()
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        raise HTTPException(
                            status_code=413,
                            detail=f"CSV too large. Maximum size is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                        )
                    out.write(chunk)
            file.file.seek(0)
            upload_stream = file.file
        else:
            # No data_dir configured — the common case for `gitlytics dashboard`
            # without persistence. The 25 MB cap MUST still be enforced here,
            # otherwise an unbounded upload is fully buffered into memory before
            # pd.read_csv runs (DoS / OOM). Read into a bounded BytesIO so the
            # size check happens before any parsing.
            import io
            buf = io.BytesIO()
            total = 0
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
                buf.write(chunk)
            buf.seek(0)
            upload_stream = buf
        df = process_uploaded_csv(upload_stream)
        df = df.replace([float('inf'), float('-inf')], None).where(pd.notnull(df), None)
        payload = build_react_payload(df, deep_stats=None)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("CSV upload failed: %s", e)
        raise HTTPException(status_code=400, detail="Failed to process CSV upload.")


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
    if full_path.startswith("api/") or full_path.startswith("assets/"):
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

