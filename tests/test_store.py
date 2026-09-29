from concurrent.futures import ThreadPoolExecutor

import pytest

from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import parse_source


def test_observations_preserve_local_decisions(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-js/pull/42", {})
    task_id = store.observe(source, "Initial title", {"state": "OPEN"})
    store.update(
        task_id,
        title="Customer fix",
        status="waiting",
        priority=0,
        description="Blocked on release. Notify customer afterward.",
    )
    store.source_error(source["key"], "Unavailable")
    store.observe(source, "Updated title", {"state": "MERGED", "head": "new"})
    task = store.get(task_id)
    assert (task["title"], task["status"], task["priority"]) == ("Customer fix", "waiting", 0)
    assert task["sources"][0]["facts"]["state"] == "MERGED"
    assert task["sources"][0]["error"] is None
    assert len(store.tasks()) == 1


def test_stale_editor_cannot_overwrite_concurrent_edit(tmp_path):
    a = Store(tmp_path / "board.db")
    b = Store(tmp_path / "board.db")
    task_id = a.create("Fix")
    revision = a.get(task_id)["revision"]
    b.update(task_id, priority=0)
    with pytest.raises(Conflict):
        a.update(task_id, expected_revision=revision, priority=3)
    assert a.get(task_id)["priority"] == 0


def test_concurrent_observations_deduplicate_and_patch_independent_fields(tmp_path):
    path = tmp_path / "board.db"
    Store(path)
    source = parse_source("https://github.com/PostHog/posthog-js/issues/42", {})

    def observe(_):
        return Store(path).observe(source, "Fix", {"state": "open"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(observe, range(12)))
    assert len(set(ids)) == 1
    store = Store(path)
    store.update(ids[0], description="Mine")
    Store(path).update(ids[0], status="ready")
    assert store.get(ids[0])["description"] == "Mine"


def test_linking_requires_explicit_reassignment(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-python/issues/1", {})
    first, second = store.create("One"), store.create("Two")
    store.link(first, source)
    store.link(first, source)
    with pytest.raises(Conflict):
        store.link(second, source)
    store.move_source(source["key"], second)
    assert store.sources(task_id=first) == []
    assert store.sources(task_id=second)[0]["key"] == source["key"]


def test_failed_sync_retains_checkpoint_and_evidence(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-go/issues/1", {})
    store.observe(source, "One", {"state": "open"})
    store.sync_result(source["scope"])
    previous = store.sync_states()[0]["succeeded_at"]
    store.sync_result(source["scope"], "Denied")
    store.source_error(source["key"], "Denied")
    assert store.sync_states()[0]["succeeded_at"] == previous
    assert store.sources()[0]["facts"] == {"state": "open"}
    assert store.sources()[0]["error"] == "Denied"


@pytest.mark.parametrize("fields", [{"priority": 4}, {"title": " "}, {"status": "merged"}])
def test_invalid_updates(tmp_path, fields):
    store = Store(tmp_path / "board.db")
    task_id = store.create("One")
    with pytest.raises(ValueError):
        store.update(task_id, **fields)
