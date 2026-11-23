"""FastAPI application entry point."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import secrets

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import router as v1_router
from app.config import settings
from app.database import async_engine
from app.dependencies import (
    PASSCODE_COOKIE,
    clear_passcode_failures,
    passcode_locked,
    passcode_ok,
    register_passcode_failure,
)
from app import mcp_server  # module: mcp_server.server / mcp_server.http_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan handler.

    Manages startup and shutdown events, including database engine cleanup.
    """
    # Startup
    async with mcp_server.server.session_manager.run():
        yield
    # Shutdown
    await async_engine.dispose()


app = FastAPI(
    title="Delivery Agent",
    description="AI-powered delivery agent for smart sprint planning and team coordination",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",  # Alternative dev port
    ],
    # Azure DevOps serves packaged extension content (our hub iframe) from the
    # extension gallery CDN, e.g. https://<publisher>.gallerycdn.vsassets.io.
    # Allow that origin so the embedded hub can call this backend cross-origin.
    # Confirm the exact origin from the browser Network tab once sideloaded and
    # tighten this regex if needed.
    allow_origin_regex=r"https://.*\.(vsassets\.io|visualstudio\.com)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API v1 router
app.include_router(v1_router, prefix="/api/v1")

# Read-only MCP endpoint for Claude/Cursor (#16): POST /mcp.
# Mounted at "/" AFTER all API routes, with the transport's own path at
# "/mcp" — the exact URL MCP clients expect, no redirect dance.
# ponytail: no MCP auth story yet — the passcode gate above covers /mcp on
# exposed deployments (MCP clients can't do the cookie unlock), so MCP works
# where PASSCODE is unset (local/self-host). Add token auth if it needs to
# go public.
app.mount("/", mcp_server.http_app)


# Optional passcode gate for exposed deployments (issue #8). Off unless
# PASSCODE is set. Exempt: the unlock page itself, health probes, and CORS
# preflights (browsers send neither cookies nor custom headers on OPTIONS).
@app.middleware("http")
async def passcode_gate(request: Request, call_next):
    path = request.url.path
    if settings.passcode and request.method != "OPTIONS" and not path.startswith(
        ("/api/v1/passcode", "/api/v1/health")
    ):
        ip = request.client.host if request.client else "unknown"
        if passcode_locked(ip):
            return JSONResponse(
                {"detail": "Too many attempts. Try again later."}, status_code=429
            )
        # Calendar apps can't do the cookie unlock: a shared ICS_TOKEN in the
        # query string exempts the feed. Empty ics_token = no exemption.
        if (
            settings.ics_token
            and path == "/api/v1/team/calendar.ics"
            and secrets.compare_digest(
                request.query_params.get("token", ""), settings.ics_token
            )
        ):
            return await call_next(request)
        if not passcode_ok(
            request.cookies.get(PASSCODE_COOKIE) or request.headers.get("X-Passcode")
        ):
            register_passcode_failure(ip)
            return JSONResponse(
                {"detail": "Passcode required. Unlock at /api/v1/passcode or send X-Passcode."},
                status_code=401,
            )
        clear_passcode_failures(ip)
    return await call_next(request)
