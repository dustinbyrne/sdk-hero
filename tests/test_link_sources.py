import json

import pytest
from textual.widgets import Button, Input, Static, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import Syncer

URL = "https://github.com/org/sdk/pull/42"


async def test_add_source_preserves_drafts_and_is_idempotent(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Work", description="Saved plan")
    app = Board(store, {"repos": []})
    app.syncer.runner = lambda *a, **kw: pytest.fail("GitHub linking must not fetch remotely")
    async with app.run_test(size=(120, 50)) as pilot:
        app.open_card(task)
        await pilot.pause()
        details = app.screen
        details.query_one("#detail-description", TextArea).load_text("Unsaved plan")
        details.query_one("#new-update", Input).value = "Unposted update"
        entry = details.query_one("#link-source-url", Input)
        button = details.query_one("#link-source-add", Button)
        for _ in range(2):
            entry.value = URL
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click(button)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert entry.value == ""
            assert not entry.disabled and not button.disabled
        after = store.get(task)
        assert [s["url"] for s in after["sources"]] == [URL]
        assert after["revision"] == details.snapshot["revision"]
        assert after["description"] == "Saved plan"
        assert details.query_one("#detail-description", TextArea).text == "Unsaved plan"
        assert details.query_one("#new-update", Input).value == "Unposted update"
        linked = [u for u in after["updates"] if u["kind"] == "linked"]
        assert len(linked) == 1 and linked[0]["actor"] == "you"
        assert not details.query_one("#detail-refresh", Button).disabled


@pytest.mark.parametrize("problem", ["invalid", "owned", "deleted"])
async def test_link_errors_retain_input_and_leave_sources_unchanged(tmp_path, problem):
    store = Store(tmp_path / "board.db")
    task = store.create("Work")
    config = {"repos": []}
    if problem == "owned":
        other = store.create("Other")
        Syncer(store, config).link_source(other, URL)
    app = Board(store, config)
    async with app.run_test(size=(120, 50)) as pilot:
        app.open_card(task)
        await pilot.pause()
        details = app.screen
        entry = details.query_one("#link-source-url", Input)
        value = "https://example.com/unsupported" if problem == "invalid" else URL
        entry.value = value
        if problem == "deleted":
            store.delete(task, expected_revision=store.get(task)["revision"])
        entry.scroll_visible(animate=False, immediate=True)
        entry.focus()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert entry.value == value
        message = str(details.query_one("#link-source-message", Static).render())
        assert {"invalid": "Supported URLs", "owned": "Already linked", "deleted": "not found"}[
            problem
        ] in message
        if problem != "deleted":
            assert store.get(task)["sources"] == []


def test_support_aliases_resolve_to_one_source_and_cannot_be_stolen(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Work")
    other = store.create("Other")
    config = {"repos": [], "support": {"host": "https://support.example.com", "project": 4242}}
    base = "https://support.example.com/project/4242/support/tickets/"
    calls = []

    def runner(argv, **kwargs):
        assert argv[6] == "conversations-tickets-retrieve"
        calls.append(json.loads(argv[7])["id"])
        return {"id": "canonical-id", "_posthogUrl": base + "42"}

    sync = Syncer(store, config, runner)
    sync.link_source(task, base + "42")
    sync.link_source(task, base + "canonical-id")
    assert calls == ["42", "canonical-id"]
    assert len(store.get(task)["sources"]) == 1
    assert store.get(task)["sources"][0]["remote_id"] == "canonical-id"
    with pytest.raises(Conflict, match="Already linked"):
        sync.link_source(other, base + "42")
    assert store.get(other)["sources"] == []
