import json
import threading

import pytest
from textual.widgets import Button, Input, Select, TextArea

from sdk_support_hero.app import Board, CardDetails
from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, SyncError, parse_source, sync_lock

SUPPORT = {
    "host": "https://support.example.com",
    "project": 4242,
    "view": "team",
    "view_name": "Example queue",
}
DATE = "2026-09-27T12:00:00Z"


def issue(number):
    return {
        "number": number,
        "title": "Remote title",
        "state": "open",
        "created_at": DATE,
        "updated_at": DATE,
        "comments": 0,
        "labels": [],
        "user": {"login": "contributor", "type": "User"},
        "author_association": "NONE",
    }


def linked(store, task_id, url):
    source = parse_source(url, {"support": SUPPORT})
    store.link(task_id, source)
    return source


def test_refresh_only_linked_sources_without_advancing_scope_checkpoints(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Local title", description="Local plan", status="waiting", priority=1)
    sources = [
        linked(store, task_id, "https://github.com/org/sdk/issues/7"),
        linked(store, task_id, "https://github.com/org/sdk/pull/8"),
        linked(store, task_id, "https://support.example.com/project/4242/support/tickets/ticket"),
    ]
    other = store.create("Unrelated")
    linked(store, other, "https://github.com/org/sdk/issues/9")
    before_other = store.get(other)
    store.sync_result("github:org/sdk", "Previous incomplete scope refresh")
    checkpoints = store.sync_states()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            assert argv[3] == "8"
            return {"state": "OPEN", "isDraft": False, "headRefOid": "head", "updatedAt": DATE}
        if argv[0] == "gh":
            assert argv[:4] == ["gh", "api", "--method", "GET"]
            endpoint = argv[-1]
            if endpoint == "repos/org/sdk/issues/7":
                return issue(7)
            if endpoint == "repos/org/sdk/issues/8":
                return {**issue(8), "pull_request": {}}
            assert endpoint == "repos/org/sdk/issues/8/timeline?per_page=100&page=1"
            return []
        assert argv[:7] == [
            "posthog-cli",
            "--host",
            SUPPORT["host"],
            "api",
            "call",
            "--json",
            argv[6],
        ]
        args = json.loads(argv[7])
        assert args["id"] == "ticket"
        if argv[6] == "conversations-tickets-retrieve":
            return {
                "id": "ticket",
                "ticket_number": 123,
                "status": "open",
                "_posthogUrl": sources[2]["url"],
            }
        assert argv[6] == "conversations-tickets-messages-retrieve"
        return {
            "results": [{"author_type": "customer", "is_private": False, "created_at": DATE}],
            "next": None,
        }

    results = Syncer(store, {"repos": ["org/sdk"], "support": SUPPORT}, runner).sync_card(task_id)
    assert results == {source["key"]: None for source in sources}
    assert len(calls) == 6
    assert len(store.tasks()) == 2
    assert store.get(other) == before_other
    assert store.sync_states() == checkpoints
    task = store.get(task_id)
    assert (
        task["title"],
        task["description"],
        task["status"],
        task["priority"],
        task["revision"],
    ) == ("Local title", "Local plan", "waiting", 1, 1)
    assert all(source["observed_at"] and source["facts"] for source in task["sources"])


def test_card_refresh_failure_preserves_facts_and_other_scopes_can_recover(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Multiple sources")
    first = linked(store, task_id, "https://github.com/org/sdk/issues/1")
    second = linked(store, task_id, "https://github.com/org/sdk/issues/2")
    other = linked(store, task_id, "https://github.com/org/other/issues/3")
    store.observe(first, "Known title", {"state": "closed"})
    failing = True
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv[-1])
        if failing and "/sdk/" in argv[-1]:
            raise SyncError("gh exit 1: authentication/permission denied")
        return issue(int(argv[-1].rsplit("/", 1)[1]))

    syncer = Syncer(store, {"repos": []}, runner)
    results = syncer.sync_card(task_id)
    assert results[first["key"]] and results[second["key"]]
    assert results[other["key"]] is None
    assert len(calls) == 2
    by_key = {s["key"]: s for s in store.sources()}
    assert by_key[first["key"]]["facts"] == {"state": "closed"}
    assert by_key[first["key"]]["error"]
    failing = False
    assert not any(syncer.sync_card(task_id).values())
    assert all(s["error"] is None for s in store.sources())
    assert any(u["kind"] == "recovered" for u in store.history(task_id))


def test_card_refresh_lock_empty_missing_and_support_scope(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Manual")

    def no_network(*args, **kwargs):
        pytest.fail("Unexpected external request")

    syncer = Syncer(store, {"repos": []}, no_network)
    assert syncer.sync_card(task_id) == {}
    with sync_lock(store.path), pytest.raises(SyncError, match="already running"):
        syncer.sync_card(task_id)
    with pytest.raises(ValueError):
        syncer.sync_card(999)
    source = linked(
        store, task_id, "https://support.example.com/project/4242/support/tickets/ticket"
    )
    assert syncer.sync_card(task_id)[source["key"]] == (
        "Linked source does not match configured support scope"
    )


def test_card_deleted_during_refresh_is_not_recreated(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Delete while fetching")
    linked(store, task_id, "https://github.com/org/sdk/issues/1")

    def runner(argv, **kwargs):
        store.delete(task_id, expected_revision=store.get(task_id)["revision"])
        return issue(1)

    assert not any(Syncer(store, {"repos": []}, runner).sync_card(task_id).values())
    assert not store.tasks() and not store.sources()


@pytest.mark.parametrize("failure", [False, True])
async def test_details_refresh_preserves_all_unsaved_fields(tmp_path, failure):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Local title", description="Saved plan")
    linked(store, task_id, "https://github.com/org/sdk/issues/1")
    proceed = threading.Event()
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv[-1])
        assert argv[-1] == "repos/org/sdk/issues/1"
        assert proceed.wait(5)
        if failure:
            raise SyncError("Source unavailable")
        return {**issue(1), "state": "closed"}

    config = {"repos": ["org/sdk"]}
    app = Board(store, config, Syncer(store, config, runner))
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.press("e")
        screen = app.screen
        screen.query_one("#detail-title", Input).value = "Draft title"
        screen.query_one(TextArea).load_text("Unsaved description")
        screen.query_one("#detail-status", Select).value = "waiting"
        screen.query_one("#detail-priority", Select).value = 0
        screen.query_one("#new-update", Input).value = "Unposted update"
        await pilot.click("#detail-refresh")
        assert screen.query_one("#detail-refresh", Button).disabled
        assert app.syncing
        assert app.start_sync(task_id) is False
        proceed.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert calls == ["repos/org/sdk/issues/1"]
        assert not app.syncing and not screen.query_one("#detail-refresh", Button).disabled
        assert screen.query_one("#detail-title", Input).value == "Draft title"
        assert screen.query_one(TextArea).text == "Unsaved description"
        assert screen.query_one("#detail-status", Select).value == "waiting"
        assert screen.query_one("#detail-priority", Select).value == 0
        assert screen.query_one("#new-update", Input).value == "Unposted update"
        assert store.get(task_id)["description"] == "Saved plan"
        expected = "Source unavailable" if failure else "closed"
        assert expected in str(screen.query_one("#detail-sources").children[1].render())
        assert store.get(task_id)["title"] == "Local title"


async def test_details_can_close_during_refresh(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("First")
    other = store.create("Other")
    source = linked(store, task_id, "https://github.com/org/sdk/issues/1")
    proceed = threading.Event()

    class FakeSyncer:
        def sync_card(self, id, progress):
            assert id == task_id and proceed.wait(5)
            store.observe(source, "Remote title", {"state": "closed"})
            return {source["key"]: None}

    app = Board(store, {"repos": []}, FakeSyncer())
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.press("e")
        await pilot.click("#detail-refresh")
        await pilot.click("#detail-close")
        assert not isinstance(app.screen, CardDetails)
        app.open_card(other)
        await pilot.pause()
        app.screen.query_one(TextArea).load_text("Other draft")
        proceed.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.task_id == other
        assert app.screen.query_one(TextArea).text == "Other draft"
        assert app.screen.query_one("#detail-refresh", Button).disabled
        assert store.get(task_id)["sources"][0]["facts"]["state"] == "closed"


async def test_refresh_button_disabled_for_manual_or_deleted_card(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Manual")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("e")
        assert app.screen.query_one("#detail-refresh", Button).disabled
        linked(store, task_id, "https://github.com/org/sdk/issues/1")
        await app.screen.refresh_evidence()
        assert not app.screen.query_one("#detail-refresh", Button).disabled
        store.delete(task_id, expected_revision=store.get(task_id)["revision"])
        await app.screen.refresh_evidence()
        assert app.screen.query_one("#detail-refresh", Button).disabled


async def test_details_refresh_disabled_during_full_board_sync(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Source")
    linked(store, task_id, "https://github.com/org/sdk/issues/1")
    proceed = threading.Event()

    class FakeSyncer:
        def sync(self, progress):
            assert proceed.wait(5)
            return {}

    app = Board(store, {"repos": []}, FakeSyncer())
    async with app.run_test(size=(100, 38)) as pilot:
        app.action_sync()
        await pilot.press("e")
        assert app.screen.query_one("#detail-refresh", Button).disabled
        proceed.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.screen.query_one("#detail-refresh", Button).disabled


async def test_refresh_worker_exception_reenables_button(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Source")
    linked(store, task_id, "https://github.com/org/sdk/issues/1")

    class FakeSyncer:
        def sync_card(self, id, progress):
            raise SyncError("Another sync is already running")

    app = Board(store, {"repos": []}, FakeSyncer())
    async with app.run_test(size=(100, 38)) as pilot:
        await pilot.press("e")
        await pilot.click("#detail-refresh")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not app.syncing
        assert not app.screen.query_one("#detail-refresh", Button).disabled
