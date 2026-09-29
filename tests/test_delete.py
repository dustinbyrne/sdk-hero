import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from sdk_support_hero.app import Board, CardDetails, DeleteCard
from sdk_support_hero.cli import main
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import Syncer, parse_source


def test_delete_suppresses_sources_and_explicit_link_reenables(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    task = store.observe(source, "Imported", {"state": "open"})
    store.add_note(task, "Local history")
    unrelated = store.create("Keep me")
    before = store.get(unrelated)
    store.delete(task, expected_revision=1)
    assert store.tasks() == [{k: v for k, v in before.items() if k not in ("sources", "updates")}]
    assert store.history(task) == [] and store.sources(task_id=task) == []
    assert store.get(unrelated) == before
    with pytest.raises(ValueError, match="not found"):
        store.get(task)
    reopened = Store(store.path)
    assert reopened.observe(source, "Imported", {"state": "open", "comments": 3}) is None
    assert len(reopened.tasks()) == 1
    replacement = reopened.create("Explicit follow-up")
    reopened.link(replacement, source)
    assert reopened.observe(source, "Imported", {"state": "open"}) == replacement


def test_refresh_does_not_reimport_deleted_card(tmp_path):
    store = Store(tmp_path / "board.db")

    def runner(argv, **kwargs):
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        return [
            {
                "number": 1,
                "title": "External issue",
                "state": "open",
                "comments": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "user": {"login": "customer"},
                "author_association": "NONE",
                "labels": [],
            }
        ]

    syncer = Syncer(store, {"repos": ["org/sdk"]}, runner)
    assert not any(syncer.sync().values())
    task = store.tasks()[0]
    store.delete(task["id"], expected_revision=task["revision"])
    assert not any(syncer.sync().values())
    assert store.tasks() == [] and store.sources() == []


def test_stale_delete_is_atomic_and_ids_are_never_reused(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    task = store.observe(source, "Imported", {})
    store.update(task, description="Changed elsewhere")
    before = store.get(task)
    with pytest.raises(Conflict):
        store.delete(task, expected_revision=1)
    assert store.get(task) == before
    store.delete(task, expected_revision=2)
    fresh = store.create("Fresh")
    assert fresh > task
    with pytest.raises(Conflict):
        store.update(task, expected_revision=1, title="Stale draft")
    assert store.get(fresh)["title"] == "Fresh"


def test_delete_racing_observation_and_create(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    task = store.observe(source, "Imported", {})
    other = Store(store.path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        deletion = pool.submit(store.delete, task, expected_revision=1)
        observation = pool.submit(other.observe, source, "Remote update", {"comments": 1})
        creation = pool.submit(other.create, "New manual")
        deletion.result()
        observation.result()
        fresh = creation.result()
    assert fresh > task
    assert [t["id"] for t in store.tasks()] == [fresh]
    assert store.observe(source, "Again", {}) is None
    with store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_v2_migration_preserves_data(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Existing", description="Keep this")
    before = store.get(task)
    with store.connect() as db:
        db.execute("DROP TABLE deleted_tasks")
        db.execute("DROP TABLE dismissed_sources")
        db.execute("PRAGMA user_version=2")
    store = Store(store.path)
    assert store.get(task) == before
    store.delete(task, expected_revision=1)
    assert store.create("Next") > task


def test_cli_delete_requires_confirmation_and_revision(tmp_path, capsys):
    path = tmp_path / "board.db"
    store = Store(path)
    task = store.create("Manual")
    args = ["--db", str(path), "--config", str(tmp_path / "none.json"), "delete", str(task)]
    assert main([*args, "--if-revision", "1"]) == 1
    assert "--yes" in capsys.readouterr().err
    assert len(store.tasks()) == 1
    with pytest.raises(SystemExit):
        main([*args, "--yes"])
    assert main([*args, "--yes", "--if-revision", "1"]) == 0
    assert store.tasks() == []


@pytest.mark.parametrize("size", [(140, 40), (80, 24)])
async def test_mouse_delete_cancel_confirm(tmp_path, size):
    store = Store(tmp_path / "board.db")
    task = store.create("Delete me")
    app = Board(store, {"repos": []})
    async with app.run_test(size=size) as pilot:
        await pilot.press("e")
        await pilot.click("#detail-delete")
        assert isinstance(app.screen, DeleteCard)
        assert app.focused.id == "delete-cancel"
        await pilot.press("enter")
        assert isinstance(app.screen, CardDetails)
        assert store.get(task)
        await pilot.click("#detail-delete")
        await pilot.click("#delete-confirm")
        await pilot.pause()
        assert not isinstance(app.screen, CardDetails)
        assert store.tasks() == []
        await pilot.press("n")
        await pilot.press("N", "e", "w", "enter")
        assert store.tasks()[0]["id"] > task


async def test_external_deletion_does_not_crash_open_details(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Open draft")
    app = Board(store, {"repos": []})
    async with app.run_test() as pilot:
        await pilot.press("e")
        store.delete(task, expected_revision=1)
        await app.screen.refresh_evidence()
        assert app.screen.query_one("#detail-save").disabled
        await pilot.press("escape")
        await pilot.pause()
        assert store.tasks() == []


async def test_post_enter_after_external_deletion_preserves_unsent_note(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Open draft")
    app = Board(store, {"repos": []})
    async with app.run_test() as pilot:
        await pilot.press("e")
        entry = app.screen.query_one("#new-update")
        entry.value = "Unsent evidence"
        store.delete(task, expected_revision=1)
        await app.screen.refresh_evidence()
        entry.focus()
        await pilot.press("enter")
        assert entry.value == "Unsent evidence"
        assert store.history(task) == []


async def test_board_actions_after_external_deletion(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Selected")
    app = Board(store, {"repos": []})
    async with app.run_test() as pilot:
        assert app.selected == task
        store.delete(task, expected_revision=1)
        app.action_priority()
        await app.action_move(1)
        await pilot.press("p", "L")
        assert store.tasks() == []


def test_note_holds_write_lock_through_existence_check(tmp_path, monkeypatch):
    store = Store(tmp_path / "board.db")
    task = store.create("Concurrent note")
    original = store._record

    def record(db, *args, **kwargs):
        with sqlite3.connect(store.path, timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        return original(db, *args, **kwargs)

    monkeypatch.setattr(store, "_record", record)
    store.add_note(task, "Evidence")
    store.delete(task, expected_revision=1)
    with pytest.raises(ValueError, match="not found"):
        store.add_note(task, "Too late")


async def test_changed_card_cannot_be_deleted_from_stale_dialog(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Original")
    app = Board(store, {"repos": []})
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.click("#detail-delete")
        store.update(task, title="Concurrent edit")
        await pilot.click("#delete-confirm")
        assert isinstance(app.screen, CardDetails)
        assert store.get(task)["title"] == "Concurrent edit"
