"""GitHub Issues task provider.

Maps the generic TaskProvider contract onto a GitHub repository's issues:

- external_id        -> issue number (as str)
- sprint             -> milestone (title = sprint name, path = "gh:<repo>#<n>")
- board status       -> open/closed + ``board:backlog|todo|in-progress|review``
                        labels; closed == done. Unlabeled open issue == todo.
- priority           -> ``priority:low|medium|high|critical`` label (default medium)
- work item type     -> ``type:bug`` / ``type:task`` label, else "Issue"
- estimated hours    -> ``estimate:<n>h`` label (capacity needs some unit; GitHub
                        has none native)
- area/project       -> GitHub has no area paths; project_name is the repo name.

Writes (create/update) respect the same read_only dial as the TFS provider:
PUSH_ENABLED off means this provider refuses to mutate the repository.
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

# Board columns are carried in labels so a plain GitHub client can still move a
# card. Order matters: first match wins when several status labels sneak in.
_STATUS_LABELS: list[tuple[str, Literal["backlog", "in_progress", "review"]]] = [
    ("board:backlog", "backlog"),
    ("board:in-progress", "in_progress"),
    ("board:review", "review"),
]
_PRIORITY_LABELS: list[tuple[str, Literal["low", "medium", "high", "critical"]]] = [
    ("priority:critical", "critical"),
    ("priority:high", "high"),
    ("priority:medium", "medium"),
    ("priority:low", "low"),
]
_TYPE_LABELS = [("type:bug", "Bug"), ("type:task", "Task")]

_API = "https://api.github.com"


def _first(items: list | None) -> dict | None:
    return items[0] if items else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubTaskProvider(TaskProvider):
    """TaskProvider backed by GitHub Issues (REST v3)."""

    def __init__(
        self,
        repo: str,
        token: str,
        read_only: bool = False,
        timeout: float = 20.0,
    ):
        # "owner/name" — anything else fails fast at the first request instead
        # of producing confusing 404s mid-sync.
        if "/" not in repo:
            raise ValueError(f"github provider needs repo as 'owner/name', got {repo!r}")
        self.repo = repo
        self.token = token
        self.read_only = read_only
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=self.timeout,
        )

    def _check_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyProviderError(
                "GitHub provider is read-only (PUSH_ENABLED off); "
                "refusing to create/update issues"
            )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        # ponytail: one retry with backoff on 5xx; a real retry queue belongs
        # to the sync job, not the provider.
        for attempt in range(2):
            try:
                async with self._client() as client:
                    resp = await client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                raise TaskProviderTimeoutError(str(exc)) from exc
            if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
                reset = int(resp.headers.get("X-RateLimit-Reset", "60"))
                retry_after = max(1, reset - int(datetime.now().timestamp()))
                raise RateLimitError("GitHub rate limit exceeded", retry_after=retry_after)
            if resp.status_code < 500 or attempt == 1:
                return resp
            await asyncio.sleep(1.5)
        raise AssertionError("unreachable: loop returns on attempt 2")

    # -- mapping ----------------------------------------------------------

    def _map_issue(self, issue: dict) -> ExternalTask:
        labels = [lab["name"].strip().lower() for lab in issue.get("labels", [])]
        status: Literal["backlog", "todo", "in_progress", "review", "done"] = "todo"
        for label, mapped in _STATUS_LABELS:
            if label in labels:
                status = mapped
                break
        if issue.get("state") == "closed":
            status = "done"

        priority: Literal["low", "medium", "high", "critical"] = "medium"
        for label, mapped in _PRIORITY_LABELS:
            if label in labels:
                priority = mapped
                break

        work_item_type = "Issue"
        for label, mapped in _TYPE_LABELS:
            if label in labels:
                work_item_type = mapped
                break

        hours = None
        for label in labels:
            if label.startswith("estimate:") and label.endswith("h"):
                digits = label[len("estimate:"):-1]
                if digits.isdigit():
                    hours = int(digits)
                    break

        assignee = issue.get("assignee") or _first(issue.get("assignees"))
        milestone = issue.get("milestone")

        return ExternalTask(
            external_id=str(issue["number"]),
            title=issue["title"],
            area_path=None,
            project_name=self.repo.split("/", 1)[1],
            iteration_path=(f"gh:{self.repo}#{milestone['number']}" if milestone else None),
            sprint_name=(milestone["title"] if milestone else None),
            work_item_type=work_item_type,
            description=issue.get("body"),
            acceptance_criteria=None,  # GitHub has no separate field; AC live in the body
            status=status,
            priority=priority,
            assignee_name=(assignee["login"] if assignee else None),
            assignee_external_id=(str(assignee["id"]) if assignee else None),
            estimated_hours=hours,
            external_state=issue["state"],
            state_changed_at=_parse_iso(issue.get("closed_at") or issue.get("updated_at")),
            external_changed_at=_parse_iso(issue.get("updated_at")),
            tags=",".join(labels) or None,
        )

    @staticmethod
    def _labels_for_status(status: str) -> list[str]:
        return {
            "backlog": ["board:backlog"],
            "todo": [],
            "in_progress": ["board:in-progress"],
            "review": ["board:review"],
            "done": [],  # state=closed carries it
        }.get(status, [])

    # -- read side --------------------------------------------------------

    async def list_tasks(self, page: int = 1, page_size: int = 50) -> TaskPage:
        resp = await self._request(
            "GET",
            f"/repos/{self.repo}/issues",
            params={"state": "all", "page": page, "per_page": page_size},
        )
        resp.raise_for_status()
        issues = [i for i in resp.json() if "pull_request" not in i]
        return TaskPage(
            items=[self._map_issue(i) for i in issues],
            total=len(issues),  # GitHub doesn't give a total header; per-page truth
            page=page,
            page_size=page_size,
            has_more=len(resp.json()) == page_size,
        )

    async def get_task(self, external_id: str) -> ExternalTask | None:
        resp = await self._request("GET", f"/repos/{self.repo}/issues/{external_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return self._map_issue(resp.json())

    async def sync_tasks(self) -> list[ExternalTask]:
        out: list[ExternalTask] = []
        page = 1
        while True:
            task_page = await self.list_tasks(page=page, page_size=50)
            out.extend(task_page.items)
            if not task_page.has_more:
                return out
            page += 1

    async def list_sprints(self) -> list[ExternalSprint]:
        resp = await self._request(
            "GET", f"/repos/{self.repo}/milestones", params={"state": "all", "per_page": 100}
        )
        if resp.status_code == 403:
            logger.warning("milestones forbidden; sprints will be empty")
            return []
        resp.raise_for_status()
        sprints: list[ExternalSprint] = []
        for m in resp.json():
            due = _parse_iso(m.get("due_on"))
            sprints.append(
                ExternalSprint(
                    name=m["title"],
                    path=f"gh:{self.repo}#{m['number']}",
                    start_date=None,
                    end_date=(due.date() if due else None),
                )
            )
        return sprints

    async def list_team_members(self) -> list[ExternalTeamMember]:
        resp = await self._request(
            "GET", f"/repos/{self.repo}/collaborators", params={"per_page": 100}
        )
        if resp.status_code == 403:
            # Fine-grained tokens often can't list collaborators; the import
            # falls back to matching issue assignees by name.
            logger.warning("collaborators forbidden; team import skipped")
            return []
        resp.raise_for_status()
        return [
            ExternalTeamMember(
                external_id=str(c["id"]),
                name=c["login"],
                unique_name=c["login"],
                email=None,
            )
            for c in resp.json()
        ]

    # -- write side -------------------------------------------------------

    async def create_task(self, task: ExternalTaskCreate) -> ExternalTask:
        self._check_writable()
        labels = self._labels_for_status(task.status) + [
            f"priority:{task.priority}",
            f"estimate:{task.estimated_hours}h" if task.estimated_hours else None,
        ]
        body = task.description or ""
        if task.acceptance_criteria:
            body += f"\n\n## Acceptance criteria\n{task.acceptance_criteria}"
        resp = await self._request(
            "POST",
            f"/repos/{self.repo}/issues",
            json={
                "title": task.title,
                "body": body or None,
                "labels": [lab for lab in labels if lab],
                "assignees": [task.assignee_name] if task.assignee_name else [],
            },
        )
        resp.raise_for_status()
        return self._map_issue(resp.json())

    async def update_task(
        self, external_id: str, changes: ExternalTaskUpdate
    ) -> ExternalTask:
        self._check_writable()
        current = await self.get_task(external_id)
        if current is None:
            raise ValueError(f"issue #{external_id} not found in {self.repo}")

        payload: dict = {}
        if changes.title is not None:
            payload["title"] = changes.title
        if changes.description is not None:
            payload["body"] = changes.description
        if changes.assignee_name is not None:
            payload["assignees"] = [changes.assignee_name]

        # Labels carry priority/estimate/status. Recompute from current labels
        # so we never clobber unrelated ones (type:, area:, user-added...).
        if any(f is not None for f in (changes.status, changes.priority, changes.estimated_hours)):
            current_labels = [lab for lab in (current.tags or "").split(",") if lab]
            strip = {lab for lab, _ in _STATUS_LABELS} | {lab for lab, _ in _PRIORITY_LABELS}
            keep = [lab for lab in current_labels if not lab.startswith("estimate:")]
            keep = [lab for lab in keep if lab not in strip]
            if changes.status is not None:
                payload["state"] = "closed" if changes.status == "done" else "open"
                keep += self._labels_for_status(changes.status)
            else:
                keep += [lab for lab in current_labels if lab in {m for m, _ in _STATUS_LABELS} or lab == "board:backlog"]
            keep.append(f"priority:{changes.priority if changes.priority else current.priority}")
            hours = changes.estimated_hours if changes.estimated_hours is not None else current.estimated_hours
            if hours:
                keep.append(f"estimate:{hours}h")
            payload["labels"] = keep

        resp = await self._request("PATCH", f"/repos/{self.repo}/issues/{external_id}", json=payload)
        resp.raise_for_status()
        return self._map_issue(resp.json())
