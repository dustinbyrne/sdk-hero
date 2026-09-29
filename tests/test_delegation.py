import json

import pytest
from textual.widgets import Button, Input, Select, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.cli import export_markdown, main
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import Syncer, parse_source

OLD = "2026-09-28T13:00:00Z"
NEW = "2026-09-28T15:00:00Z"


def setup_card(tmp_path, delegated_to="Platform team"):
    store = Store(tmp_path / "board.db")
    task = store.create(
        "Monitor progress", status="waiting", delegated_to=delegated_to, description="Saved plan"
    )
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    store.observe(
        source, "Issue", {"state": "open", "last_external_reply_at": OLD, "needs_team_reply": False}
    )
    return store, task, source


def reply(store, source):
    store.observe(
        source,
        "Issue",
        {
            "state": "open",
            "last_external_reply_at": NEW,
            "awaiting_team_since": NEW,
            "needs_team_reply": True,
        },
    )


def test_delegated_reply_keeps_waiting_and_sla_selection(tmp_path):
    store, task, source = setup_card(tmp_path)
    other = parse_source("https://github.com/org/sdk/pull/2", {})
    store.link(task, other)
    store.update(task, sla_source=other["key"])
    before = store.get(task)
    reply(store, source)
    after = Store(store.path).get(task)
    assert {k: v for k, v in after.items() if k not in ("sources", "updates")} == {
        k: v for k, v in before.items() if k not in ("sources", "updates")
    }
    assert after["sources"][0]["facts"]["needs_team_reply"]
    assert after["updates"][-1]["kind"] == "source_changed"
    assert not any(u["kind"] == "reopened" for u in after["updates"])


def test_ordinary_waiting_still_requeues(tmp_path):
    store, task, source = setup_card(tmp_path, "")
    reply(store, source)
    assert store.get(task)["status"] == "inbox"


@pytest.mark.parametrize("status", ["inbox", "ready", "in_progress", "done"])
def test_leaving_waiting_clears_delegation_transactionally(tmp_path, status):
    store, task, source = setup_card(tmp_path)
    revision = store.get(task)["revision"]
    store.update(task, status=status, expected_revision=revision)
    assert store.get(task)["delegated_to"] == ""
    assert store.get(task)["revision"] == revision + 1
    assert store.history(task)[-1]["details"]["delegated_to"] == {
        "before": "Platform team",
        "after": "",
    }
    if status == "done":
        reply(store, source)
        assert store.get(task)["status"] == "inbox"


def test_validation_normalization_and_stale_take_back(tmp_path):
    store, task, _ = setup_card(tmp_path, "  SDK team  ")
    assert store.get(task)["delegated_to"] == "SDK team"
    revision = store.get(task)["revision"]
    store.update(task, delegated_to="Another team")
    with pytest.raises(Conflict):
        store.update(task, expected_revision=revision, status="inbox", delegated_to="")
    assert store.get(task)["delegated_to"] == "Another team"
    with pytest.raises(ValueError, match="Waiting"):
        store.create("Invalid", delegated_to="Someone")
    with pytest.raises(ValueError, match="Waiting"):
        store.update(task, status="ready", delegated_to="Someone")
    with pytest.raises(ValueError):
        store.update(task, delegated_to=None)


def test_automatic_completion_clears_delegation_and_new_reply_reopens(tmp_path, monkeypatch):
    monkeypatch.setattr("sdk_support_hero.store.now", lambda: "2026-09-28T14:00:00Z")
    store = Store(tmp_path / "board.db")
    task = store.create("Delegated PR", status="waiting", delegated_to="SDK team")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task, source)
    sync = Syncer(store, {"repos": []})
    snapshots = sync.completion_snapshots()
    facts = {"state": "MERGED", "mergedAt": OLD, "needs_team_reply": False}
    store.observe(source, "PR", facts)
    store.complete_merged_cards({source["key"]: {"title": "PR", "facts": facts}}, snapshots)
    assert store.get(task)["status"] == "done" and store.get(task)["delegated_to"] == ""
    assert store.history(task)[-1]["details"]["changes"]["delegated_to"]["after"] == ""
    store.observe(source, "PR", {**facts, "last_external_reply_at": NEW, "needs_team_reply": True})
    assert store.get(task)["status"] == "inbox"


def test_cli_delegation_and_export(tmp_path, capsys):
    path = tmp_path / "board.db"
    base = ["--db", str(path)]
    assert (
        main([*base, "add", "Delegated", "--status", "waiting", "--delegated-to", "SDK team"]) == 0
    )
    task = json.loads(capsys.readouterr().out)
    assert task["delegated_to"] == "SDK team"
    assert "**Delegated to:** SDK team" in export_markdown(Store(path))
    assert (
        main(
            [
                *base,
                "update",
                str(task["id"]),
                "--status",
                "inbox",
                "--if-revision",
                str(task["revision"]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["delegated_to"] == ""


def test_version_five_migration_preserves_tasks_and_history(tmp_path):
    store, task, _ = setup_card(tmp_path, "")
    before = store.get(task)
    with store.connect() as db:
        db.execute("ALTER TABLE tasks DROP COLUMN delegated_to")
        db.execute("PRAGMA user_version=5")
    migrated = Store(store.path)
    assert migrated.get(task) == before
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


async def test_details_delegate_and_take_back_preserve_other_drafts(tmp_path):
    store, task, source = setup_card(tmp_path, "")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.press("right", "right", "right", "e")
        assert app.screen.query_one("#take-back", Button).disabled
        app.screen.query_one("#detail-delegated-to", Input).value = "Platform team"
        await pilot.click("#detail-save")
        assert store.get(task)["delegated_to"] == "Platform team"
        assert not app.screen.query_one("#take-back", Button).disabled
        app.screen.query_one(TextArea).load_text("Unsaved plan")
        app.screen.query_one("#detail-title", Input).value = "Unsaved title"
        reply(store, source)
        await app.screen.refresh_evidence()
        assert store.get(task)["status"] == "waiting"
        await pilot.click("#take-back")
        assert store.get(task)["status"] == "inbox" and store.get(task)["delegated_to"] == ""
        assert store.get(task)["description"] == "Saved plan"
        assert app.screen.query_one(TextArea).text == "Unsaved plan"
        assert app.screen.query_one("#detail-title", Input).value == "Unsaved title"
        assert app.screen.query_one("#detail-status", Select).value == "inbox"
        await pilot.click("#detail-save")
        assert store.get(task)["description"] == "Unsaved plan"
        assert store.get(task)["title"] == "Unsaved title"


async def test_board_labels_and_details_clear_owner_when_moving_to_done(tmp_path):
    store, task, _ = setup_card(tmp_path)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        assert "Platform team" in str(app.query_one(f"#card-{task} .card-delegation").render())
        await pilot.press("right", "right", "right", "e")
        app.screen.query_one("#detail-status", Select).value = "done"
        await pilot.pause()
        assert app.screen.query_one("#detail-delegated-to", Input).disabled
        await pilot.click("#detail-save")
        assert store.get(task)["delegated_to"] == ""
        assert app.screen.query_one("#detail-delegated-to", Input).value == ""
        await pilot.click("#detail-close")
        assert not list(app.query(".card-delegation"))
