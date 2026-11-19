# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Optional local FastAPI dashboard server (`pip install gitlytics[dashboard]`)."""

from .app import app, create_app
from .cache import (
    _AUTH_CACHE_MAX,
    _AUTH_CACHE_TTL,
    _AUTH_NEG_TTL,
    _CSV_CACHE,
    _CSV_CACHE_LOCK,
    _DEEP_STATS_CACHE,
    _DEEP_STATS_CACHE_LOCK,
    _DEEP_STATS_CACHE_MAX,
    _DEEP_STATS_CACHE_TTL,
    _TRAFFIC_CACHE,
    _TRAFFIC_CACHE_LOCK,
    _TRAFFIC_CACHE_MAX,
    _TRAFFIC_CACHE_TTL,
    _auth_cache,
    _auth_cache_lock,
    _cached_deep_stats,
    _cached_live_traffic,
    _normalize_csv_header,
    _read_csv_cached,
    _validate_token_cached,
)
from .routes import (
    _MAX_UPLOAD_BYTES,
    _SYNC_CSV_RE,
    _fetch_username_data,
    auth,
    get_config,
    get_star_history_endpoint,
    get_traffic,
    get_username_data,
    get_username_data_by_path,
    serve_index,
    serve_spa_fallback,
    upload_csv,
)
from .security import (
    _ALLOWED_ORIGINS,
    STATIC_DIR,
    _env_token_allowed,
    _get_token,
    _parse_static_security_headers,
    _require_browser_same_origin,
    _require_upload_access,
    _SecurityHeadersMiddleware,
    _static_security_headers,
)

__all__ = [
    "STATIC_DIR",
    "app",
    "create_app",
]
