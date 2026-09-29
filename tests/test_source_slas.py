from datetime import datetime, timedelta

from textual.widgets import Link, Select, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.sla import card_schedule
from sdk_support_hero.store import Store
from sdk_support_hero.sync import parse_source


async def test_source_types_individual_slas_and_countdowns_preserve_drafts_and_history(
    tmp_path, monkeypatch
):
    moment = [datetime.fromisoformat("2026-09-28T16:01:00+00:00")]
    monkeypatch.setattr(
        "sdk_support_hero.app.card_schedule",
        lambda task, sources: card_schedule(task, sources, moment[0]),
    )
    store = Store(tmp_path / "board.db")
    task = store.create("Combined card")
    config = {"repos": [], "support": {"host": "https://support.example.com", "project": 4242}}
    specs = [
        (
            "https://support.example.com/project/4242/support/tickets/ticket",
            "Support ticket",
            {"status": "open", "sla_due_at": "2026-09-28T17:00:00Z"},
        ),
        (
            "https://github.com/org/sdk/issues/1",
            "Issue",
            {
                "state": "open",
                "needs_team_reply": True,
                "awaiting_team_since": "2026-09-25T13:00:00Z",
            },
        ),
        (
            "https://github.com/org/sdk/pull/2",
            "Pull request",
            {
                "state": "OPEN",
                "review_waiting_since": "2026-09-28T14:00:00Z",
                "review_target_hours": 4,
            },
        ),
        ("https://github.com/org/sdk/issues/3", "Issue", {"state": "closed"}),
        ("https://github.com/org/sdk/actions/runs/4", "Workflow run", {"status": "completed"}),
    ]
    for url, _, facts in specs:
        source = parse_source(url, config)
        store.link(task, source)
        store.observe(source, "Remote title", facts)
    source_rows = store.sources(task_id=task)
    pinned = source_rows[2]["key"]
    store.update(task, sla_source=pinned)
    history = store.history(task)
    app = Board(store, config)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("e")
        screen = app.screen
        links = list(screen.query_one("#detail-sources").query(Link))
        assert [str(link.render()) for link in links] == [
            f"{source_type} · Remote title" for _, source_type, _ in specs
        ]
        assert [link.url for link in links] == [url for url, _, _ in specs]
        labels = list(screen.query(".source-sla"))
        assert [str(label.render()) for label in labels] == [
            "SLA: 59m",
            "SLA: Reply · -7h",
            "SLA: 1h",
            "SLA: No active SLA",
            "SLA: No active SLA",
        ]
        assert labels[0].has_class("soon") and labels[1].has_class("overdue")
        assert specs[2][0] in labels[2].tooltip
        assert "due" in labels[2].tooltip
        screen.query_one(TextArea).load_text("Unsaved plan")
        screen.query_one("#detail-sla-source", Select).value = source_rows[1]["key"]
        moment[0] += timedelta(minutes=1)
        await screen.refresh_evidence()
        assert list(screen.query(".source-sla"))[0] is labels[0]
        assert str(labels[0].render()) == "SLA: 58m"
        assert screen.query_one(TextArea).text == "Unsaved plan"
        assert screen.query_one("#detail-sla-source", Select).value == source_rows[1]["key"]
        assert store.history(task) == history
        assert store.get(task)["sla_source"] == pinned
        # A completed review clears only the PR's clock, not other sources' displays.
        store.observe(
            source_rows[2], "Remote title", {"state": "OPEN", "review_waiting_since": None}
        )
        await screen.refresh_evidence()
        labels = list(screen.query(".source-sla"))
        assert str(labels[2].render()) == "SLA: No active SLA"
        assert str(labels[0].render()) == "SLA: 58m"
        assert str(labels[1].render()).startswith("SLA: Reply · -")
        assert not card_schedule(store.get(task), store.sources(task_id=task), moment[0])["badge"]
