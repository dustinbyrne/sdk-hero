import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import parse_source


def test_migration_preserves_all_legacy_text_and_is_idempotent(tmp_path):
    path = tmp_path / "board.db"
    store = Store(path)
    task_id = store.create("Old card", status="waiting", priority=1)
    source = parse_source("https://github.com/PostHog/posthog-js/issues/1", {})
    store.link(task_id, source)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE updates")
        db.execute("ALTER TABLE tasks DROP COLUMN description")
        for field in ("owner", "next_action", "blocker", "notes"):
            db.execute(f"ALTER TABLE tasks ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        db.execute(
            "UPDATE tasks SET owner='Me',next_action='Reply',blocker='Release',"
            "notes='First\nSecond'"
        )
        db.execute("PRAGMA user_version=1")
    store = Store(path)
    task = store.get(task_id)
    assert (
        task["description"]
        == "Next owner: Me\n\nNext action: Reply\n\nBlocker: Release\n\nNotes: First\nSecond"
    )
    assert task["priority"] == 1 and task["status"] == "waiting"
    assert task["sources"][0]["key"] == source["key"]
    assert task["updates"][0]["kind"] == "migration"
    assert "notes" not in task
    assert Store(path).get(task_id) == task


def test_local_history_atomic_noops_and_conflict(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("One", description="Original")
    store.update(task_id, description="Current", status="ready", actor="you", expected_revision=1)
    edit = store.history(task_id)[-1]
    assert edit["actor"] == "you"
    assert edit["details"]["description"] == {"before": "Original", "after": "Current"}
    count = len(store.history(task_id))
    store.update(task_id, description="Current")
    assert len(store.history(task_id)) == count
    with pytest.raises(Conflict):
        store.update(task_id, expected_revision=1, description="Stale")
    assert len(store.history(task_id)) == count
    store.add_note(task_id, "Customer replied")
    assert store.get(task_id)["description"] == "Current"
    assert store.get(task_id)["revision"] == 2
    assert store.history(task_id)[-1]["summary"] == "Customer replied"


def test_external_changes_log_meaningful_deltas_only(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-js/pull/42", {})
    checks = [{"name": "a", "conclusion": "SUCCESS"}, {"name": "b", "conclusion": "FAILURE"}]
    task_id = store.observe(source, "A PR", {"state": "OPEN", "checks": checks, "comments": 0})
    count = len(store.history(task_id))
    store.observe(
        source,
        "A PR",
        {
            "state": "OPEN",
            "checks": checks[::-1],
            "comments": 0,
            "updated_at": "later",
            "unread_team_count": 2,
        },
    )
    assert len(store.history(task_id)) == count
    store.observe(source, "A PR", {"state": "MERGED", "checks": checks, "comments": 1})
    update = store.history(task_id)[-1]
    assert update["kind"] == "source_changed"
    assert update["details"]["changes"] == {
        "state": {"before": "OPEN", "after": "MERGED"},
        "comments": {"before": 0, "after": 1},
    }
    assert store.get(task_id)["status"] == "inbox"
    store.source_error(source["key"], "Denied")
    store.source_error(source["key"], "Denied")
    assert len([e for e in store.history(task_id) if e["kind"] == "error"]) == 1
    store.observe(source, "A PR", {"state": "MERGED", "checks": checks, "comments": 1})
    assert store.history(task_id)[-1]["kind"] == "recovered"


def test_concurrent_same_observation_logs_one_change(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-js/issues/1", {})
    task_id = store.observe(source, "An issue", {"state": "open"})

    def observe(_):
        Store(store.path).observe(source, "An issue", {"state": "closed"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(observe, range(8)))
    assert len([e for e in store.history(task_id) if e["kind"] == "source_changed"]) == 1


def test_relink_keeps_history_on_both_cards(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-js/issues/1", {})
    first = store.observe(source, "Issue", {"state": "open"})
    second = store.create("One problem")
    store.move_source(source["key"], second)
    assert store.history(first)[-1]["kind"] == "unlinked"
    assert store.history(second)[-1]["kind"] == "linked"
    store.observe(source, "Issue", {"state": "closed"})
    assert store.history(first)[-1]["kind"] == "unlinked"
    assert store.history(second)[-1]["kind"] == "source_changed"
