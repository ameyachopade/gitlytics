# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""HTTP API endpoints and SPA fallback routes for the local dashboard server."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from gitlytics.core.stars import fetch_star_history
from gitlytics.core.users import (
    get_public_repos,
    get_public_user,
    get_user_profile,
    validate_token,
)
from gitlytics.core.validation import validate_github_username
from gitlytics.exceptions import (
    GitHubRateLimitError,
    StargazersRestrictedError,
    StarHistoryFetchError,
    TokenExpiredError,
)
from gitlytics.io.csv import process_uploaded_csv
from gitlytics.io.payloads import _to_bool, build_react_payload

from . import cache
from .security import (
    STATIC_DIR,
    _env_token_allowed,
    _get_token,
    _require_browser_same_origin,
    _require_upload_access,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Matches the 5 MB upload ceiling enforced by the hosted backend and docs.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_SYNC_CSV_RE = re.compile(r"^traffic_\d{4}(?:-\d{2})?\.csv$")


@router.get("/api/config")
def get_config():
    """Report whether the local server has a pre-configured token or CSV data directory."""
    return {
        "has_token": bool(os.environ.get("GITLYTICS_TOKEN")) and _env_token_allowed(),
        "has_data_dir": bool(os.environ.get("GITLYTICS_DATA_DIR")),
    }


@router.post("/api/auth")
def auth(request: Request, token: str = Body("", embed=True)):
    """Validate a GitHub token and return the authenticated user's profile."""
    _require_browser_same_origin(request)
    active_token = _get_token(token)
    if not active_token:
        raise HTTPException(
            status_code=401, detail="No token provided and no environment token found."
        )

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


def _fetch_username_data(username: str, token: str | None = None) -> dict:
    """Validate a GitHub username and fetch its public profile and repositories."""
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
        raise HTTPException(
            status_code=502, detail="GitHub is temporarily unavailable."
        )
    except Exception as exc:
        logger.warning("Username fetch failed for %s: %s", username, exc)
        raise HTTPException(status_code=500, detail="Failed to fetch GitHub data.")


@router.get("/api/username/{username}")
def get_username_data_by_path(
    request: Request,
    username: str,
    authorization: str | None = Header(default=None),
):
    """Fetch public profile and repos by URL path parameter."""
    _require_browser_same_origin(request)
    tok = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        tok = (
            parts[1].strip()
            if len(parts) == 2 and parts[0].lower() == "bearer"
            else authorization.strip()
        )
    return _fetch_username_data(username, token=tok)


@router.post("/api/username")
def get_username_data(
    request: Request,
    username: str = Body("", embed=True),
    token: str = Body("", embed=True),
    authorization: str | None = Header(default=None),
):
    """Fetch public profile and repos from a JSON POST payload."""
    _require_browser_same_origin(request)
    tok = (token or "").strip() or None
    if not tok and authorization:
        parts = authorization.strip().split(None, 1)
        tok = (
            parts[1].strip()
            if len(parts) == 2 and parts[0].lower() == "bearer"
            else authorization.strip()
        )
    return _fetch_username_data(username, token=tok)


@router.post("/api/traffic")
def get_traffic(request: Request, token: str = Body("", embed=True)):
    """Serve merged historical CSV archives and live GitHub traffic for the dashboard."""
    _require_browser_same_origin(request)
    active_token = _get_token(token)
    if not active_token:
        raise HTTPException(status_code=401, detail="No token provided")

    ok, auth_username = cache._validate_token_cached(active_token)
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
                    f
                    for f in data_dir_path.glob("traffic_*.csv")
                    if _SYNC_CSV_RE.match(f.name)
                ),
                key=lambda f: (len(f.stem), f.name),
            )
            if not csv_files:
                logger.warning(f"No traffic_*.csv files found in '{data_dir}'.")
            for csv_file in csv_files:
                try:
                    frame = cache._read_csv_cached(csv_file)
                except Exception as exc:
                    logger.warning(f"Skipping unreadable CSV '{csv_file}': {exc}")
                    continue
                missing = [c for c in ("date", "repository") if c not in frame.columns]
                if missing:
                    logger.warning(
                        f"Skipping CSV '{csv_file}': missing required column(s) {missing}."
                    )
                    continue
                dfs.append(frame)

    import pandas as pd

    try:
        live_df = cache._cached_live_traffic(active_token)
    except TokenExpiredError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    except Exception as exc:
        logger.warning(f"Failed to fetch live traffic: {exc}")
        live_df = pd.DataFrame()

    # Coalesce historical CSV rows with live rows, scoped to the authenticated caller.
    if dfs:
        csv_df = pd.concat(dfs, ignore_index=True)
        caller_login = (auth_username or "").strip().lower()
        if caller_login and "repository" in csv_df.columns:
            live_repos = (
                set(live_df["repository"].dropna().astype(str).str.lower())
                if not live_df.empty and "repository" in live_df.columns
                else set()
            )
            repo_col = csv_df["repository"].fillna("").astype(str)
            owner_match = (
                repo_col.str.split("/", n=1).str[0].str.lower() == caller_login
            )
            live_match = repo_col.str.lower().isin(live_repos) if live_repos else False
            csv_df = csv_df[owner_match | live_match]
        if not live_df.empty:
            df = pd.concat([csv_df, live_df], ignore_index=True)
            if "date" in df.columns and "repository" in df.columns:
                df = df.groupby(
                    ["date", "repository"], as_index=False, sort=False
                ).last()
        else:
            df = csv_df
            if "date" in df.columns and "repository" in df.columns:
                df = df.drop_duplicates(subset=["date", "repository"], keep="last")
    else:
        df = live_df
        if not df.empty and "date" in df.columns and "repository" in df.columns:
            df = df.drop_duplicates(subset=["date", "repository"], keep="last")

    if not df.empty and "is_private" in df.columns:
        df["is_private"] = df["is_private"].map(_to_bool).astype(bool)

    df = df.replace([float("inf"), float("-inf")], None).where(pd.notnull(df), None)

    repos_with_views = []
    if not df.empty and "repository" in df.columns and "views" in df.columns:
        for repo_name, group in df.groupby("repository"):
            series = group["views"]
            if not pd.api.types.is_numeric_dtype(series):
                series = (
                    series.astype(str).str.replace(",", "", regex=False).str.strip()
                )
            views_total = int(pd.to_numeric(series, errors="coerce").fillna(0).sum())
            repos_with_views.append(
                {"repository": repo_name, "total_views": views_total}
            )

    deep_stats = {}
    if repos_with_views and active_token:
        deep_stats = cache._cached_deep_stats(active_token, repos_with_views, top_n=20)

    return build_react_payload(df, deep_stats=deep_stats)


