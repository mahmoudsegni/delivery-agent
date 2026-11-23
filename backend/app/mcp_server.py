"""Read-only MCP server: delivery data for Claude/Cursor (issue #16).

Streamable HTTP only, mounted by app.main at /mcp. Every tool opens its own
short DB session through the same services the REST API uses — no new logic,
no writes. Vendors in this space (Linear, LinearB, Swarmia) all ship remote
MCP as the integration surface now; this is our thin version of it.
"""

import json
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.database import async_session_maker
from app.schemas.sprint import SprintResponse
from app.schemas.task import TaskResponse
from app.services.brief_service import BriefService
from app.services.forecast_service import forecast as run_forecast
from app.services.sprint_service import SprintNotFoundError, SprintService
from app.services.task_service import TaskService

server = MCPServer(
    name="delivery-agent",
    instructions=(
        "Read-only access to this team's delivery data: the live snapshot "
        "the morning brief is written from, the backlog, sprints, and Monte "
        "Carlo completion forecasts. Call delivery_snapshot first — it "
        "usually answers 'what's going on' in one shot."
    ),
)


@server.tool()
async def delivery_snapshot() -> str:
    """Everything the morning brief is written from, in one JSON blob:
    stream health, portfolio confidence + 7-day trend, active sprint,
    what shipped in the last day, AI-workforce stats, latest standup
    summary, and pending proposal counts."""
    async with async_session_maker() as db:
        return json.dumps(await BriefService(db).assemble_snapshot(), default=str)


@server.tool()
async def backlog(status: str | None = None, limit: int = 25) -> str:
    """List tasks, newest first. Optional status filter: backlog, todo,
    in_progress, review, done."""
    async with async_session_maker() as db:
        tasks, total = await TaskService(db).list_tasks(
            status_filter=status, limit=limit
        )
        return json.dumps(
            {
                "total": total,
                "tasks": [TaskResponse.model_validate(t).model_dump(mode="json") for t in tasks],
            }
        )


@server.tool()
async def forecast(sprint_id: str) -> str:
    """Monte Carlo completion odds for a sprint: p50/p85 days remaining,
    on-time probability, weekly throughput. Find sprint ids via sprints()."""
    try:
        sprint_uuid = UUID(sprint_id)
    except ValueError:
        return json.dumps({"error": "sprint_id must be a UUID"})
    async with async_session_maker() as db:
        try:
            return json.dumps(await run_forecast(db, sprint_uuid), default=str)
        except SprintNotFoundError:
            return json.dumps({"error": f"sprint {sprint_id} not found"})


@server.tool()
async def sprints() -> str:
    """List sprints with id, name, goal, dates and status."""
    async with async_session_maker() as db:
        rows, total = await SprintService(db).list_sprints()
        return json.dumps(
            {
                "total": total,
                "sprints": [SprintResponse.model_validate(s).model_dump(mode="json") for s in rows],
            }
        )


# ponytail: DNS-rebinding protection off — same posture as the REST API
# mounted around it (no host/origin checks there either); the passcode gate
# still covers /mcp on exposed deployments. Revisit if MCP goes public.
http_app = server.streamable_http_app(
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
