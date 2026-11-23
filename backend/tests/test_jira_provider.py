"""Pure mapping tests for the Jira provider.

Same philosophy as test_github_provider: no network, no fixtures — feed
crafted REST v3 issue JSON into the mapper and assert the contract the import
layer depends on. The status mapping (statusCategory + review-status
override) is the public interface to anyone pointing the app at a Jira site;
these tests are its spec.
"""

import asyncio

import pytest

from app.integrations.jira_task_provider import (
    DEFAULT_REVIEW_STATUSES,
    JiraTaskProvider,
    _doc_text,
)
from app.integrations.task_provider import ExternalTaskCreate

P = JiraTaskProvider(url="https://acme.atlassian.net", email="a@b.c", api_token="t")


def issue(**over):
    base = {
        "id": "10042",
        "key": "NW-42",
        "fields": {
            "summary": "Fix checkout",
            "description": None,
            "status": {"name": "To Do", "statusCategory": {"key": "new"}},
            "assignee": None,
            "priority": {"name": "Medium"},
            "issuetype": {"name": "Task"},
            "fixVersions": [],
            "labels": [],
            "updated": "2026-11-15T09:00:00.000+0000",
        },
    }
    base["fields"].update(over.pop("fields", {}))
    base.update(over)
    return base


def status(name, category):
    return {"name": name, "statusCategory": {"key": category}}


def test_status_categories():
    assert P._map_issue(issue(fields={"status": status("To Do", "new")})).status == "backlog"
    assert P._map_issue(issue(fields={"status": status("In Dev", "indeterminate")})).status == "in_progress"
    assert P._map_issue(issue(fields={"status": status("Closed", "done")})).status == "done"


def test_review_status_name_overrides_category():
    # "In Review" boards as review even though its category is indeterminate.
    t = P._map_issue(issue(fields={"status": status("In Review", "indeterminate")}))
    assert t.status == "review"
    # Custom list replaces the default, not extends it.
    custom = JiraTaskProvider(url="https://x.atlassian.net", email="a", api_token="t",
                              review_statuses=["QA Gate"])
    assert custom._map_issue(issue(fields={"status": status("QA Gate", "new")})).status == "review"
    assert custom._map_issue(issue(fields={"status": status("In Review", "indeterminate")})).status == "in_progress"


def test_fixversion_becomes_sprint():
    t = P._map_issue(issue(fields={"fixVersions": [{"id": "5", "name": "Sprint 15",
                                                    "releaseDate": "2026-11-30"}]}))
    assert t.sprint_name == "Sprint 15"
    assert t.iteration_path == "jira:NW#5"
    assert P._map_issue(issue()).sprint_name is None


def test_priority_name_map():
    for name, expected in [("Highest", "critical"), ("High", "critical"),
                           ("Medium", "medium"), ("Low", "low"), ("Lowest", "low")]:
        assert P._map_issue(issue(fields={"priority": {"name": name}})).priority == expected
    # Unknown or absent priority falls back to medium.
    assert P._map_issue(issue(fields={"priority": {"name": "Trivial"}})).priority == "medium"
    assert P._map_issue(issue(fields={"priority": None})).priority == "medium"


def test_description_doc_json_is_flattened():
    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Fix the pay flow."}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "visa"}]},
                ]},
                {"type": "listItem", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "amex"}]},
                ]},
            ]},
        ],
    }
    t = P._map_issue(issue(fields={"description": doc}))
    assert t.description == "Fix the pay flow.\nvisa\namex"
    assert P._map_issue(issue()).description is None


def test_assignee_identity_is_stable():
    t = P._map_issue(issue(fields={"assignee": {"accountId": "5b10a2",
                                                "displayName": "Sarah Chen"}}))
    assert t.assignee_name == "Sarah Chen"
    assert t.assignee_external_id == "5b10a2"


