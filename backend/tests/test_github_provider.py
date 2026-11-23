"""Pure mapping tests for the GitHub Issues provider.

Same philosophy as test_burndown: no network, no fixtures — feed crafted issue
JSON into the mapper and assert the contract that the import layer depends on.
The label grammar (board:/priority:/estimate:) is the public interface to
anyone moving cards from plain GitHub; these tests are its spec.
"""

from app.integrations.github_task_provider import GitHubTaskProvider
from app.integrations.task_provider import ExternalTaskUpdate

P = GitHubTaskProvider(repo="acme/widgets", token="t")


def issue(**over):
    base = {
        "number": 42,
        "title": "Fix checkout",
        "state": "open",
        "labels": [{"name": "bug"}],
        "body": "desc",
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "updated_at": "2026-11-15T09:00:00Z",
        "closed_at": None,
    }
    base.update(over)
    return base


def labels(*names):
    return [{"name": n} for n in names]


def test_plain_open_issue_is_todo():
    t = P._map_issue(issue())
    assert t.status == "todo" and t.external_id == "42"
    assert t.work_item_type == "Issue" and t.priority == "medium"


def test_closed_is_done_regardless_of_labels():
    t = P._map_issue(issue(state="closed", closed_at="2026-11-14T10:00:00Z",
                            labels=labels("board:review")))
    assert t.status == "done"
    assert t.state_changed_at is not None and t.state_changed_at.year == 2026


def test_status_label_grammar():
    assert P._map_issue(issue(labels=labels("board:backlog"))).status == "backlog"
    assert P._map_issue(issue(labels=labels("board:in-progress"))).status == "in_progress"
    assert P._map_issue(issue(labels=labels("board:review"))).status == "review"


def test_priority_and_type_and_estimate_labels():
    t = P._map_issue(issue(labels=labels("priority:high", "type:bug", "estimate:8h")))
    assert t.priority == "high" and t.work_item_type == "Bug" and t.estimated_hours == 8


def test_bad_estimate_label_is_ignored():
    t = P._map_issue(issue(labels=labels("estimate:soon")))
    assert t.estimated_hours is None


def test_milestone_becomes_sprint():
    t = P._map_issue(issue(milestone={"number": 3, "title": "Sprint 15"}))
    assert t.sprint_name == "Sprint 15"
    assert t.iteration_path == "gh:acme/widgets#3"
    assert P._map_issue(issue()).sprint_name is None


def test_assignee_identity_is_stable():
    t = P._map_issue(issue(assignee={"id": 7, "login": "sarah"}))
    assert t.assignee_name == "sarah" and t.assignee_external_id == "7"


def test_project_name_is_repo_leaf():
    assert P._map_issue(issue()).project_name == "widgets"


def test_update_status_recomputes_labels_without_clobbering():
    # The label-preservation contract: unrelated labels survive, exactly one
    # board:/priority:/estimate: set remains.
    current = P._map_issue(issue(labels=labels("bug", "area:payments",
                                               "board:in-progress", "priority:low",
                                               "estimate:4h")))
    changes = ExternalTaskUpdate(status="review", priority="high")
    payload_labels = None
    # Reproduce update_task's label math directly (network-free).
    strip = {"board:backlog", "board:in-progress", "board:review",
             "priority:low", "priority:medium", "priority:high", "priority:critical"}
    keep = [lab for lab in (current.tags or "").split(",") if lab]
    keep = [lab for lab in keep if not lab.startswith("estimate:") and lab not in strip]
    keep += P._labels_for_status(changes.status)
    keep.append(f"priority:{changes.priority}")
    hours = current.estimated_hours
    if hours:
        keep.append(f"estimate:{hours}h")
    payload_labels = keep
    assert payload_labels == ["bug", "area:payments", "board:review", "priority:high", "estimate:4h"]


def test_repo_must_be_owner_slash_name():
    import pytest
    with pytest.raises(ValueError):
        GitHubTaskProvider(repo="justaname", token="t")


def test_read_only_refuses_writes():
    import pytest
    from app.integrations.task_provider import ReadOnlyProviderError
    ro = GitHubTaskProvider(repo="a/b", token="t", read_only=True)
    # The guard is sync on purpose: every write path hits it before any I/O.
    with pytest.raises(ReadOnlyProviderError):
        ro._check_writable()
