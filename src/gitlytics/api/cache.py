# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Thread-safe bounded TTL/LRU caches for the local dashboard server."""

from __future__ import annotations

import hashlib
import threading as _threading
import time as _time
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from gitlytics.core.repos import fetch_deep_stats_for_top
from gitlytics.core.traffic import fetch_traffic_data
from gitlytics.core.users import validate_token
from gitlytics.io.csv import normalize_csv_dataframe

# Token validation cache: positive results cache 5m, failures 10s so blips recover fast.
_auth_cache: "OrderedDict[str, tuple]" = OrderedDict()
_auth_cache_lock = _threading.Lock()
_AUTH_CACHE_TTL = 300
_AUTH_NEG_TTL = 10
_AUTH_CACHE_MAX = 256

# Historical CSV cache keyed by (resolved path, mtime) with mtime_ns + size verification.
_CSV_CACHE: "OrderedDict[tuple[str, float], tuple[int, int, pd.DataFrame]]" = (
    OrderedDict()
)
_CSV_CACHE_LOCK = _threading.Lock()

# Short-lived caches for live GitHub traffic and top-20 deep repository stats.
_TRAFFIC_CACHE: "OrderedDict[str, tuple[float, pd.DataFrame]]" = OrderedDict()
_TRAFFIC_CACHE_LOCK = _threading.Lock()
_TRAFFIC_CACHE_TTL = 120.0
_TRAFFIC_CACHE_MAX = 16

_DEEP_STATS_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_DEEP_STATS_CACHE_LOCK = _threading.Lock()
_DEEP_STATS_CACHE_TTL = 300.0
_DEEP_STATS_CACHE_MAX = 16


def _normalize_csv_header(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize CSV column headers using the shared io.csv helper."""
    return normalize_csv_dataframe(df)


def _validate_token_cached(token: str) -> tuple[bool, str]:
    """Validate a GitHub token with bounded thread-safe LRU/TTL caching."""
    key = hashlib.sha256(token.encode()).hexdigest()
    now = _time.time()
    with _auth_cache_lock:
        cached = _auth_cache.get(key)
        if cached is not None:
            valid, username, expires = cached
            if now < expires:
                _auth_cache.move_to_end(key)
                return valid, username

    valid, username = validate_token(token)
    ttl = _AUTH_CACHE_TTL if valid else _AUTH_NEG_TTL
    with _auth_cache_lock:
        _auth_cache[key] = (valid, username, now + ttl)
        _auth_cache.move_to_end(key)
        while len(_auth_cache) > _AUTH_CACHE_MAX:
            _auth_cache.popitem(last=False)
    return valid, username


def _read_csv_cached(path: Path) -> pd.DataFrame:
    """Read and normalize a CSV archive file, reusing cached frames when unchanged on disk."""
    try:
        st = path.stat()
    except OSError:
        return _normalize_csv_header(pd.read_csv(path))

    key = (str(path.resolve()), st.st_mtime)
    with _CSV_CACHE_LOCK:
        cached = _CSV_CACHE.get(key)
        if cached is not None:
            c_ns, c_size, c_df = cached
            if c_ns == st.st_mtime_ns and c_size == st.st_size:
                _CSV_CACHE.move_to_end(key)
                return c_df.copy()

    df = _normalize_csv_header(pd.read_csv(path))
    with _CSV_CACHE_LOCK:
        _CSV_CACHE[key] = (st.st_mtime_ns, st.st_size, df.copy())
        _CSV_CACHE.move_to_end(key)
        while len(_CSV_CACHE) > 128:
            _CSV_CACHE.popitem(last=False)
    return df


def _cached_live_traffic(token: str) -> pd.DataFrame:
    """Fetch live traffic for a token with a short TTL cache; never caches empty frames."""
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


def _cached_deep_stats(
    token: str, repos_with_views: list[dict], top_n: int = 20
) -> dict:
    """Cache concurrent deep-stats results for the top N viewed repositories."""
    top_names = tuple(
        r["repository"]
        for r in sorted(
            repos_with_views, key=lambda x: x.get("total_views", 0), reverse=True
        )[:top_n]
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
