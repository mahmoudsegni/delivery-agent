"""Linear task provider.

Maps the generic TaskProvider contract onto a Linear workspace (GraphQL):

- external_id        -> issue identifier (e.g. "ENG-42"); the node uuid is
                        kept internally for mutations
- sprint             -> issue cycle (name = cycle name, path = "linear:cycle:<id>")
- board status       -> state.name checked against REVIEW_STATUSES first, then
                        state.type: backlog->backlog, unstarted->todo,
                        started->in_progress, completed/canceled->done
- priority           -> Linear ints: 1 Urgent->critical, 2 High->high,
                        3 Medium->medium, 4 Low->low; 0/unset -> medium
- story points       -> issue.estimate (Linear's estimate field, when configured)
- project/team       -> team key (identifier prefix, e.g. "ENG" from ENG-42)
- tags               -> label names joined with ","

Writes (create/update) respect the same read_only dial as the other
providers: PUSH_ENABLED off means this provider refuses to mutate the
workspace. Status changes pick a workflow state of the team: "review"
prefers a state whose name is in REVIEW_STATUSES, every other target
matches on state.type.
"""

import asyncio
import logging
from datetime import datetime
from typing import Literal

import httpx

from app.integrations.task_provider import (
    ExternalSprint,
    ExternalTask,
    ExternalTaskCreate,
    ExternalTaskUpdate,
    ExternalTeamMember,
    RateLimitError,
    ReadOnlyProviderError,
    TaskPage,
    TaskProvider,
    TaskProviderTimeoutError,
)

logger = logging.getLogger(__name__)

API_URL = "https://api.linear.app/graphql"

# Workflow state names that mean "review" whatever their type says.
DEFAULT_REVIEW_STATUSES = ["In Review", "Review", "Code Review"]

_STATE_TYPE: dict[str, Literal["backlog", "todo", "in_progress", "done"]] = {
    "backlog": "backlog",
    "unstarted": "todo",
    "started": "in_progress",
    "completed": "done",
    "canceled": "done",  # ponytail: closed counts as done; split out if the board ever needs dropped-vs-delivered
}
# Board status -> Linear WorkflowState.type to move into.
_TARGET_TYPE: dict[str, str] = {
    "backlog": "backlog",
    "todo": "unstarted",
    "in_progress": "started",
    "review": "started",
    "done": "completed",
}
_LINEAR_PRIORITY: dict[int, Literal["critical", "high", "medium", "low"]] = {
    1: "critical",
    2: "high",
    3: "medium",
    4: "low",
}
_OUR_PRIORITY_TO_LINEAR: dict[str, int] = {
    "critical": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}

# One node shape, shared by the list query, the single-issue lookup and the
# mutation return values so the mapper never sees two formats.
_NODE_FIELDS = """
  id identifier title description priority updatedAt estimate
  state { name type }
  assignee { id name }
  team { id key }
  cycle { id name startsAt endsAt }
  labels { nodes { name } }
"""

_ISSUES_QUERY = """
query($after: String, $first: Int) {
  issues(after: $after, first: $first, orderBy: updatedAt) {
    pageInfo { hasNextPage endCursor }
    nodes {""" + _NODE_FIELDS + """}
  }
}
"""

_ISSUE_QUERY = """
query($identifier: String!) {
  issues(first: 1, filter: { identifier: { eq: $identifier } }) {
    nodes {""" + _NODE_FIELDS + """}
  }
}
"""

_TEAM_QUERY = """
query($key: String!) { team(id: $key) { id key name } }
"""

_STATES_QUERY = """
query($teamId: String!) {
  workflowStates(first: 50, filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name type }
  }
}
"""

_CYCLES_QUERY = """
query($teamId: String!) {
  cycles(first: 50, filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name startsAt endsAt }
  }
}
"""

_USERS_QUERY = """
query($name: String!) {
  users(first: 10, filter: { name: { icontains: $name } }) { nodes { id name active } }
}
"""

_CREATE_MUTATION = """
mutation($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue {""" + _NODE_FIELDS + """} }
}
"""

_UPDATE_MUTATION = """
mutation($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue {""" + _NODE_FIELDS + """} }
}
"""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)  # py3.11+ parses trailing "Z"
    except ValueError:
        return None


def _pick_state(
    states: list[dict],
    target: str,
    review_statuses: list[str],
) -> dict | None:
    """Pick the workflow state that represents ``target``.

    "review" prefers a state whose name is one of the configured review
    statuses (same names the read side trusts); every other target matches
    on state.type.
    """
    if target == "review":
        for s in states:
            if s.get("name") in review_statuses:
                return s
    wanted = _TARGET_TYPE[target]
    for s in states:
        if s.get("type") == wanted:
            return s
    return None


