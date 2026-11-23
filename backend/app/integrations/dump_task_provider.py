"""TaskProvider backed by a local `scripts/tfs-dump.py` capture.

The on-prem server is only reachable from the corporate network, which makes
iterating on the board painful. This provider replays a dump of real work items
from disk through the *same* mapping code the live TFS provider uses, so the
Sync button, the sync endpoint and the Celery job all behave identically
offline — only the transport changes.

Enable it with ``INTEGRATION_PROVIDER=dump`` in the repo-root .env. Writes are
refused: a dump is a snapshot, there is nothing to push back to.
"""

import glob
import json
import logging
import os

from datetime import date, datetime

from app.integrations.tfs_task_provider import (
    DEFAULT_SYNC_TYPES,
    EXCLUDED_STATES,
    F_AREA,
    F_STATE,
    F_TYPE,
    TFSTaskProvider,
)

from app.integrations.task_provider import (
    ExternalSprint,
    ExternalTask,
    ExternalTaskCreate,
    ExternalTaskUpdate,
    ExternalTeamMember,
    ReadOnlyProviderError,
    TaskPage,
    TaskProvider,
)


def _as_date(value: str | None) -> date | None:
    """Parse an Azure DevOps ISO timestamp ("2026-08-02T00:00:00Z") to a date."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None

logger = logging.getLogger(__name__)


class DumpTaskProvider(TaskProvider):
    """Serves work items from `<dump_dir>/sprints/*.json`."""

    def __init__(
        self,
        dump_dir: str = "/dump",
        sync_types: tuple[str, ...] | None = None,
        area_path: str = "",
        max_items: int = 0,
    ) -> None:
        self.dump_dir = dump_dir
        self.sync_types = DEFAULT_SYNC_TYPES if sync_types is None else tuple(sync_types)
        self.area_path = area_path.strip()
        self.max_items = max_items
        self._cache: list[ExternalTask] | None = None

    # ------------------------------------------------------------------
    def _load(self) -> list[ExternalTask]:
        if self._cache is not None:
            return self._cache

        pattern = os.path.join(self.dump_dir, "sprints", "*.json")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(
                f"No dump files at {pattern}. Run scripts/tfs-dump.py while on "
                "the corporate network, and check the volume mount."
            )

        raw: list[dict] = []
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    raw.extend(json.load(fh).get("items", []))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Skipping unreadable dump file %s: %s", path, exc)

        tasks: list[ExternalTask] = []
        skipped_type = skipped_state = skipped_area = 0
        seen: set[str] = set()

        for item in raw:
            fields = item.get("fields", {})
            if self.sync_types and fields.get(F_TYPE) not in self.sync_types:
                skipped_type += 1
                continue
            if fields.get(F_STATE) in EXCLUDED_STATES:
                skipped_state += 1
                continue
            if self.area_path and not (fields.get(F_AREA) or "").startswith(
                self.area_path
            ):
                skipped_area += 1
                continue

            # A work item can appear in several sprint files if it moved
            # between iterations; last write wins, as it would on the server.
            ext = TFSTaskProvider._to_external(item)
            if ext.external_id in seen:
                tasks = [t for t in tasks if t.external_id != ext.external_id]
            seen.add(ext.external_id)
            tasks.append(ext)

        logger.info(
            "Dump loaded: %d files, %d raw items -> %d tasks "
            "(skipped: type=%d state=%d area=%d)",
            len(files), len(raw), len(tasks),
            skipped_type, skipped_state, skipped_area,
        )
        if self.max_items > 0:
            tasks = tasks[: self.max_items]
        self._cache = tasks
        return tasks

    # ------------------------------------------------------------------
    async def sync_tasks(self) -> list[ExternalTask]:
        return self._load()

    async def list_sprints(self) -> list[ExternalSprint]:
        """Iterations captured alongside the work items."""
        path = os.path.join(self.dump_dir, "iterations.json")
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("No iterations in dump (%s): %s", path, exc)
            return []

        sprints: list[ExternalSprint] = []
        for row in raw:
            if not row.get("name"):
                continue
            sprints.append(
                ExternalSprint(
                    name=row["name"],
                    path=row.get("path") or row["name"],
                    start_date=_as_date(row.get("start")),
                    end_date=_as_date(row.get("finish")),
                )
            )
        return sprints

    async def list_team_members(self) -> list[ExternalTeamMember]:
        """The team roster captured by `scripts/tfs-people.py`.

        Work items name their assignee but the roster is a separate API, so it
        lands in its own file. Absent (nobody has run the script yet) is a
        normal state, not an error — the import falls back to name matching.
        """
        path = os.path.join(self.dump_dir, "people.json")
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            logger.info(
                "No %s; run scripts/tfs-people.py on the corporate network to "
                "import the real team", path
            )
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable team roster %s: %s", path, exc)
            return []

        members: list[ExternalTeamMember] = []
        seen: set[str] = set()
        for row in raw.get("people") or []:
            if not row.get("id") or not row.get("name"):
                continue
            # capacity_hours is 0 unless the team fills in the Capacity tab;
            # None keeps the model default rather than importing a fake zero.
            hours = row.get("capacity_hours") if row.get("has_capacity") else None
            seen.add(row["id"])
            members.append(
                ExternalTeamMember(
                    external_id=row["id"],
                    name=row["name"],
                    unique_name=row.get("unique_name"),
                    capacity_hours_per_sprint=round(hours) if hours else None,
                )
            )
        roster_size = len(members)

        # People carrying work who are not on the team roster — QA and other
        # streams lending a hand. Without them their tasks import unassigned
        # and the board shows "—" for an item the server knows the owner of.
        for task in self._load():
            ext_id = task.assignee_external_id
            if not ext_id or ext_id in seen or not task.assignee_name:
                continue
            seen.add(ext_id)
            members.append(
                ExternalTeamMember(external_id=ext_id, name=task.assignee_name)
            )

        logger.info(
            "Dump roster: %d team members (%d from %s, %d more found on work items)",
            len(members), roster_size, path, len(members) - roster_size,
        )
        return members

    async def list_tasks(self, page: int = 1, page_size: int = 50) -> TaskPage:
        items = self._load()
        start = (page - 1) * page_size
        end = start + page_size
        return TaskPage(
            items=items[start:end],
            total=len(items),
            page=page,
            page_size=page_size,
            has_more=end < len(items),
        )

    async def get_task(self, external_id: str) -> ExternalTask | None:
        return next(
            (t for t in self._load() if t.external_id == external_id), None
        )

    async def create_task(self, task: ExternalTaskCreate) -> ExternalTask:
        raise ReadOnlyProviderError(
            "create_task blocked: the dump provider is a read-only snapshot"
        )

    async def update_task(
        self, external_id: str, changes: ExternalTaskUpdate
    ) -> ExternalTask:
        raise ReadOnlyProviderError(
            "update_task blocked: the dump provider is a read-only snapshot"
        )
