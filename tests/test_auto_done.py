import pytest
from textual.widgets import Static, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, SyncError, parse_source

CREATED = "2026-09-25T13:00:00Z"
MERGED = "2026-09-28T14:00:00Z"
REPLY = "2026-09-28T15:00:00Z"


def setup_card(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Review", status="ready", priority=1, description="Local plan")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task, source)
    states = {"org/sdk:1": "MERGED"}
    timeline = []
    failures = set()

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            key = f"{argv[5]}:{argv[3]}"
            return {
                "state": states[key],
                "mergedAt": MERGED if states[key] == "MERGED" else None,
                "isDraft": False,
                "headRefOid": "head",
                "updatedAt": MERGED,
            }
        endpoint = argv[-1]
        if "/timeline?" in endpoint:
            return timeline
        parts = endpoint.split("/")
        key = f"{parts[1]}/{parts[2]}:{parts[4]}"
        if key in failures:
            raise SyncError("Unavailable")
        state = states[key]
        return {
            "title": "Remote",
            "state": state.lower(),
            "created_at": CREATED,
            "closed_at": MERGED if state == "closed" else None,
            "updated_at": MERGED,
            "comments": 0,
            "labels": [],
            "user": {"type": "User", "login": "contributor"},
            "author_association": "NONE",
        }

    return store, task, source, states, timeline, failures, Syncer(store, {"repos": []}, runner)


@pytest.mark.parametrize("mode", ["card", "board"])
def test_merged_pr_completes_once_and_preserves_local_fields(tmp_path, mode):
    store, task, source, _, _, _, sync = setup_card(tmp_path)
    store.update(task, sla_source=source["key"])
    before = store.get(task)
    refresh = (lambda: sync.sync_card(task)) if mode == "card" else sync.sync
    assert not any(refresh().values())
    after = store.get(task)
    assert after["status"] == "done" and after["revision"] == before["revision"] + 1
    for field in ("title", "description", "priority", "sla_source"):
        assert after[field] == before[field]
    assert after["updates"][-1]["kind"] == "completed"
    assert after["updates"][-1]["details"]["sources"] == [source["url"]]
    history = store.history(task)
    refresh()
    assert store.history(task) == history


@pytest.mark.parametrize("state", ["OPEN", "CLOSED"])
def test_unmerged_pr_does_not_complete(tmp_path, state):
    store, task, _, states, _, _, sync = setup_card(tmp_path)
    states["org/sdk:1"] = state
    sync.sync_card(task)
    assert store.get(task)["status"] == "ready"


@pytest.mark.parametrize("mode", ["card", "board"])
def test_linked_issue_must_be_freshly_verified_closed(tmp_path, mode):
    store, task, _, states, _, failures, sync = setup_card(tmp_path)
    issue = parse_source("https://github.com/org/other/issues/2", {})
    store.link(task, issue)
    # A stale cached closure must not hide a currently open issue or a failed refresh.
    store.observe(issue, "Remote", {"state": "closed"})
    states["org/other:2"] = "open"
    refresh = (lambda: sync.sync_card(task)) if mode == "card" else sync.sync
    refresh()
    assert store.get(task)["status"] == "ready"
    states["org/other:2"] = "closed"
    failures.add("org/other:2")
    assert any(refresh().values())
    assert store.get(task)["status"] == "ready"
    failures.clear()
    assert not any(refresh().values())
    assert store.get(task)["status"] == "done"


def test_scope_only_refresh_cannot_complete_using_other_scopes_cached_data(tmp_path):
    store, task, _, states, _, _, sync = setup_card(tmp_path)
    issue = parse_source("https://github.com/org/other/issues/2", {})
    store.link(task, issue)
    store.observe(issue, "Remote", {"state": "closed"})
    states["org/other:2"] = "closed"
    sync.sync(only="github:org/sdk")
    assert store.get(task)["status"] == "ready"
    sync.sync()
    assert store.get(task)["status"] == "done"


def test_all_linked_prs_must_be_merged(tmp_path):
    store, task, _, states, _, _, sync = setup_card(tmp_path)
    store.link(task, parse_source("https://github.com/org/sdk/pull/2", {}))
    states["org/sdk:2"] = "OPEN"
    sync.sync_card(task)
    assert store.get(task)["status"] == "ready"
    states["org/sdk:2"] = "MERGED"
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"


def test_post_merge_question_reopens_and_stays_open_until_answered(tmp_path, monkeypatch):
    monkeypatch.setattr("sdk_support_hero.store.now", lambda: "2026-09-28T14:01:00Z")
    store, task, _, _, timeline, _, sync = setup_card(tmp_path)
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"
    timeline.append(
        {
            "event": "commented",
            "created_at": REPLY,
            "user": {"type": "User"},
            "author_association": "CONTRIBUTOR",
        }
    )
    sync.sync_card(task)
    assert store.get(task)["status"] == "inbox"
    history = store.history(task)
    sync.sync_card(task)
    assert store.get(task)["status"] == "inbox" and store.history(task) == history
    timeline.append(
        {
            "event": "commented",
            "created_at": "2026-09-28T16:00:00Z",
            "user": {"type": "User"},
            "author_association": "MEMBER",
        }
    )
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"


def test_first_refresh_of_merged_pr_with_post_merge_question_stays_open(tmp_path):
    store, task, _, _, timeline, _, sync = setup_card(tmp_path)
    timeline.append(
        {
            "event": "commented",
            "created_at": REPLY,
            "user": {"type": "User"},
            "author_association": "CONTRIBUTOR",
        }
    )
    sync.sync_card(task)
    assert store.get(task)["status"] == "ready"


def test_pre_merge_comment_does_not_block_completion(tmp_path):
    store, task, _, _, timeline, _, sync = setup_card(tmp_path)
    timeline.append(
        {
            "event": "commented",
            "created_at": CREATED,
            "user": {"type": "User"},
            "author_association": "CONTRIBUTOR",
        }
    )
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"


@pytest.mark.parametrize("change", ["task", "source", "link", "delete"])
def test_completion_rejects_concurrent_local_or_source_changes(tmp_path, change):
    store, task, source, _, _, _, sync = setup_card(tmp_path)
    snapshots = sync.completion_snapshots()
    sync.github_item(source)
    if change == "task":
        store.update(task, description="New local decision")
    elif change == "source":
        store.observe(source, "Different observation", {"state": "OPEN"})
    elif change == "link":
        store.link(task, parse_source("https://github.com/org/sdk/issues/2", {}))
    else:
        store.delete(task, expected_revision=store.get(task)["revision"])
    store.complete_merged_cards(sync.refreshed, snapshots)
    if change == "delete":
        assert not store.tasks()
    else:
        assert store.get(task)["status"] == "ready"


async def test_details_refresh_moves_merged_card_to_done_without_losing_draft(tmp_path):
    store, task, _, _, _, _, sync = setup_card(tmp_path)
    app = Board(store, {"repos": []}, sync)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.press("right", "e")
        app.screen.query_one(TextArea).load_text("Unsaved draft")
        await pilot.click("#detail-refresh")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert store.get(task)["status"] == "done"
        assert app.screen.query_one(TextArea).text == "Unsaved draft"
        assert "changed elsewhere" in str(app.screen.query_one("#detail-warning", Static).render())
        await pilot.click("#detail-close")
        assert [c.task_id for c in app.query_one("#list-done").children] == [task]
        assert not list(app.query_one("#list-ready").children)


def test_unanswered_post_closure_issue_reply_blocks_completion(tmp_path):
    store, task, source, _, _, _, sync = setup_card(tmp_path)
    issue = parse_source("https://github.com/org/sdk/issues/2", {})
    store.link(task, issue)
    snapshots = sync.completion_snapshots()
    sync.github_item(source)
    facts = {
        "state": "closed",
        "closed_at": MERGED,
        "needs_team_reply": True,
        "last_external_reply_at": REPLY,
    }
    store.observe(issue, "Issue", facts)
    sync.refreshed[issue["key"]] = {"title": "Issue", "facts": facts}
    store.complete_merged_cards(sync.refreshed, snapshots)
    assert store.get(task)["status"] == "ready"


@pytest.mark.parametrize("status", ["open", "pending", "on_hold", "resolved"])
def test_support_link_must_be_resolved_before_completion(tmp_path, status):
    store, task, source, _, _, _, sync = setup_card(tmp_path)
    ticket = parse_source(
        "https://support.example.com/project/4242/support/tickets/ticket",
        {"support": {"host": "https://support.example.com", "project": 4242}},
    )
    store.link(task, ticket)
    snapshots = sync.completion_snapshots()
    sync.github_item(source)
    facts = {"status": status}
    store.observe(ticket, "Support", facts)
    sync.refreshed[ticket["key"]] = {"title": "Support", "facts": facts}
    store.complete_merged_cards(sync.refreshed, snapshots)
    assert store.get(task)["status"] == ("done" if status == "resolved" else "ready")
