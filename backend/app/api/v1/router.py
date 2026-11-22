"""API v1 router aggregating all endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import agent, ws
from app.api.v1.autopilot import router as autopilot_router
from app.api.v1.briefs import router as briefs_router
from app.api.v1.coding import router as coding_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.exec import router as exec_router
from app.api.v1.features import router as features_router
from app.api.v1.portfolio import router as portfolio_router
from app.api.v1.sprints import router as sprints_router
from app.api.v1.search import router as search_router
from app.api.v1.standup import router as standup_router
from app.api.v1.streams import router as streams_router
from app.api.v1.tasks import router as tasks_router
from app.api.v1.team import router as team_router
from app.config import settings
from app.database import get_db
from app.dependencies import (
    PASSCODE_COOKIE,
    clear_passcode_failures,
    passcode_locked,
    passcode_ok,
    register_passcode_failure,
)
from app.models.sync_state import SyncState

router = APIRouter()

# Include sub-routers
router.include_router(team_router)
router.include_router(tasks_router)
router.include_router(sprints_router)
router.include_router(search_router)
router.include_router(features_router)
router.include_router(agent.router)
router.include_router(standup_router)
router.include_router(portfolio_router)
router.include_router(streams_router)
router.include_router(coding_router)
router.include_router(exec_router)
router.include_router(briefs_router)
router.include_router(autopilot_router)
router.include_router(dashboard_router)
router.include_router(ws.router)


@router.get("/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """
    Health check for monitoring, load balancers and compose healthchecks.

    200 only when the database answers; sync freshness is reported, never
    gated on (sync cadence is deployment-specific).
    """
    try:
        await db.execute(text("SELECT 1"))
    except Exception as e:
        return JSONResponse(
            {"status": "degraded", "db": "unreachable", "detail": str(e)[:200]},
            status_code=503,
        )
    row = (
        await db.execute(
            select(SyncState).order_by(SyncState.last_sync_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    sync = None
    if row is not None and row.last_sync_at is not None:
        ts = row.last_sync_at
        if ts.tzinfo is None:  # naive timestamps (SQLite/demo) are UTC
            ts = ts.replace(tzinfo=timezone.utc)
        sync = {
            "provider": row.provider,
            "last_sync_at": ts.isoformat(),
            "age_hours": round(
                (datetime.now(timezone.utc) - ts).total_seconds() / 3600, 1
            ),
            "last_status": row.last_status,
            "last_error": row.last_error,
        }
    return {"status": "ok", "db": "ok", "sync": sync}


# --- Optional passcode gate (issue #8) -------------------------------------
# Under /api/v1 so the page is reachable from both origins (Vite proxies /api).
_PASSCODE_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delivery Agent</title>
<style>body{font-family:system-ui;display:grid;place-items:center;height:100vh;background:#0f172a;color:#e2e8f0;margin:0}
form{display:flex;gap:8px;margin-top:16px}input{padding:10px 12px;border-radius:8px;border:1px solid #475569;background:#1e293b;color:inherit}
button{padding:10px 16px;border-radius:8px;border:0;background:#3b82f6;color:#fff;cursor:pointer}p{color:#94a3b8}p.err{color:#f87171}</style></head>
<body><div><h2>Delivery Agent</h2><p>This deployment is passcode-protected.</p>
<p class="err">{ERR}</p>
<form method="post" action="/api/v1/passcode">
<input type="password" name="passcode" placeholder="Passcode" autofocus required>
<button type="submit">Unlock</button></form></div></body></html>"""


@router.get("/passcode", response_class=HTMLResponse)
async def passcode_page() -> str:
    """Unlock page: form sets the da_passcode cookie the gate checks."""
    return _PASSCODE_PAGE.replace("{ERR}", "")


@router.post("/passcode")
async def passcode_submit(request: Request, passcode: str = Form("")):
    # Same rate limiter as the gate middleware: the form is the other
    # guessable path (issue #25).
    ip = request.client.host if request.client else "unknown"
    if passcode_locked(ip):
        return JSONResponse({"detail": "Too many attempts. Try again later."}, status_code=429)
    if passcode_ok(passcode):
        clear_passcode_failures(ip)
        resp = RedirectResponse("/", status_code=303)
        # ponytail: cookie holds the shared secret itself; signed sessions if
        # this ever protects more than a demo deployment.
        resp.set_cookie(
            PASSCODE_COOKIE,
            settings.passcode,
            httponly=True,
            samesite="lax",
            max_age=30 * 24 * 3600,
        )
        return resp
    register_passcode_failure(ip)
    return HTMLResponse(_PASSCODE_PAGE.replace("{ERR}", "Wrong passcode."), status_code=401)
