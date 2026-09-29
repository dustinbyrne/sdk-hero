from datetime import datetime

import pytest
from textual.widgets import Select, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.sla import card_schedule
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import parse_source

MOMENT = datetime.fromisoformat("2026-09-28T16:01:00+00:00")
TASK = {"id": 1, "kind": "support", "status": "ready", "priority": 2}


def sources():
    return [
        {"key": "ticket", "kind": "ticket", "facts": {"sla_due_at": "2026-09-28T17:00:00Z"}},
        {
            "key": "pr",
            "kind": "pr",
            "facts": {"review_waiting_since": "2026-09-28T13:00:00Z", "review_target_hours": 4},
        },
        {
            "key": "issue",
            "kind": "issue",
            "facts": {"needs_team_reply": True, "awaiting_team_since": "2026-09-25T13:00:00Z"},
        },
    ]


def test_support_default_and_explicit_override():
    linked = sources()
    automatic = card_schedule(TASK, linked, MOMENT)
    assert automatic["badge"] == "59m" and "Support deadline" in automatic["tooltip"]
    for key, label in (("pr", "PR follow-up"), ("issue", "Issue response")):
        chosen = card_schedule({**TASK, "sla_source": key}, linked, MOMENT)
        assert label in chosen["tooltip"]
        assert chosen["group"] == automatic["group"]
        assert chosen["waiting_since"] == automatic["waiting_since"]
    assert card_schedule({**TASK, "sla_source": "ticket"}, linked, MOMENT) == automatic


def test_automatic_uses_latest_started_github_obligation_when_no_support_deadline():
    linked = sources()[1:]
    assert "PR follow-up" in card_schedule(TASK, linked, MOMENT)["tooltip"]
    linked[1]["facts"]["awaiting_team_since"] = "2026-09-28T14:00:00Z"
    assert "Issue response" in card_schedule(TASK, linked, MOMENT)["tooltip"]
    assert card_schedule(TASK, linked, MOMENT) == card_schedule(TASK, linked[::-1], MOMENT)
    # Latest start, not latest due: the older follow-up has a longer target window.
    linked[0] = {
        "key": "older",
        "kind": "issue",
        "facts": {
            "state": "open",
            "last_team_reply_at": "2026-09-28T13:00:00Z",
            "last_external_reply_at": "2026-09-25T13:00:00Z",
        },
    }
    assert "Issue response" in card_schedule(TASK, linked, MOMENT)["tooltip"]
    linked.append({"kind": "ticket", "facts": {}})
    assert "Issue response" in card_schedule(TASK, linked, MOMENT)["tooltip"]


@pytest.mark.parametrize("inactive", ["reviewed", "closed", "draft", "removed"])
def test_pinned_pr_does_not_fall_back_after_review(inactive):
    linked = sources()
    task = {**TASK, "sla_source": "pr"}
    assert card_schedule(task, linked, MOMENT)["badge"] == "59m"
    if inactive == "reviewed":
        linked[1]["facts"].update(review_waiting_since=None, review_target_hours=None)
    elif inactive == "closed":
        linked[1]["facts"]["state"] = "MERGED"
    elif inactive == "draft":
        linked[1]["facts"]["isDraft"] = True
    else:
        linked.pop(1)
    schedule = card_schedule(task, linked, MOMENT)
    assert schedule["badge"] == "" and schedule["due_at"] is None
    assert (
        "Support deadline" in card_schedule({**task, "sla_source": ""}, linked, MOMENT)["tooltip"]
    )


def test_sources_without_targets_and_done_cards():
    assert not card_schedule({**TASK, "sla_source": "run"}, sources(), MOMENT)["badge"]
    assert not card_schedule({**TASK, "sla_source": "pr", "status": "done"}, sources(), MOMENT)[
        "badge"
    ]


def test_sla_source_migration_validation_history_and_refresh(tmp_path):
    path = tmp_path / "board.db"
    store = Store(path)
    task = store.create("Card")
    other = store.create("Other")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task, source)
    before_history = store.history(task)
    with store.connect() as db:
        db.execute("ALTER TABLE tasks DROP COLUMN sla_source")
        db.execute("PRAGMA user_version=3")
    store = Store(path)
    assert store.get(task)["sla_source"] == ""
    assert store.history(task) == before_history
    revision = store.get(task)["revision"]
    store.update(task, sla_source=source["key"], expected_revision=revision)
    assert store.history(task)[-1]["details"]["sla_source"] == {
        "before": "",
        "after": source["key"],
    }
    with pytest.raises(Conflict):
        store.update(task, sla_source="", expected_revision=revision)
    with pytest.raises(ValueError, match="linked"):
        store.update(other, sla_source=source["key"])
    with pytest.raises(ValueError):
        store.update(task, sla_source=None)
    with pytest.raises(ValueError):
        store.create("Cannot select before linking", sla_source=source["key"])
    store.observe(source, "PR", {"state": "OPEN", "review_waiting_since": None})
    assert Store(path).get(task)["sla_source"] == source["key"]
    store.move_source(source["key"], other)
    assert store.get(task)["sla_source"] == source["key"]
    store.update(task, description="Keep detached selection")
    store.update(task, sla_source="")
    assert Store(path).get(task)["sla_source"] == ""


async def test_details_saves_sla_choice_and_preserves_draft_during_evidence_refresh(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Card")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task, source)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("e")
        selector = app.screen.query_one("#detail-sla-source", Select)
        assert selector.value == ""
        selector.value = source["key"]
        app.screen.query_one(TextArea).load_text("Unsaved plan")
        store.observe(source, "PR title", {"state": "OPEN"})
        new_source = parse_source("https://github.com/org/sdk/issues/2", {})
        store.link(task, new_source)
        await app.screen.refresh_evidence()
        assert selector.value == source["key"]
        selector.value = new_source["key"]
        selector.value = source["key"]
        assert app.screen.query_one(TextArea).text == "Unsaved plan"
        assert store.get(task)["sla_source"] == ""
        await pilot.click("#detail-save")
        assert store.get(task)["sla_source"] == source["key"]
        await pilot.click("#detail-close")
        await pilot.press("e")
        selector = app.screen.query_one("#detail-sla-source", Select)
        assert selector.value == source["key"]
        other = store.create("Other")
        store.move_source(source["key"], other)
        await app.screen.refresh_evidence()
        assert selector.value == source["key"]
        selector.value = ""
        await pilot.click("#detail-close")
        assert store.get(task)["sla_source"] == source["key"]
        await pilot.press("e")
        app.screen.query_one("#detail-sla-source", Select).value = ""
        await pilot.click("#detail-save")
        assert store.get(task)["sla_source"] == ""
