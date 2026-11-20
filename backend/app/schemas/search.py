"""Schemas for global search (issue #35)."""

from pydantic import BaseModel

__all__ = ["TaskHit", "MemberHit", "SearchResponse"]


class TaskHit(BaseModel):
    """One task result row; joined sprint/assignee names for context."""

    id: str
    title: str
    status: str
    sprint_id: str | None
    sprint_name: str | None
    assignee_name: str | None


class MemberHit(BaseModel):
    """One teammate result row."""

    id: str
    name: str
    role: str


class SearchResponse(BaseModel):
    """Both sections always present as lists (possibly empty, never null)."""

    tasks: list[TaskHit] = []
    members: list[MemberHit] = []
