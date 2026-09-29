import json
from datetime import datetime

import pytest

from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, SyncError, inventory_repos, parse_source, sync_lock

SUPPORT = {
    "host": "https://support.example.com",
    "project": 4242,
    "view": "test-view",
    "view_name": "Example queue",
}
START = datetime.fromisoformat("2026-09-26T00:00:00+00:00")


@pytest.fixture(autouse=True)
def fixed_rotation(monkeypatch):
    monkeypatch.setattr("sdk_support_hero.sync.rotation_start", lambda: START)


def issue(number, state="open"):
    return {
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "labels": [],
        "user": {"login": "contributor"},
        "author_association": "NONE",
        "created_at": "2026-09-27T12:00:00Z",
    }


def test_github_pagination_and_closed_tracked_items(tmp_path):
    store = Store(tmp_path / "board.db")
    tracked = parse_source("https://github.com/org/sdk/issues/201", {})
    task_id = store.observe(tracked, "Follow up", {"state": "open"})
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        if argv[-1].endswith("&page=1"):
            return [issue(i) for i in range(1, 101)]
        if argv[-1].endswith("&page=2"):
            return [issue(101)]
        assert argv[-1] == "repos/org/sdk/issues/201"
        return issue(201, "closed")

    result = Syncer(store, {"repos": ["org/sdk"]}, runner).sync()
    assert not any(result.values())
    assert len(calls) == 3
    assert len(store.tasks()) == 102
    assert store.get(task_id)["status"] == "inbox"
    assert store.get(task_id)["sources"][0]["facts"]["state"] == "closed"


def test_partial_sync_does_not_advance_success_or_delete(tmp_path):
    store = Store(tmp_path / "board.db")
    store.sync_result("github:org/sdk")
    checkpoint = store.sync_states()[0]["succeeded_at"]

    def runner(argv, **kwargs):
        if argv[-1].endswith("&page=1"):
            return [issue(i) for i in range(100)]
        raise SyncError("gh exit 1: authentication/permission denied")

    result = Syncer(store, {"repos": ["org/sdk"]}, runner).sync()
    assert result["github:org/sdk"]
    assert store.sync_states()[0]["succeeded_at"] == checkpoint
    assert len(store.tasks()) == 100


def test_pr_facts_and_no_mutations(tmp_path):
    store = Store(tmp_path / "board.db")
    store.observe(
        parse_source("https://github.com/org/sdk/pull/1", {}), "Tracked PR", {}, kind="external_pr"
    )

    def runner(argv, **kwargs):
        if argv[:2] == ["gh", "api"]:
            assert argv[2:4] == ["--method", "GET"]
            if "/timeline?" in argv[-1]:
                return []
            if argv[-1].endswith("/issues/1"):
                return {**issue(1), "pull_request": {}}
            return [{**issue(n), "pull_request": {}} for n in (1, 2)]
        assert argv[:3] == ["gh", "pr", "view"]
        return {
            "state": "OPEN",
            "headRefOid": "abc",
            "isDraft": False,
            "updatedAt": "2026-09-27T12:00:00Z",
            "statusCheckRollup": [{"name": "test", "conclusion": "FAILURE"}],
        }

    Syncer(store, {"repos": ["org/sdk"]}, runner).sync()
    task = store.get(1)
    assert task["kind"] == "external_pr"
    assert task["sources"][0]["facts"]["headRefOid"] == "abc"
    assert task["sources"][0]["facts"]["checks"][0]["conclusion"] == "FAILURE"


def test_support_pagination_resolved_followup_and_privacy(tmp_path):
    store = Store(tmp_path / "board.db")
    config = {"repos": [], "support": SUPPORT}
    old = parse_source("https://support.example.com/project/4242/support/tickets/old", config)
    task_id = store.observe(old, "Customer followup", {"status": "open"})
    list_calls = []

    def runner(argv, env=None):
        assert argv[:7] == [
            "posthog-cli",
            "--host",
            SUPPORT["host"],
            "api",
            "call",
            "--json",
            argv[6],
        ]
        assert env == {"POSTHOG_CLI_PROJECT_ID": "4242"}
        tool, args = argv[6], json.loads(argv[7])
        if tool == "conversations-views-retrieve":
            return {
                "name": "Example queue",
                "filters": {
                    "assignee": {"id": "role-id", "type": "role"},
                    "status": ["open"],
                    "snoozed": False,
                },
            }
        if tool == "conversations-tickets-list":
            list_calls.append(args)
            return {
                "results": [{"id": "a" if args["offset"] == 0 else "b"}],
                "next": "next-page" if args["offset"] == 0 else None,
            }
        if tool == "conversations-tickets-messages-retrieve":
            return {
                "results": [
                    {
                        "author_type": "customer",
                        "created_at": "2026-09-27T12:00:00Z",
                        "is_private": False,
                        "content": "private",
                    }
                ],
                "next": None,
            }
        assert tool == "conversations-tickets-retrieve"
        return {
            "id": args["id"],
            "ticket_number": 1,
            "status": "resolved",
            "priority": "critical",
            "message_count": 3,
            "assignee": {"id": "role-id", "type": "role", "user": {"email": "secret"}},
            "last_message_text": "private body",
            "person": {"email": "private"},
            "_posthogUrl": f"https://support.example.com/project/4242/support/tickets/{args['id']}",
        }

    assert not any(Syncer(store, config, runner).sync().values())
    assert [call["offset"] for call in list_calls] == [0, 100]
    assert all(call["status"] == "new,open,pending,on_hold" for call in list_calls)
    assert all(call["date_from"] == "2026-09-26" for call in list_calls)
    assert all("view" not in call and "snoozed" not in call for call in list_calls)
    assert store.get(task_id)["sources"][0]["facts"]["status"] == "resolved"
    serialized = json.dumps(store.sources())
    assert "private" not in serialized and "secret" not in serialized
    assert store.get(task_id)["status"] == "inbox"


def test_scope_failure_does_not_prevent_other_scope(tmp_path):
    def runner(argv, **kwargs):
        if "bad" in argv[-1]:
            raise SyncError("Denied")
        return []

    store = Store(tmp_path / "board.db")
    results = Syncer(store, {"repos": ["org/bad", "org/good"]}, runner).sync()
    assert results == {"github:org/bad": "Denied", "github:org/good": None}


def test_linked_product_repo_only_refreshes_linked_items(tmp_path):
    store = Store(tmp_path / "board.db")
    store.link(
        store.create("Bump"), parse_source("https://github.com/PostHog/posthog/issues/1", {})
    )
    endpoints = []

    def runner(argv, **kwargs):
        endpoints.append(argv[-1])
        return issue(1)

    Syncer(store, {"repos": []}, runner).sync()
    assert endpoints == ["repos/posthog/posthog/issues/1"]


def test_sync_lock(tmp_path):
    path = tmp_path / "board.db"
    with sync_lock(path):
        with pytest.raises(SyncError, match="already running"):
            with sync_lock(path):
                pass


def test_inventory(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text(
        "<!-- posthog-sdk-inventory:start -->\nhttps://github.com/PostHog/posthog-js\n"
        "https://github.com/PostHog/posthog-js\n<!-- posthog-sdk-inventory:end -->"
    )
    assert inventory_repos(path) == ["PostHog/posthog-js"]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/org/repo/issues/1",
        "https://github.com/org/repo/issues/1?x=y",
        "https://evil.example/org/repo/issues/1",
        "https://github.com/org/repo/issues/1;rm -rf ~",
    ],
)
def test_source_url_validation(url):
    with pytest.raises(ValueError):
        parse_source(url, {})