class LinearTaskProvider(TaskProvider):
    """TaskProvider backed by Linear (GraphQL, API-key auth)."""

    def __init__(
        self,
        api_key: str,
        team_key: str = "",
        review_statuses: list[str] | None = None,
        read_only: bool = False,
        timeout: float = 20.0,
    ):
        if not api_key:
            raise ValueError("linear provider needs an API key (LINEAR_API_KEY)")
        self.api_key = api_key
        self.team_key = team_key.strip()
        self.review_statuses = review_statuses or DEFAULT_REVIEW_STATUSES
        self.read_only = read_only
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    async def _gql(self, query: str, variables: dict) -> httpx.Response:
        # ponytail: one retry with backoff on 5xx; a real retry queue belongs
        # to the sync job, not the provider.
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    base_url=API_URL,
                    headers={"Authorization": self.api_key},
                    timeout=self.timeout,
                ) as client:
                    resp = await client.post("", json={"query": query, "variables": variables})
            except httpx.TimeoutException as exc:
                raise TaskProviderTimeoutError(str(exc)) from exc
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After") or "60"
                try:
                    wait = max(1, int(retry_after))
                except ValueError:
                    wait = 60
                raise RateLimitError("Linear rate limit exceeded", retry_after=wait)
            if resp.status_code < 500 or attempt == 1:
                return resp
            await asyncio.sleep(1.5)
        raise AssertionError("unreachable: loop returns on attempt 2")

    async def _data(self, query: str, variables: dict) -> dict:
        """POST the query and return its data object, raising on GraphQL errors."""
        resp = await self._gql(query, variables)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors") and not body.get("data"):
            raise ValueError(f"Linear rejected query: {body['errors'][0].get('message')}")
        return body.get("data") or {}

    def _check_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyProviderError(
                "Linear provider is read-only (PUSH_ENABLED off); "
                "refusing to create/update issues in Linear"
            )

    # -- mapping ----------------------------------------------------------

    def _map_status(self, node: dict) -> Literal["backlog", "todo", "in_progress", "review", "done"]:
        state = node.get("state") or {}
        if state.get("name") in self.review_statuses:
            return "review"
        return _STATE_TYPE.get(state.get("type") or "", "backlog")

    def _map_issue(self, node: dict) -> ExternalTask:
        identifier = node["identifier"]
        cycle = node.get("cycle")

        return ExternalTask(
            external_id=identifier,
            title=node.get("title", ""),
            area_path=None,
            project_name=identifier.split("-", 1)[0],  # team key
            iteration_path=(f"linear:cycle:{cycle['id']}" if cycle else None),
            sprint_name=(cycle["name"] if cycle else None),
            work_item_type=None,  # Linear has no issue types; labels carry taxonomy
            description=node.get("description") or None,
            acceptance_criteria=None,  # no separate field; AC live in the description
            status=self._map_status(node),
            priority=_LINEAR_PRIORITY.get(node.get("priority") or 0, "medium"),
            assignee_name=(node.get("assignee") or {}).get("name"),
            assignee_external_id=(node.get("assignee") or {}).get("id"),
            story_points=(int(node["estimate"]) if node.get("estimate") is not None else None),
            estimated_hours=None,  # Linear has no native hours field
            external_state=(node.get("state") or {}).get("name"),
            state_changed_at=None,  # history needs a second query; not worth it
            external_changed_at=_parse_iso(node.get("updatedAt")),
            tags=",".join(lab["name"] for lab in (node.get("labels") or {}).get("nodes") or [])
            or None,
        )

    # -- read side --------------------------------------------------------

    async def _fetch(self, external_id: str) -> dict | None:
        """Raw node by identifier (carries the uuid mutations need)."""
        data = await self._data(_ISSUE_QUERY, {"identifier": external_id})
        nodes = (data.get("issues") or {}).get("nodes") or []
        return nodes[0] if nodes else None

    async def _issues_page(self, cursor: str | None, first: int) -> dict:
        data = await self._data(_ISSUES_QUERY, {"after": cursor, "first": first})
        return data["issues"]

    async def list_tasks(self, page: int = 1, page_size: int = 50) -> TaskPage:
        # ponytail: page>1 walks cursors (O(page) requests); sync walks
        # forward page by page so it stays one request per page.
        cursor: str | None = None
        for _ in range(page - 1):
            conn = await self._issues_page(cursor, page_size)
            if not conn["pageInfo"]["hasNextPage"]:
                return TaskPage(items=[], total=0, page=page, page_size=page_size, has_more=False)
            cursor = conn["pageInfo"]["endCursor"]
        conn = await self._issues_page(cursor, page_size)
        return TaskPage(
            items=[self._map_issue(n) for n in conn["nodes"]],
            # Linear exposes no total; per-page truth like the GitHub provider.
            total=(page - 1) * page_size + len(conn["nodes"]),
            page=page,
            page_size=page_size,
            has_more=conn["pageInfo"]["hasNextPage"],
        )

    async def get_task(self, external_id: str) -> ExternalTask | None:
        node = await self._fetch(external_id)
        return self._map_issue(node) if node else None

    async def sync_tasks(self) -> list[ExternalTask]:
        out: list[ExternalTask] = []
        page = 1
        while True:
            task_page = await self.list_tasks(page=page, page_size=50)
            out.extend(task_page.items)
            if not task_page.has_more:
                return out
            page += 1

    async def _team_id(self) -> str | None:
        """Team UUID: the configured key if set, else the team of the first issue."""
        if self.team_key:
            data = await self._data(_TEAM_QUERY, {"key": self.team_key})
            team = data.get("team") or {}
            if team.get("id"):
                return team["id"]
            logger.warning("Linear team %r not found; sprints will be empty", self.team_key)
            return None
        conn = await self._issues_page(None, 1)
        nodes = conn.get("nodes") or []
        return (nodes[0].get("team") or {}).get("id") if nodes else None

    async def list_sprints(self) -> list[ExternalSprint]:
        team_id = await self._team_id()
        if team_id is None:
            return []
        data = await self._data(_CYCLES_QUERY, {"teamId": team_id})
        out: list[ExternalSprint] = []
        for c in (data.get("cycles") or {}).get("nodes") or []:
            starts = _parse_iso(c.get("startsAt"))
            ends = _parse_iso(c.get("endsAt"))
            out.append(
                ExternalSprint(
                    name=c["name"],
                    path=f"linear:cycle:{c['id']}",
                    start_date=(starts.date() if starts else None),
                    end_date=(ends.date() if ends else None),
                )
            )
        return out

    async def list_team_members(self) -> list[ExternalTeamMember]:
        # Needs the team's members connection; the import falls back to
        # matching issue assignees by display name.
        return []

    # -- write side -------------------------------------------------------

    async def _assignee_id(self, name: str) -> str:
        """Linear assigns by user uuid, not display name; look it up."""
        data = await self._data(_USERS_QUERY, {"name": name})
        for user in (data.get("users") or {}).get("nodes") or []:
            if user.get("active", True) and user.get("name", "").lower() == name.lower():
                return user["id"]
        raise ValueError(f"no active Linear user matching {name!r}")

    async def _target_state_id(self, team_id: str, target: str) -> str:
        data = await self._data(_STATES_QUERY, {"teamId": team_id})
        states = (data.get("workflowStates") or {}).get("nodes") or []
        pick = _pick_state(states, target, self.review_statuses)
        if pick is None:
            raise ValueError(
                f"no workflow state for {target!r} on team {team_id} "
                f"(has: {[s.get('name') for s in states]})"
            )
        return pick["id"]

    async def create_task(self, task: ExternalTaskCreate) -> ExternalTask:
        self._check_writable()
        team_id = await self._team_id()
        if team_id is None:
            raise ValueError(
                "could not resolve a Linear team; set LINEAR_TEAM_KEY "
                "(e.g. \"ENG\") or sync once so the team can be inferred"
            )
        body = task.description or ""
        if task.acceptance_criteria:
            body += f"\n\n## Acceptance criteria\n{task.acceptance_criteria}"
        issue_input: dict = {
            "teamId": team_id,
            "title": task.title,
            "description": body or None,
            "priority": _OUR_PRIORITY_TO_LINEAR[task.priority],
        }
        if task.assignee_name:
            issue_input["assigneeId"] = await self._assignee_id(task.assignee_name)
        # New issues land in the team's default state (backlog/todo family);
        # move if asked further.
        if task.status not in ("backlog", "todo"):
            issue_input["stateId"] = await self._target_state_id(team_id, task.status)
        data = await self._data(_CREATE_MUTATION, {"input": issue_input})
        payload = data.get("issueCreate") or {}
        if not payload.get("success") or not payload.get("issue"):
            raise ValueError(f"Linear create failed: {payload}")
        return self._map_issue(payload["issue"])

    async def update_task(
        self, external_id: str, changes: ExternalTaskUpdate
    ) -> ExternalTask:
        self._check_writable()
        node = await self._fetch(external_id)
        if node is None:
            raise ValueError(f"issue {external_id} not found in Linear")

        issue_input: dict = {}
        if changes.title is not None:
            issue_input["title"] = changes.title
        if changes.description is not None:
            issue_input["description"] = changes.description
        if changes.priority is not None:
            issue_input["priority"] = _OUR_PRIORITY_TO_LINEAR[changes.priority]
        if changes.assignee_name is not None:
            issue_input["assigneeId"] = await self._assignee_id(changes.assignee_name)
        if changes.status is not None and changes.status != self._map_status(node):
            team_id = (node.get("team") or {}).get("id")
            if not team_id:
                raise ValueError(f"issue {external_id} carries no team; cannot change state")
            issue_input["stateId"] = await self._target_state_id(team_id, changes.status)
        if not issue_input:
            return self._map_issue(node)  # nothing asked for; nothing sent
        data = await self._data(_UPDATE_MUTATION, {"id": node["id"], "input": issue_input})
        payload = data.get("issueUpdate") or {}
        if not payload.get("success") or not payload.get("issue"):
            raise ValueError(f"Linear update failed: {payload}")
        return self._map_issue(payload["issue"])
