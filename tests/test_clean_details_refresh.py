import pytest
from textual.widgets import Input, Select, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, parse_source


async def test_card_refresh_adopts_automatic_done_and_preserves_unposted_note(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    task = store.observe(source, "Fix", {"state": "OPEN"})
    store.update(task, status="waiting", delegated_to="Reviewer")

    class MergedSyncer:
        def sync_card(self, task_id, progress):
            assert task_id == task
            snapshots = Syncer(store, {"repos": []}).completion_snapshots()
            facts = {"state": "MERGED", "mergedAt": "2026-09-29T12:00:00Z"}
            store.observe(source, "Fix", facts)
            store.complete_merged_cards(
                {source["key"]: {"title": "Fix", "facts": facts}}, snapshots
            )
            return {source["scope"]: None}

    app = Board(store, {"repos": []}, MergedSyncer())
    async with app.run_test(size=(120, 45)) as pilot:
        app.open_card(task)
        await pilot.pause()
        screen = app.screen
        screen.query_one("#new-update", Input).value = "Unposted update"
        await pilot.click("#detail-refresh")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert store.get(task)["status"] == "done"
        assert screen.query_one("#detail-status", Select).value == "done"
        assert screen.query_one("#detail-delegated-to", Input).value == ""
        assert screen.snapshot["revision"] == store.get(task)["revision"]
        assert str(screen.query_one("#detail-warning").render()) == ""
        assert screen.query_one("#new-update", Input).value == "Unposted update"
        before_save = store.get(task)
        await pilot.click("#detail-save")
        assert store.get(task) == before_save


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Draft title"),
        ("description", "Draft plan"),
        ("sdk", "other"),
        ("status", "ready"),
        ("priority", 0),
        ("kind", "support"),
        ("delegated_to", "New delegate"),
        ("sla_source", "github:org/sdk:item:1"),
    ],
)
async def test_any_unsaved_field_preserves_snapshot_and_conflict(tmp_path, field, value):
    store = Store(tmp_path / "board.db")
    task = store.create("Card", status="waiting", delegated_to="Original delegate")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(120, 45)) as pilot:
        app.open_card(task)
        await pilot.pause()
        screen = app.screen
        original = screen.snapshot.copy()
        widget = screen.query_one(f"#detail-{field.replace('_', '-')}")
        if isinstance(widget, TextArea):
            widget.load_text(value)
        else:
            widget.value = value
        store.update(task, status="done")
        await screen.refresh_evidence()
        assert screen.form_values()[field] == value
        assert screen.snapshot["revision"] == original["revision"]
        assert "Draft preserved" in str(screen.query_one("#detail-warning").render())
        # Reverting the edit makes the form clean, so the next refresh can adopt it.
        if isinstance(widget, TextArea):
            widget.load_text(original[field])
        else:
            widget.value = original[field]
        await screen.refresh_evidence()
        assert screen.query_one("#detail-status", Select).value == "done"
        assert screen.snapshot["revision"] == store.get(task)["revision"]
        assert str(screen.query_one("#detail-warning").render()) == ""


async def test_clean_form_adopts_all_fields_and_new_sla_option(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Old title")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.press("e")
        source = parse_source("https://github.com/org/sdk/issues/1", {})
        store.link(task, source)
        patch = {
            "title": "New title",
            "description": "New plan",
            "sdk": "new-sdk",
            "priority": 0,
            "status": "waiting",
            "kind": "issue",
            "delegated_to": "Team",
            "sla_source": source["key"],
        }
        store.update(task, **patch)
        await app.screen.refresh_evidence()
        assert app.screen.form_values() == patch
        assert not app.screen.query_one("#detail-delegated-to", Input).disabled
