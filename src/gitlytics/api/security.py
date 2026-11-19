# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""Security headers middleware, same-origin checks, upload auth, and token resolution."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Header, HTTPException, Request

# Resolves to src/gitlytics/static regardless of working directory.
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# Only local browser origins are allowed for CORS on the local dashboard server.
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


def _parse_static_security_headers(headers_file: Path) -> dict[str, str]:
    """Parse global /* security headers from the packaged _headers file."""
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
            in_global_block = line.strip() == "/*"
            continue
        if not in_global_block or ":" not in line:
            continue
        name, _, value = line.strip().partition(":")
        name = name.strip()
        value = value.strip()
        if not name or name.lower() in ("cache-control", "strict-transport-security"):
            continue
        parsed[name] = value
    return parsed


def _static_security_headers() -> list[tuple[bytes, bytes]]:
    """Build the default + CSP security headers emitted on every local response."""
    headers: dict[str, str] = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
    }
    headers.update(_parse_static_security_headers(STATIC_DIR / "_headers"))
    return [
        (name.lower().encode("latin-1"), value.encode("latin-1", errors="replace"))
        for name, value in headers.items()
    ]


class _SecurityHeadersMiddleware:
    """ASGI middleware that attaches baseline security and CSP headers to HTTP responses."""

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


_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "testserver"}


def _is_trusted_host(host: str) -> bool:
    """Allow only loopback hosts, Starlette testserver, or explicit GITLYTICS_ALLOWED_HOSTS."""
    raw = (host or "").strip().lower()
    if not raw:
        return False
    extra = {
        h.strip().lower()
        for h in (os.environ.get("GITLYTICS_ALLOWED_HOSTS") or "").split(",")
        if h.strip()
    }
    if raw in extra:
        return True
    try:
        parsed = urlsplit(f"http://{raw}")
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return hostname in _LOOPBACK_HOSTNAMES or hostname in extra


def _is_same_host_origin(origin: str, host: str) -> bool:
    """Return True when an Origin or Referer URL matches a trusted request Host header."""
    if not _is_trusted_host(host):
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.netloc)
        and bool(host)
        and parsed.netloc.lower() == host.lower()
    )


def _require_upload_access(
    request: Request,
    x_upload_key: str | None = Header(default=None),
) -> None:
    """Enforce GITLYTICS_UPLOAD_KEY when configured, otherwise require a same-origin browser tab."""
    expected = (os.environ.get("GITLYTICS_UPLOAD_KEY") or "").strip()
    if expected:
        got = (x_upload_key or "").strip()
        if got and secrets.compare_digest(got, expected):
            return
        raise HTTPException(
            status_code=403,
            detail="Upload requires a valid upload key.",
        )

    headers = getattr(request, "headers", None) or {}
    origin = headers.get("origin") or headers.get("referer")
    if origin and _is_same_host_origin(origin, headers.get("host") or ""):
        return

    raise HTTPException(
        status_code=403,
        detail="Upload requires a same-origin request or a valid upload key.",
    )


def _require_browser_same_origin(request: Request) -> None:
    """Reject cross-origin browser requests and non-JSON browser POSTs."""
    headers = getattr(request, "headers", None) or {}
    origin = headers.get("origin") or headers.get("referer")
    if origin:
        if not _is_same_host_origin(origin, headers.get("host") or ""):
            raise HTTPException(status_code=403, detail="Cross-origin request rejected.")
        if request.method == "POST":
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise HTTPException(
                    status_code=415,
                    detail="Unsupported Content-Type; application/json required.",
                )


def _env_token_allowed() -> bool:
    """Return True only when serve_dashboard opted in to loopback env-token fallback."""
    return (os.environ.get("GITLYTICS_ALLOW_ENV_TOKEN") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _get_token(token: str = None) -> str | None:
    """Resolve the active GitHub token, preferring explicit request tokens over env fallback."""
    if token and token.strip():
        return token.strip()
    if _env_token_allowed():
        return os.environ.get("GITLYTICS_TOKEN")
    return None
