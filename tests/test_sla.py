from datetime import datetime

import pytest

from sdk_support_hero.sla import (
    EASTERN,
    add_business_hours,
    business_seconds,
    card_schedule,
    compact_time,
    work_key,
)
from sdk_support_hero.sync import conversation_facts, pr_review_facts


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=EASTERN)


@pytest.mark.parametrize(
    "start,hours,expected",
    [
        ("2026-09-28T09:00", 4, "2026-09-28T13:00"),
        ("2026-09-28T15:00", 4, "2026-09-29T11:00"),
        ("2026-09-25T16:00", 8, "2026-09-28T16:00"),
        ("2026-09-26T12:00", 4, "2026-09-28T13:00"),
        ("2026-09-28T17:00", 4, "2026-09-29T13:00"),
        ("2026-10-30T16:00", 8, "2026-11-02T16:00"),
        ("2026-03-06T16:00", 8, "2026-03-09T16:00"),
    ],
)
def test_business_deadlines(start, hours, expected):
    due = add_business_hours(dt(start), hours)
    assert due == dt(expected)
    assert business_seconds(dt(start), due) == hours * 3600


def test_overdue_pauses_overnight_weekends_and_dst():
    assert business_seconds(dt("2026-09-28T10:00"), dt("2026-09-25T16:00")) == -7200
    assert business_seconds(dt("2026-09-25T17:00"), dt("2026-09-28T09:00")) == 0
    assert business_seconds(dt("2026-10-30T16:00"), dt("2026-11-02T10:00")) == 7200


@pytest.mark.parametrize(
    "seconds,text",
    [(28800, "8h"), (1800, "30m"), (-7200, "-2h"), (0, "0m"), (1, "1m"), (-1, "-1m")],
)
def test_compact_badges(seconds, text):
    assert compact_time(seconds) == text


def test_first_unanswered_message_not_latest():
    events = [
        ("external", "2026-09-28T10:00:00Z"),
        ("external", "2026-09-28T11:00:00Z"),
        (None, "2026-09-28T12:00:00Z"),
    ]
    assert conversation_facts(events)["awaiting_team_since"] == "2026-09-28T10:00:00+00:00"
    events.append(("team", "2026-09-28T13:00:00Z"))
    assert conversation_facts(events)["awaiting_team_since"] is None
    events.append(("external", "2026-09-28T14:00:00Z"))
    assert conversation_facts(events)["awaiting_team_since"] == "2026-09-28T14:00:00+00:00"


def event(kind, date, role="NONE", **extras):
    return {
        "event": kind,
        "created_at": date,
        "submitted_at": date,
        "author_association": role,
        "user": {"login": "person", "type": "User"},
        **extras,
    }


def pr(timeline=(), previous=None, **extra):
    item = {
        "created_at": "2026-09-28T13:00:00Z",
        "author_association": "NONE",
        "user": {"login": "contributor"},
    }
    data = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "head",
        "updatedAt": "2026-09-28T17:00:00Z",
        **extra,
    }
    return pr_review_facts(item, data, list(timeline), previous)


def test_initial_review_ignores_acknowledgments_and_followups():
    result = pr(
        [
            event("commented", "2026-09-28T14:00:00Z", "MEMBER"),
            event("commented", "2026-09-28T15:00:00Z"),
        ]
    )
    assert result["review_target_hours"] == 8
    assert result["review_waiting_since"] == "2026-09-28T13:00:00+00:00"


def test_review_followup_and_head_clock_does_not_reset():
    review = event(
        "reviewed", "2026-09-28T14:00:00Z", "MEMBER", state="changes_requested", commit_id="old"
    )
    first = pr([review])
    assert first["review_target_hours"] == 4
    assert first["review_waiting_since"] == "2026-09-28T17:00:00+00:00"
    previous = {**first, "headRefOid": "head"}
    same = pr([review], previous, updatedAt="2026-09-29T15:00:00Z")
    assert same == first
    updated = pr([review], previous, headRefOid="next", updatedAt="2026-09-29T15:00:00Z")
    assert updated["review_waiting_since"] == first["review_waiting_since"]
    reply = event("commented", "2026-09-28T16:00:00Z")
    assert pr([review, reply])["review_waiting_since"] == "2026-09-28T16:00:00+00:00"


def test_team_review_satisfies_current_head_and_bots_do_not():
    review = event("reviewed", "2026-09-28T16:00:00Z", "MEMBER", state="approved", commit_id="head")
    assert pr([review])["review_waiting_since"] is None
    bot = {**review, "user": {"login": "review-bot", "type": "Bot"}}
    assert pr([bot])["review_target_hours"] == 8
    comment = event("commented", "2026-09-28T17:00:00Z")
    result = pr([review, comment])
    assert result["review_target_hours"] == 4
    assert (
        pr([review, comment, event("commented", "2026-09-28T18:00:00Z", "MEMBER")])[
            "review_waiting_since"
        ]
        is None
    )


def test_draft_ready_and_closed():
    ready = event("ready_for_review", "2026-09-29T13:00:00Z")
    assert pr([ready])["review_waiting_since"] == "2026-09-29T13:00:00+00:00"
    assert pr(isDraft=True)["review_waiting_since"] is None
    assert pr(state="MERGED")["review_waiting_since"] is None


