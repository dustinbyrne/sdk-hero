from sdk_support_hero.app import Board
from sdk_support_hero.sla import card_schedule, work_key
from sdk_support_hero.store import Store
from sdk_support_hero.sync import parse_source


def test_priority_then_earliest_deadline_then_no_deadline():
    entries = [
        (1, 3, "2026-09-21T09:00:00-04:00"),
        (2, 2, None),
        (3, 2, "2026-09-29T09:00:00-04:00"),
        (4, 2, "2026-09-28T10:00:00-04:00"),
        (5, 2, "2026-09-25T10:00:00-04:00"),
        (6, 1, "2026-09-30T09:00:00-04:00"),
        (7, 0, None),
        (8, 2, "2026-09-28T13:00:00+00:00"),
    ]

    def key(entry):
        id, priority, due = entry
        return work_key(
            {"id": id, "priority": priority},
            {"due_at": due, "group": 0 if due is None else 3, "waiting_since": None},
        )

    assert [entry[0] for entry in sorted(entries, key=key)] == [7, 6, 5, 8, 4, 3, 2, 1]


async def test_column_sort_uses_selected_sla_and_preserves_selection_and_local_state(tmp_path):
    store = Store(tmp_path / "board.db")
    pinned = store.create("Pinned PR", status="ready")
    other = store.create("Issue", status="ready")
    urgent = store.create("Urgent without SLA", status="ready", priority=1)
    other_column = store.create("In progress", status="in_progress", priority=3)
    pr = parse_source("https://github.com/org/sdk/pull/1", {})
    linked_issue = parse_source("https://github.com/org/sdk/issues/2", {})
    issue = parse_source("https://github.com/org/sdk/issues/3", {})
    for id, source, facts in (
        (pinned, pr, {"review_waiting_since": "2026-09-28T14:00:00Z", "review_target_hours": 4}),
        (
            pinned,
            linked_issue,
            {"needs_team_reply": True, "awaiting_team_since": "2026-09-21T13:00:00Z"},
        ),
        (other, issue, {"needs_team_reply": True, "awaiting_team_since": "2026-09-25T13:00:00Z"}),
    ):
        store.link(id, source)
        store.observe(source, "Remote", facts)
    store.update(pinned, sla_source=pr["key"])
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.press("right")
        assert [c.task_id for c in app.query_one("#list-ready").children] == [urgent, other, pinned]
        await pilot.click(f"#card-{pinned} .card-title")
        assert app.selected == pinned
        before = {id: store.get(id) for id in (pinned, other, urgent, other_column)}
        await app.refresh_board()
        assert {id: store.get(id) for id in before} == before
        # A manual source change changes the deadline used for ordering.
        store.update(pinned, sla_source=linked_issue["key"])
        history = store.history(pinned)
        await app.refresh_board()
        await pilot.pause()
        assert [c.task_id for c in app.query_one("#list-ready").children] == [urgent, pinned, other]
        assert app.selected == pinned
        assert app.query_one(f"#card-{pinned}").has_class("card-selected")
        assert store.history(pinned) == history
        assert [c.task_id for c in app.query_one("#list-in_progress").children] == [other_column]
        assert store.get(pinned)["status"] == "ready" and store.get(pinned)["priority"] == 2
        # Satisfying the pinned source removes its deadline rather than borrowing another.
        store.observe(linked_issue, "Remote", {"state": "closed"})
        assert card_schedule(store.get(pinned), store.sources(task_id=pinned))["due_at"] is None
        await app.refresh_board()
        await pilot.pause()
        assert [c.task_id for c in app.query_one("#list-ready").children] == [urgent, other, pinned]
        assert app.selected == pinned