@router.get("/api/star-history")
def get_star_history_endpoint(
    owner: str,
    repo: str,
    response: Response,
    authorization: str | None = Header(default=None),
):
    """Fetch cumulative star history using the caller's explicit Bearer token."""
    if not authorization:
        raise HTTPException(
            status_code=401, detail="Authorization bearer token required."
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="Malformed Authorization header.")
    token = parts[1].strip()

    try:
        points = fetch_star_history(owner, repo, token)
    except GitHubRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except StargazersRestrictedError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except StarHistoryFetchError as exc:
        msg = str(exc).lower()
        if "401" in msg or "unauthorized" in msg:
            raise HTTPException(status_code=401, detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    response.headers["Cache-Control"] = "private, no-cache"
    return {"owner": owner, "repo": repo, "points": points}


# Allow small multipart framing overhead above the 5 MB CSV limit.
_MAX_MULTIPART_BODY_BYTES = _MAX_UPLOAD_BYTES + 65536


@router.post("/api/upload-csv")
async def upload_csv(
    request: Request,
    _: None = Depends(_require_upload_access),
):
    """Parse an uploaded CSV preview in a temp file and return its dashboard payload."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_MULTIPART_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"CSV too large. Maximum size is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
        except ValueError:
            pass

    # Buffer the raw request stream up to the size cap before parsing multipart data.
    body_buf = bytearray()
    async for chunk in request.stream():
        body_buf.extend(chunk)
        if len(body_buf) > _MAX_MULTIPART_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"CSV too large. Maximum size is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
    request._body = bytes(body_buf)

    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "file"):
        raise HTTPException(status_code=422, detail="Missing 'file' upload field.")

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
        import pandas as pd

        df = process_uploaded_csv(tmp_path)
        df = df.replace([float("inf"), float("-inf")], None).where(pd.notnull(df), None)
        return build_react_payload(df, deep_stats=None)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("CSV upload failed: %s", exc)
        raise HTTPException(status_code=400, detail="Failed to process CSV upload.")
    finally:
        await form.close()
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@router.get("/")
def serve_index():
    """Serve the bundled React dashboard index.html shell."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        status_code=503,
        content={"error": "Dashboard assets not found in the package installation."},
    )


@router.get("/{full_path:path}")
def serve_spa_fallback(full_path: str):
    """Serve static root files safely or fall back to index.html for React Router."""
    if full_path in ("api", "assets") or full_path.startswith(("api/", "assets/")):
        return JSONResponse(status_code=404, content={"error": "Not found."})

    # Block dotfiles and internal static config files such as _headers.
    if any(part.startswith((".", "_")) for part in Path(full_path).parts):
        return JSONResponse(status_code=404, content={"error": "Not found."})

    # Confine resolved paths to STATIC_DIR so traversal attempts cannot escape.
    frontend_root = STATIC_DIR.resolve()
    try:
        asset_file = (STATIC_DIR / full_path).resolve()
    except (OSError, ValueError):
        return JSONResponse(status_code=404, content={"error": "Not found."})

    if (
        asset_file == frontend_root or asset_file.is_relative_to(frontend_root)
    ) and asset_file.is_file():
        return FileResponse(asset_file)

    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(status_code=404, content={"error": "Not found."})