def test_key_and_project_and_type():
    t = P._map_issue(issue())
    assert t.external_id == "NW-42"
    assert t.project_name == "NW"           # key prefix
    assert t.work_item_type == "Task"       # issuetype passes through as-is
    assert P._map_issue(issue(fields={"issuetype": {"name": "Bug"}})).work_item_type == "Bug"
    assert t.area_path is None and t.story_points is None and t.estimated_hours is None
    assert t.external_state == "To Do"
    assert t.external_changed_at is not None and t.external_changed_at.year == 2026


def test_labels_join_to_tags():
    t = P._map_issue(issue(fields={"labels": ["payments", "regression"]}))
    assert t.tags == "payments,regression"


def test_url_must_be_absolute():
    with pytest.raises(ValueError):
        JiraTaskProvider(url="acme.atlassian.net", email="a", api_token="t")


def test_write_side():
    import httpx

    from app.integrations.jira_task_provider import _pick_transition, _text_doc
    from app.integrations.task_provider import ExternalTaskUpdate, ReadOnlyProviderError

    class FakeJira(JiraTaskProvider):
        """Records calls, replays canned responses — no network."""

        def __init__(self, responses):
            super().__init__(url="https://acme.atlassian.net", email="a@b.c", api_token="t")
            self.responses = list(responses)  # (method, path, status, json_body)
            self.calls = []

        async def _request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json"), kwargs.get("params")))
            want_method, want_path, code, body = self.responses.pop(0)
            assert (method, path) == (want_method, want_path), \
                f"expected {want_method} {want_path}, got {method} {path}"
            return httpx.Response(code, json=body, request=httpx.Request(method, path))

    # -- text <-> ADF round-trips (create/update descriptions are ADF-only) --
    assert _doc_text(_text_doc("one\ntwo")) == "one\ntwo"
    assert _text_doc(None)["content"] == [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]

    # -- transition picking --
    transitions = [
        {"id": "11", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}},
        {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
    ]
    assert _pick_transition(transitions, "review", DEFAULT_REVIEW_STATUSES)["id"] == "11"
    assert _pick_transition(transitions, "done", DEFAULT_REVIEW_STATUSES)["id"] == "31"
    assert _pick_transition([{"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}}],
                            "in_progress", DEFAULT_REVIEW_STATUSES) is None

    # -- read-only guard fires before any I/O --
    ro = JiraTaskProvider(url="https://x.atlassian.net", email="a", api_token="t",
                          read_only=True)
    with pytest.raises(ReadOnlyProviderError):
        ro._check_writable()
    with pytest.raises(ReadOnlyProviderError):
        asyncio.run(ro.create_task(ExternalTaskCreate(title="x")))
    with pytest.raises(ReadOnlyProviderError):
        asyncio.run(ro.update_task("NW-42", ExternalTaskUpdate(status="done")))

    # -- create: project resolved from JQL, description as ADF, priority reversed --
    p = FakeJira([
        ("GET", "/rest/api/3/search", 200, {"issues": [{"key": "NW-7"}]}),
        ("POST", "/rest/api/3/issue", 201, {"key": "NW-99"}),
        ("GET", "/rest/api/3/issue/NW-99", 200, issue(**{"key": "NW-99"})),
    ])
    created = asyncio.run(p.create_task(ExternalTaskCreate(
        title="Fix checkout", description="Line one\nLine two", priority="critical")))
    assert created.external_id == "NW-99"
    method, path, body, _ = p.calls[1]
    assert (method, path) == ("POST", "/rest/api/3/issue")
    assert body["fields"]["project"] == {"key": "NW"}
    assert body["fields"]["description"] == _text_doc("Line one\nLine two")
    assert body["fields"]["priority"] == {"name": "Highest"}
    assert body["fields"]["issuetype"] == {"name": "Task"}

    # -- create with a status beyond the first column transitions after create --
    p = FakeJira([
        ("GET", "/rest/api/3/search", 200, {"issues": [{"key": "NW-7"}]}),
        ("POST", "/rest/api/3/issue", 201, {"key": "NW-98"}),
        ("GET", "/rest/api/3/issue/NW-98", 200, issue(**{"key": "NW-98"})),
        ("GET", "/rest/api/3/issue/NW-98/transitions", 200, {"transitions": transitions}),
        ("POST", "/rest/api/3/issue/NW-98/transitions", 204, None),
        ("GET", "/rest/api/3/issue/NW-98", 200, issue(**{"key": "NW-98",
            "fields": {"status": status("Done", "done")}})),
    ])
    created = asyncio.run(p.create_task(ExternalTaskCreate(title="x", status="done")))
    assert created.status == "done"
    assert p.calls[4][2] == {"id": "31"}

    # -- update: PUT fields + transition, re-read for the return value --
    p = FakeJira([
        ("GET", "/rest/api/3/issue/NW-42", 200,
         issue(fields={"status": status("In Dev", "indeterminate")})),
        ("PUT", "/rest/api/3/issue/NW-42", 204, None),
        ("GET", "/rest/api/3/issue/NW-42/transitions", 200, {"transitions": transitions}),
        ("POST", "/rest/api/3/issue/NW-42/transitions", 204, None),
        ("GET", "/rest/api/3/issue/NW-42", 200, issue(fields={"status": status("Done", "done")})),
    ])
    updated = asyncio.run(p.update_task("NW-42", ExternalTaskUpdate(
        status="done", priority="critical")))
    assert updated.status == "done"
    assert p.calls[1][2] == {"fields": {"priority": {"name": "Highest"}}}
    assert p.calls[3][2] == {"id": "31"}
    # a status-only no-op (same mapped status) never PUTs or transitions
    p = FakeJira([
        ("GET", "/rest/api/3/issue/NW-42", 200,
         issue(fields={"status": status("In Dev", "indeterminate")})),
        ("GET", "/rest/api/3/issue/NW-42", 200,
         issue(fields={"status": status("In Dev", "indeterminate")})),
    ])
    asyncio.run(p.update_task("NW-42", ExternalTaskUpdate(status="in_progress")))
    assert [c[0] for c in p.calls] == ["GET", "GET"]

    # -- assignee: display name resolved to an active accountId --
    p = FakeJira([
        ("GET", "/rest/api/3/issue/NW-42", 200, issue()),
        ("GET", "/rest/api/3/users/search", 200, [
            {"accountId": "9", "displayName": "Sarah Chen", "active": False},
            {"accountId": "5b10a2", "displayName": "Sarah Chen", "active": True},
        ]),
        ("PUT", "/rest/api/3/issue/NW-42", 204, None),
        ("GET", "/rest/api/3/issue/NW-42", 200, issue()),
    ])
    asyncio.run(p.update_task("NW-42", ExternalTaskUpdate(assignee_name="Sarah Chen")))
    assert p.calls[1][3] == {"query": "Sarah Chen"}
    assert p.calls[2][2] == {"fields": {"assignee": {"id": "5b10a2"}}}
    with pytest.raises(ValueError):  # unknown user is loud, not silently skipped
        asyncio.run(FakeJira([
            ("GET", "/rest/api/3/issue/NW-42", 200, issue()),
            ("GET", "/rest/api/3/users/search", 200, []),
        ]).update_task("NW-42", ExternalTaskUpdate(assignee_name="Nobody")))

    # -- search: 410 on the deprecated GET falls back to POST /search/jql --
    p = FakeJira([
        ("GET", "/rest/api/3/search", 410, {}),
        ("POST", "/rest/api/3/search/jql", 200, {"issues": [issue()], "isLast": True}),
    ])
    page = asyncio.run(p.list_tasks())
    assert page.has_more is False and len(page.items) == 1
    assert p.calls[1][1] == "/rest/api/3/search/jql"
    assert p.calls[1][2]["jql"] == "ORDER BY updated DESC"
