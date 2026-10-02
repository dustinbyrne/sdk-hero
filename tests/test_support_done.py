import pytest

from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, SyncError, parse_source

CONFIG = {
    "repos": [],
    "support": {
        "host": "https://support.example.com",
        "project": 4242,
        "view": "example",
        "view_name": "Example queue",
    },
}


def setup_ticket(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create(
        "Support work", status="waiting", delegated_to="Team", priority=1, description="Local plan"
    )
    source = parse_source(
        "https://support.example.com/project/4242/support/tickets/example", CONFIG
    )
    store.link(task, source)
    state = {"status": "resolved", "fail": False, "edit": False}

    def runner(argv, **kwargs):
        tool = argv[6]
        if tool == "conversations-views-retrieve":
            return {
                "name": "Example queue",
                "filters": {"assignee": {"type": "role", "id": "team"}},
            }
        if tool == "conversations-tickets-list":
            return {"results": [], "next": None}
        if state["fail"]:
            raise SyncError("Unavailable")
        if tool == "conversations-tickets-retrieve":
            if state["edit"]:
                store.update(task, description="Concurrent edit")
            return {
                "id": "example",
                "ticket_number": 1,
                "status": state["status"],
                "_posthogUrl": source["url"],
            }
        assert tool == "conversations-tickets-messages-retrieve"
        return {
            "results": [
                {
                    "author_type": "customer",
                    "is_private": False,
                    "created_at": "2026-09-28T12:00:00Z",
                }
            ],
            "next": None,
        }

    return store, task, source, state, Syncer(store, CONFIG, runner)


@pytest.mark.parametrize("mode", ["card", "board"])
def test_resolved_ticket_completes_once_and_preserves_plan(tmp_path, mode):
    store, task, source, state, sync = setup_ticket(tmp_path)
    refresh = (lambda: sync.sync_card(task)) if mode == "card" else sync.sync
    before = store.get(task)
    assert not any(refresh().values())
    assert sync.completed_card_ids == [task]
    after = store.get(task)
    assert after["status"] == "done" and after["delegated_to"] == ""
    assert after["revision"] == before["revision"] + 1
    for field in ("title", "description", "priority"):
        assert after[field] == before[field]
    assert after["updates"][-1]["kind"] == "completed"
    assert after["updates"][-1]["details"]["sources"] == [source["url"]]
    refresh()
    assert sync.completed_card_ids == []
    assert store.get(task) == after


@pytest.mark.parametrize("blocker", ["failure", "edit", "open", "unknown"])
def test_resolution_requires_successful_current_observation_without_edits(tmp_path, blocker):
    store, task, source, state, sync = setup_ticket(tmp_path)
    store.observe(source, "Support", {"status": "resolved"})
    state["fail"] = blocker == "failure"
    state["edit"] = blocker == "edit"
    if blocker in {"open", "unknown"}:
        state["status"] = blocker
    sync.sync_card(task)
    assert store.get(task)["status"] == "waiting"
    if blocker == "edit":
        assert store.get(task)["description"] == "Concurrent edit"


@pytest.mark.parametrize(
    "kind,state,expected",
    [
        ("issue", "open", "waiting"),
        ("issue", "closed", "done"),
        ("pr", "OPEN", "waiting"),
        ("pr", "CLOSED", "waiting"),
        ("pr", "MERGED", "done"),
        ("ticket", "pending", "waiting"),
        ("ticket", "resolved", "done"),
        ("run", "completed", "waiting"),
    ],
)
def test_all_linked_work_must_be_finished(tmp_path, kind, state, expected):
    store, task, source, _, sync = setup_ticket(tmp_path)
    route = {"issue": "issues", "pr": "pull", "run": "actions/runs"}.get(kind)
    url = (
        f"https://github.com/org/sdk/{route}/2"
        if route
        else "https://support.example.com/project/4242/support/tickets/other"
    )
    other = parse_source(url, CONFIG)
    store.link(task, other)
    snapshots = sync.completion_snapshots()
    facts = {"status": "resolved"}
    other_facts = {"status" if kind == "ticket" else "state": state}
    store.observe(source, "Support", facts)
    store.observe(other, "Other work", other_facts)
    receipts = {
        source["key"]: {"title": "Support", "facts": facts},
        other["key"]: {"title": "Other work", "facts": other_facts},
    }
    store.complete_merged_cards({source["key"]: receipts[source["key"]]}, snapshots)
    assert store.get(task)["status"] == "waiting"
    store.complete_merged_cards(receipts, snapshots)
    assert store.get(task)["status"] == expected
