import threading

from textual.widgets import Input, ListView, TextArea

from sdk_support_hero.app import AddCard, Board, Card, CardDetails, PriorityPicker
from sdk_support_hero.store import Store
from sdk_support_hero.sync import parse_source


async def test_card_number_is_visible_on_board_and_in_details(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Investigate flags")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 45)) as pilot:
        title = app.query_one(f"#card-{task_id} .card-title")
        assert str(title.render()) == f"#{task_id} · Investigate flags"
        await pilot.press("e")
        assert app.screen.query_one("#card-detail").border_title == f"Card #{task_id}"
        assert app.screen.query_one("#detail-title", Input).value == "Investigate flags"
        await pilot.click("#detail-save")
        assert store.get(task_id)["title"] == "Investigate flags"


async def test_detail_title_has_full_width_row_above_buttons(tmp_path):
    store = Store(tmp_path / "board.db")
    store.create("A long card title that needs space beside the action buttons")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.press("e")
        title = app.screen.query_one("#detail-title", Input)
        actions = app.screen.query_one("#detail-actions")
        assert title.region.bottom <= actions.region.y
        assert title.region.width == actions.region.width
        title.value = "Updated title"
        await pilot.click("#detail-save")
        assert store.tasks()[0]["title"] == "Updated title"


async def test_mouse_add_open_edit_post_and_priority(tmp_path):
    store = Store(tmp_path / "board.db")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 45)) as pilot:
        await pilot.click("#add-ready")
        assert isinstance(app.screen, AddCard)
        await pilot.press(*"Investigate flags", "enter")
        task_id = store.tasks()[0]["id"]
        assert store.get(task_id)["status"] == "ready"
        card = app.query_one(f"#card-{task_id}")
        add_button = app.query_one("#add-ready")
        assert card.region.y >= add_button.region.bottom + 1
        await pilot.click(f"#card-{task_id} .card-title")
        assert isinstance(app.screen, CardDetails)
        app.screen.query_one("#detail-description", TextArea).load_text(
            "Reproduce the failure.\nThen reply."
        )
        await pilot.click("#detail-save")
        assert store.get(task_id)["description"] == "Reproduce the failure.\nThen reply."
        app.screen.query_one("#new-update", Input).value = "Reproduction confirmed"
        app.screen.query_one("#post-update").scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click("#post-update")
        assert store.history(task_id)[-1]["summary"] == "Reproduction confirmed"
        await pilot.click("#detail-close")
        await pilot.click(f"#priority-{task_id}")
        assert isinstance(app.screen, PriorityPicker)
        await pilot.click("#choose-p0")
        assert store.get(task_id)["priority"] == 0


