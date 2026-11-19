# Copyright (c) 2026 Ameya Sanjay Chopade
# SPDX-License-Identifier: LicenseRef-Apache-2.0-with-Commons-Clause
"""FastAPI application factory and ASGI instance for the local dashboard."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import router
from .security import _ALLOWED_ORIGINS, STATIC_DIR, _SecurityHeadersMiddleware


def create_app() -> FastAPI:
    """Create and configure the local Gitlytics dashboard FastAPI app."""
    application = FastAPI(title="GitHub Traffic API")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization"],
    )
    application.add_middleware(_SecurityHeadersMiddleware)

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        application.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    application.include_router(router)
    return application


app = create_app()
