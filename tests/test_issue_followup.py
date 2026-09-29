from datetime import datetime

from sdk_support_hero.sla import EASTERN, card_schedule
from sdk_support_hero.sync import conversation_facts


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=EASTERN)


TASK = {"id": 1, "kind": "issue", "status": "ready", "priority": 2}


def schedule(events, moment, state="open", task=TASK):
    source = {"kind": "issue", "facts": {"state": state, **conversation_facts(events)}}
    return card_schedule(task, [source], dt(moment))


def test_reply_followup_update_and_new_external_reply():
    events = [("external", "2026-09-28T13:00:00Z")]
    assert schedule(events, "2026-09-28T09:00")["badge"] == "Reply · 4h"
    events.append(("team", "2026-09-28T14:00:00Z"))
    result = schedule(events, "2026-09-28T10:00")
    assert result["badge"] == "Follow up · 16h"
    assert result["due_at"] == "2026-09-30T10:00:00-04:00"
    assert result["group"] == 3
    assert (
        schedule(events, "2026-09-28T10:00", task={**TASK, "kind": "external_pr"})["badge"]
        == "Follow up · 16h"
    )
    events.append((None, "2026-09-29T13:00:00Z"))
    assert schedule(events, "2026-09-28T10:00")["due_at"] == result["due_at"]
    events.append(("team", "2026-09-29T14:00:00Z"))
    assert schedule(events, "2026-09-29T10:00")["due_at"] == "2026-10-01T10:00:00-04:00"
    events.append(("external", "2026-09-29T15:00:00Z"))
    result = schedule(events, "2026-09-29T11:00")
    assert result["badge"] == "Reply · 4h" and result["group"] == 2
    events.append(("external", "2026-09-29T16:00:00Z"))
    assert schedule(events, "2026-09-29T12:00")["badge"] == "Reply · 3h"


def test_followup_weekend_dst_and_overdue():
    events = [("external", "2026-10-30T18:00:00Z"), ("team", "2026-10-30T20:00:00Z")]
    result = schedule(events, "2026-10-30T16:00")
    assert result["due_at"] == "2026-11-03T16:00:00-05:00"
    assert schedule(events, "2026-11-04T10:00")["badge"] == "Follow up · -2h"
    assert schedule(events, "2026-11-04T10:00", state="closed")["badge"] == ""
    assert schedule(events, "2026-11-04T10:00", task={**TASK, "status": "done"})["badge"] == ""
    assert (
        schedule(events, "2026-11-04T10:00", task={**TASK, "status": "waiting"})["badge"]
        == "Follow up · -2h"
    )


def test_internal_issue_without_external_conversation_has_no_followup():
    assert schedule([("team", "2026-09-28T13:00:00Z")], "2026-09-28T10:00")["badge"] == ""


async def test_followup_badge_on_card_without_history_ticks(tmp_path):
    from sdk_support_hero.app import Board
    from sdk_support_hero.store import Store
    from sdk_support_hero.sync import parse_source

    store = Store(tmp_path / "board.db")
    task = store.observe(
        parse_source("https://github.com/org/sdk/issues/1", {}),
        "Follow up",
        {
            "state": "open",
            **conversation_facts(
                [("external", "2026-09-28T13:00:00Z"), ("team", "2026-09-28T14:00:00Z")]
            ),
        },
        kind="issue",
    )
    before = store.history(task)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(80, 24)):
        assert "Follow up" in str(app.query_one(".sla-badge").render())
        await app.refresh_board()
        assert store.history(task) == before