def test_return_to_draft_and_dismissed_review():
    timeline = [
        event("ready_for_review", "2026-09-28T14:00:00Z"),
        event("converted_to_draft", "2026-09-28T15:00:00Z"),
        event("ready_for_review", "2026-09-28T16:00:00Z"),
    ]
    assert pr(timeline)["review_waiting_since"] == "2026-09-28T16:00:00+00:00"
    review = event("reviewed", "2026-09-28T14:00:00Z", "MEMBER", state="dismissed", commit_id="old")
    reply = event("commented", "2026-09-28T16:00:00Z")
    result = pr([review, reply])
    assert result["review_waiting_since"] == "2026-09-28T16:00:00+00:00"
    assert result["review_target_hours"] == 4


def test_consolidated_support_age_is_independent_of_deadlines():
    task = {"id": 1, "kind": "support", "status": "ready", "priority": 2}
    sources = [
        {"kind": "ticket", "facts": {"awaiting_team_since": "2026-09-25T13:00:00Z"}},
        {
            "kind": "ticket",
            "facts": {
                "awaiting_team_since": "2026-09-28T13:00:00Z",
                "sla_due_at": "2026-09-29T17:00:00Z",
            },
        },
    ]
    result = card_schedule(task, sources)
    assert result["waiting_since"] == "2026-09-25T13:00:00+00:00"
    assert result["due_at"] == "2026-09-29T17:00:00+00:00"
    sources[0]["facts"]["status"] = "resolved"
    assert card_schedule(task, sources)["waiting_since"] == "2026-09-28T13:00:00+00:00"


def test_support_deadline_and_source_precedence():
    task = {"id": 1, "kind": "support", "status": "ready", "priority": 2}
    sources = [
        {"kind": "ticket", "facts": {"sla_due_at": "2026-09-28T17:00:00Z"}},
        {
            "kind": "issue",
            "facts": {"needs_team_reply": True, "awaiting_team_since": "2026-09-25T13:00:00Z"},
        },
    ]
    result = card_schedule(task, sources, dt("2026-09-28T12:30"))
    assert result["badge"] == "30m" and result["group"] == 0
    sources[0]["facts"].pop("sla_due_at")
    assert card_schedule(task, sources, dt("2026-09-28T12:30"))["badge"].startswith("Reply · -")


def test_work_order_ties_use_category_then_oldest_request():
    def task(id, priority=2):
        return {"id": id, "priority": priority}

    support = {"group": 0, "waiting_since": "2026-09-28T14:00:00Z"}
    pr_clock = {"group": 1, "waiting_since": "2026-09-28T13:00:00Z"}
    older = {"group": 1, "waiting_since": "2026-09-25T13:00:00Z"}
    assert work_key(task(1), support) < work_key(task(2), older) < work_key(task(3), pr_clock)
    assert work_key(task(4, 0), pr_clock) < work_key(task(1), support)


def test_pr_refresh_preserves_clock_and_history(tmp_path):
    from sdk_support_hero.store import Store
    from sdk_support_hero.sync import Syncer, parse_source

    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    task = store.create("Review", kind="external_pr")
    store.link(task, source)
    data = {
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": "new",
        "updatedAt": "2026-09-28T17:00:00Z",
        "statusCheckRollup": [],
    }
    review = event(
        "reviewed", "2026-09-28T14:00:00Z", "MEMBER", state="changes_requested", commit_id="old"
    )

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            return data
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        if "/timeline?" in argv[-1]:
            return [review]
        return {
            "title": "PR",
            "state": "open",
            "pull_request": {},
            "labels": [],
            "created_at": "2026-09-28T13:00:00Z",
            "author_association": "NONE",
            "user": {"login": "contributor"},
        }

    sync = Syncer(store, {"repos": []}, runner)
    assert not any(sync.sync().values())
    count = len(store.history(task))
    data["updatedAt"] = "2026-09-29T16:00:00Z"
    assert not any(sync.sync().values())
    assert len(store.history(task)) == count
    assert (
        store.get(task)["sources"][0]["facts"]["review_waiting_since"]
        == "2026-09-28T17:00:00+00:00"
    )
    data["headRefOid"] = "another-head"
    assert not any(sync.sync().values())
    assert (
        store.get(task)["sources"][0]["facts"]["review_waiting_since"]
        == "2026-09-28T17:00:00+00:00"
    )


async def test_badge_and_sorted_cards_do_not_write_history(tmp_path):
    from sdk_support_hero.app import Board
    from sdk_support_hero.store import Store
    from sdk_support_hero.sync import parse_source

    store = Store(tmp_path / "board.db")
    issue = store.observe(
        parse_source("https://github.com/org/sdk/issues/1", {}),
        "Issue",
        {"state": "open", "needs_team_reply": True, "awaiting_team_since": "2026-09-28T13:00:00Z"},
        kind="issue",
    )
    support = store.create("Support", kind="support")
    for id in (issue, support):
        store.update(id, status="ready")
    before = store.history(issue)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(100, 32)) as pilot:
        listing = app.query_one("#list-ready")
        assert [c.task_id for c in listing.children] == [issue, support]
        assert app.query_one(f"#card-{issue} .sla-badge")
        await app.refresh_board()
        assert store.history(issue) == before
        await pilot.press("right")
        await pilot.click(f"#card-{issue} .sla-badge")
        assert app.screen.task_id == issue