async def test_select_then_open_and_click_column_to_move(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("First")
    second = store.create("Second")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.click(f"#card-{second} .card-title")
        assert not isinstance(app.screen, CardDetails)
        assert app.selected == second
        assert [c.task_id for c in app.query("Card.card-selected")] == [second]
        assert not app.query_one(f"#card-{first}").has_class("card-selected")
        assert app.query_one(f"#card-{second}").styles.border_left[1].a == 1
        assert app.query_one(f"#card-{first}").styles.border_left[1].a == 0
        await pilot.click(f"#card-{second} .card-title")
        assert isinstance(app.screen, CardDetails)
        assert app.screen.task_id == second
        await pilot.click("#detail-close")
        await pilot.click("#heading-waiting")
        assert store.get(second)["status"] == "waiting"
        assert store.get(first)["status"] == "inbox"
        assert app.selected == second
        assert app.query_one(f"#card-{second}").has_class("card-selected")
        assert store.history(second)[-1]["details"]["status"]["after"] == "waiting"
        await pilot.click("#list-ready", offset=(3, 20))
        assert store.get(second)["status"] == "ready"
        count = len(store.history(second))
        await pilot.click("#heading-ready")
        await pilot.click("#search")
        assert len(store.history(second)) == count


async def test_other_column_card_selects_without_moving_either_card(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("Source")
    target = store.create("Destination card", status="ready", sdk="flutter")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        assert app.selected == first
        before = {id: store.history(id) for id in (first, target)}
        await pilot.click(f"#card-{target} .card-labels")
        assert not isinstance(app.screen, CardDetails)
        assert app.selected == target
        assert [c.task_id for c in app.query("Card.card-selected")] == [target]
        assert {id: store.history(id) for id in (first, target)} == before
        await pilot.click("#heading-waiting")
        assert store.get(first)["status"] == "inbox"
        assert store.get(target)["status"] == "waiting"


async def test_populated_column_background_moves_original_selection(tmp_path):
    store = Store(tmp_path / "board.db")
    source = store.create("Source")
    target = store.create("Existing destination", status="ready")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.click("#list-ready", offset=(0, 0))
        assert store.get(source)["status"] == "ready"
        assert store.get(target)["status"] == "ready"
        assert app.selected == source
        await pilot.click("#column-waiting", offset=(0, 0))
        assert store.get(source)["status"] == "waiting"
        assert store.get(target)["status"] == "ready"
        await pilot.click(f"#card-{target}", offset=(1, 1))
        assert app.selected == target
        assert store.get(source)["status"] == "waiting"
        assert store.get(target)["status"] == "ready"
        assert not isinstance(app.screen, CardDetails)


async def test_scrolling_another_column_does_not_move_selection(tmp_path):
    store = Store(tmp_path / "board.db")
    source = store.create("Source")
    for i in range(20):
        store.create(f"Ready {i}", status="ready")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        scrollbar = app.query_one("#list-ready").vertical_scrollbar
        before = store.history(source)
        await pilot.click(scrollbar, offset=(0, 5))
        assert store.history(source) == before
        assert app.selected == source


async def test_column_buttons_do_not_move_selected_card(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("Source")
    target = store.create("Other", status="ready")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        before = store.history(first)
        await pilot.click(f"#priority-{target}")
        assert isinstance(app.screen, PriorityPicker)
        await pilot.press("escape")
        await pilot.click("#add-waiting")
        assert isinstance(app.screen, AddCard)
        await pilot.press("escape")
        assert store.history(first) == before
        assert store.get(target)["status"] == "ready"


async def test_hidden_selection_cannot_move_a_replacement_card(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("First")
    second = store.create("Second")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        app.query_one("#search", Input).value = "Second"
        await pilot.pause()
        assert app.selected is None
        assert not list(app.query("Card.card-selected"))
        await pilot.click("#heading-ready")
        assert store.get(first)["status"] == store.get(second)["status"] == "inbox"
        await pilot.click(f"#card-{second} .card-title")
        assert app.selected == second
        assert not isinstance(app.screen, CardDetails)


async def test_external_move_keeps_selection_on_the_same_card(tmp_path):
    store = Store(tmp_path / "board.db")
    source = store.create("Source")
    other = store.create("Other")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        store.update(source, status="ready")
        await app.refresh_board()
        await pilot.pause()
        assert app.selected == source
        assert [c.task_id for c in app.query("Card.card-selected")] == [source]
        await pilot.click("#heading-waiting")
        assert store.get(source)["status"] == "waiting"
        assert store.get(other)["status"] == "inbox"


async def test_click_move_rejects_stale_revision_and_deleted_card(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Source")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        app.query_one(Card).values["revision"] -= 1
        await pilot.click("#heading-ready")
        assert store.get(task_id)["status"] == "inbox"
        store.delete(task_id, expected_revision=store.get(task_id)["revision"])
        await pilot.click("#heading-ready")
        assert not store.tasks()
        assert app.selected is None


async def test_keyboard_navigation_search_and_external_updates(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("Fix lifecycle", sdk="python", kind="issue")
    store.create("Review PR", sdk="node", kind="external_pr")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(140, 42)) as pilot:
        await pilot.press("j", "L")
        assert store.get(first)["status"] == "ready"
        await pilot.press("p")
        await pilot.click("#choose-p1")
        assert store.get(first)["priority"] == 1
        Store(store.path).update(first, description="Reproduce")
        await app.refresh_board()
        await pilot.press("e")
        assert app.screen.query_one(TextArea).text == "Reproduce"
        await pilot.press("escape")
        await pilot.press("/")
        assert app.query_one("#search", Input).has_focus
        await pilot.press(*"node")
        await pilot.pause()
        assert len(app.query_one("#list-inbox", ListView).children) == 1
        assert len(app.query_one("#list-ready", ListView).children) == 0


async def test_conflicting_save_keeps_draft_and_new_history_visible(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Original", description="Old")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("e")
        app.screen.query_one(TextArea).load_text("My draft")
        Store(store.path).update(task_id, description="Agent edit")
        await app.screen.refresh_evidence()
        assert app.screen.query_one(TextArea).text == "My draft"
        await pilot.click("#detail-save")
        assert isinstance(app.screen, CardDetails)
        assert store.get(task_id)["description"] == "Agent edit"
        assert app.screen.query_one(TextArea).text == "My draft"
        assert "changed elsewhere" in str(app.screen.query_one("#detail-warning").render())


async def test_background_sync_while_detail_open(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/PostHog/posthog-js/pull/1", {})
    task_id = store.observe(source, "Fix", {"state": "OPEN"})
    proceed = threading.Event()

    class FakeSyncer:
        def sync(self, progress):
            assert proceed.wait(5)
            progress("Reading source")
            store.observe(source, "Fix", {"state": "MERGED"})
            return {"github:posthog/posthog-js": None}

    app = Board(store, {"repos": []}, FakeSyncer())
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("r", "e")
        app.screen.query_one(TextArea).load_text("Keep this draft")
        proceed.set()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one(TextArea).text == "Keep this draft"
        assert "PR #1: State: merged." in str(
            app.screen.query_one("#detail-updates").children[0].render()
        )
        assert store.get(task_id)["status"] == "inbox"


async def test_narrow_terminal_create_and_edit(tmp_path):
    store = Store(tmp_path / "board.db")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("l", "l", "l", "l", "n")
        await pilot.press(*"Complete task", "enter")
        assert store.tasks()[0]["status"] == "done"
        await pilot.press("enter")
        assert isinstance(app.screen, CardDetails)
        app.screen.query_one(TextArea).load_text("Done and notified")
        await pilot.press("ctrl+s", "escape")
        assert store.tasks()[0]["description"] == "Done and notified"


async def test_focus_targets_correct_card(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("Inbox")
    second = store.create("Ready", status="ready")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(140, 42)) as pilot:
        app.query_one("#list-ready", ListView).focus()
        await pilot.pause()
        assert app.selected == second
        await pilot.press("e")
        assert app.screen.task_id == second
        assert store.get(first)["status"] == "inbox"


async def test_arrows_and_mouse_selection_survive_closing_details(tmp_path):
    store = Store(tmp_path / "board.db")
    first = store.create("First")
    second = store.create("Second")
    store.create("Ready", status="ready")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.press("right")
        assert app.column_index == 1
        await pilot.press("left")
        assert app.column_index == 0
        await pilot.click(f"#card-{second} .card-title")
        await pilot.click(f"#card-{second} .card-title")
        await pilot.click("#detail-close")
        assert app.selected == second
        assert app.current_list().index == 1
        await pilot.press("p")
        await pilot.click("#choose-p0")
        assert store.get(first)["priority"] == 2
        assert store.get(second)["priority"] == 0


async def test_full_history_remains_scrollable(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Long history")
    for number in range(10):
        store.add_note(task_id, f"Update {number}\n" + "Long note\n" * 8)
    for number in range(5):
        store.link(task_id, parse_source(f"https://github.com/org/sdk/issues/{number + 1}", {}))
    app = Board(store, {"repos": []})
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("e")
        history = app.screen.query_one("#detail-updates")
        assert len(history.children) == 16
        oldest = history.children[-1]
        oldest.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        viewport = app.screen.query_one("#detail-scroll").content_region
        assert oldest.region.y >= viewport.y
        assert oldest.region.bottom <= viewport.bottom
        assert history.region.height >= sum(child.region.height for child in history.children)
