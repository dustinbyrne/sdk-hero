import json

import pytest
from textual.widgets import Static, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.sla import card_schedule
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import Syncer, parse_source, pr_review_facts

OLD = "2026-09-28T13:00:00Z"
TEAM = "2026-09-28T14:00:00Z"
NEW = "2026-09-28T15:00:00Z"
FACTS = {
    "state": "open",
    "last_external_reply_at": OLD,
    "last_team_reply_at": TEAM,
    "needs_team_reply": False,
    "awaiting_team_since": None,
}
REPLY = {
    **FACTS,
    "last_external_reply_at": NEW,
    "needs_team_reply": True,
    "awaiting_team_since": NEW,
}


def setup_card(tmp_path, status="done"):
    store = Store(tmp_path / "board.db")
    task = store.create(
        "Discussion", status=status, description="Our response is complete", priority=1
    )
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    store.observe(source, "Issue", FACTS)
    return store, task, source


@pytest.mark.parametrize("status", ["done", "waiting"])
@pytest.mark.parametrize("selection", ["automatic", "same", "other"])
def test_new_unanswered_reply_requeues_and_resets_only_a_different_pin(tmp_path, status, selection):
    store, task, source = setup_card(tmp_path, status)
    pin = ""
    if selection == "same":
        pin = source["key"]
    elif selection == "other":
        other = parse_source("https://github.com/org/sdk/pull/2", {})
        store.link(task, other)
        pin = other["key"]
    store.update(task, sla_source=pin)
    store.set_sla_resolved(task, store.sources(task_id=task)[0], True)
    revision = store.get(task)["revision"]
    store.observe(source, "Issue", REPLY)
    card = store.get(task)
    assert card["status"] == "inbox"
    assert card["sla_source"] == (pin if selection == "same" else "")
    assert card["priority"] == 1 and card["description"] == "Our response is complete"
    assert card["revision"] == revision + 1
    assert not card["sources"][0]["sla_resolved"]
    assert card_schedule(card, card["sources"])["badge"].startswith("Reply")
    entry = store.history(task)[-1]
    assert entry["kind"] == "reopened" and entry["details"]["url"] == source["url"]
    assert entry["details"]["changes"]["status"] == {"before": status, "after": "inbox"}
    assert ("sla_source" in entry["details"]["changes"]) == (selection == "other")
    with pytest.raises(Conflict):
        store.update(task, expected_revision=revision, status=status)
    history = store.history(task)
    store.observe(source, "Issue", REPLY)
    assert store.history(task) == history
    # Re-disposition after reading the reply is respected until another reply arrives.
    store.update(task, status=status)
    store.observe(source, "Issue", REPLY)
    assert store.get(task)["status"] == status


@pytest.mark.parametrize("status", ["inbox", "ready", "in_progress"])
def test_active_work_is_not_moved_or_revised(tmp_path, status):
    store, task, source = setup_card(tmp_path, status)
    revision = store.get(task)["revision"]
    store.observe(source, "Issue", REPLY)
    assert store.get(task)["status"] == status
    assert store.get(task)["revision"] == revision


@pytest.mark.parametrize(
    "facts",
    [
        FACTS,
        {**FACTS, "updated_at": NEW, "comments": 99},
        {**FACTS, "last_team_reply_at": NEW},
        {**REPLY, "needs_team_reply": False, "last_team_reply_at": "2026-09-28T16:00:00Z"},
        {**REPLY, "last_external_reply_at": OLD},
    ],
)
def test_metadata_own_replies_and_already_answered_replies_do_not_requeue(tmp_path, facts):
    store, task, source = setup_card(tmp_path)
    store.observe(source, "Updated title", facts)
    assert store.get(task)["status"] == "done"
    assert not any(u["kind"] == "reopened" for u in store.history(task))


def test_first_observation_is_a_baseline_not_a_new_reply(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Already handled", status="done")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    store.observe(source, "Issue", REPLY)
    assert store.get(task)["status"] == "done"
    store.observe(source, "Issue", {**REPLY, "last_external_reply_at": "2026-09-29T13:00:00Z"})
    assert store.get(task)["status"] == "inbox"


def test_first_external_reply_uses_last_observation_as_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr("sdk_support_hero.store.now", lambda: TEAM)
    store, task, source = setup_card(tmp_path)
    store.observe(source, "Issue", {**FACTS, "last_external_reply_at": None})
    store.observe(source, "Issue", REPLY)
    assert store.get(task)["status"] == "inbox"


def test_pr_conversation_tracks_human_comments_reviews_and_responses():
    item = {"created_at": OLD, "author_association": "NONE", "user": {"type": "User"}}
    data = {"state": "OPEN", "isDraft": False, "headRefOid": "head"}
    review = {
        "event": "reviewed",
        "state": "APPROVED",
        "submitted_at": TEAM,
        "author_association": "MEMBER",
        "user": {"type": "User"},
        "commit_id": "head",
    }
    reply = {
        "event": "commented",
        "created_at": NEW,
        "author_association": "NONE",
        "user": {"type": "User"},
    }
    facts = pr_review_facts(item, data, [review, reply])
    assert facts["needs_team_reply"] and facts["last_external_reply_at"].startswith(
        "2026-09-28T15:00"
    )
    assert facts["review_target_hours"] == 4
    bot = {**reply, "created_at": "2026-09-28T16:00:00Z", "user": {"type": "Bot"}}
    assert pr_review_facts(item, data, [review, bot])["last_external_reply_at"] is None
    answered = {**review, "submitted_at": "2026-09-28T16:00:00Z"}
    assert not pr_review_facts(item, data, [review, reply, answered])["needs_team_reply"]
    inline = {
        **review,
        "author_association": "CONTRIBUTOR",
        "submitted_at": NEW,
        "state": "COMMENTED",
    }
    assert pr_review_facts(item, data, [review, inline])["needs_team_reply"]
    # Explicitly linked team-authored PRs can also receive external questions.
    assert pr_review_facts({**item, "author_association": "MEMBER"}, data, [reply])[
        "needs_team_reply"
    ]


def test_pr_metadata_backfill_preserves_done_and_local_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr("sdk_support_hero.store.now", lambda: "2026-09-28T16:00:00Z")
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    facts = {"state": "OPEN", "review_waiting_since": OLD, "review_target_hours": 8}
    task = store.observe(source, "PR", facts)
    store.update(task, status="done")
    store.set_sla_resolved(task, store.sources(task_id=task)[0], True)
    history = store.history(task)
    enriched = {
        **facts,
        "last_external_reply_at": OLD,
        "last_team_reply_at": None,
        "needs_team_reply": True,
        "awaiting_team_since": OLD,
    }
    store.observe(source, "PR", enriched)
    assert store.history(task) == history
    assert store.get(task)["status"] == "done" and store.sources(task_id=task)[0]["sla_resolved"]
    store.observe(source, "PR", {**enriched, "last_external_reply_at": "2026-09-29T13:00:00Z"})
    assert (
        store.get(task)["status"] == "inbox" and not store.sources(task_id=task)[0]["sla_resolved"]
    )


def test_real_issue_sync_requeues_only_human_unanswered_reply(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Discussion", status="done")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    comments = []
    item = {
        "title": "Issue",
        "state": "open",
        "created_at": OLD,
        "updated_at": OLD,
        "labels": [],
        "user": {"login": "reporter", "type": "User"},
        "author_association": "NONE",
    }

    def runner(argv, **kwargs):
        if "/comments?" in argv[-1]:
            return comments
        assert argv[-1] == "repos/org/sdk/issues/1"
        return {**item, "comments": len(comments)}

    sync = Syncer(store, {"repos": []}, runner)
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"
    comments.append({"created_at": TEAM, "author_association": "MEMBER", "user": {"type": "Bot"}})
    sync.sync_card(task)
    assert store.get(task)["status"] == "done"
    comments.append({"created_at": NEW, "author_association": "NONE", "user": {"type": "User"}})
    sync.sync_card(task)
    assert store.get(task)["status"] == "inbox"


@pytest.mark.parametrize("author", ["NONE", "MEMBER"])
def test_real_pr_refresh_requeues_external_comment(tmp_path, monkeypatch, author):
    monkeypatch.setattr("sdk_support_hero.store.now", lambda: TEAM)
    store = Store(tmp_path / "board.db")
    task = store.create("PR", status="waiting")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task, source)
    timeline = [
        {
            "event": "reviewed",
            "state": "CHANGES_REQUESTED",
            "submitted_at": TEAM,
            "commit_id": "head",
            "user": {"type": "User"},
            "author_association": "MEMBER",
        }
    ]

    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            return {"state": "OPEN", "isDraft": False, "headRefOid": "head", "updatedAt": OLD}
        if "/timeline?" in argv[-1]:
            return timeline
        return {
            "title": "PR",
            "state": "open",
            "created_at": OLD,
            "updated_at": OLD,
            "pull_request": {},
            "author_association": author,
            "user": {"type": "User"},
        }

    sync = Syncer(store, {"repos": []}, runner)
    sync.sync_card(task)
    assert store.get(task)["status"] == "waiting"
    timeline.append(
        {
            "event": "commented",
            "created_at": NEW,
            "user": {"type": "User"},
            "author_association": "CONTRIBUTOR",
        }
    )
    sync.sync_card(task)
    assert store.get(task)["status"] == "inbox"


def test_support_refresh_ignores_ai_and_private_messages_but_requeues_customer(tmp_path):
    store = Store(tmp_path / "board.db")
    config = {"repos": [], "support": {"host": "https://support.example.com", "project": 4242}}
    task = store.create("Support", status="done")
    source = parse_source("https://support.example.com/project/4242/support/tickets/ticket", config)
    store.link(task, source)
    messages = [
        {"author_type": "customer", "created_at": OLD, "is_private": False},
        {"author_type": "support", "created_at": TEAM, "is_private": False},
    ]

    def runner(argv, **kwargs):
        assert json.loads(argv[-1])["id"] == "ticket"
        if argv[-2] == "conversations-tickets-retrieve":
            return {
                "id": "ticket",
                "ticket_number": 1,
                "_posthogUrl": source["url"],
                "status": "open",
                "message_count": len(messages),
                "sla_due_at": NEW,
            }
        assert argv[-2] == "conversations-tickets-messages-retrieve"
        return {"results": messages, "next": None}

    sync = Syncer(store, config, runner)
    sync.sync_card(task)
    for author, private in (("AI", False), ("customer", True)):
        messages.append({"author_type": author, "is_private": private, "created_at": NEW})
        sync.sync_card(task)
        assert store.get(task)["status"] == "done"
    messages.append({"author_type": "customer", "is_private": False, "created_at": NEW})
    sync.sync_card(task)
    assert store.get(task)["status"] == "inbox"


async def test_open_details_preserves_draft_and_cannot_undo_requeue(tmp_path):
    store, task, source = setup_card(tmp_path, "waiting")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.press("right", "right", "right", "e")
        app.screen.query_one(TextArea).load_text("Unsaved plan")
        store.observe(source, "Issue", REPLY)
        await app.screen.refresh_evidence()
        assert "changed elsewhere" in str(app.screen.query_one("#detail-warning", Static).render())
        await pilot.click("#detail-save")
        assert store.get(task)["status"] == "inbox"
        assert app.screen.query_one(TextArea).text == "Unsaved plan"
        await pilot.click("#detail-close")
        assert [c.task_id for c in app.query_one("#list-inbox").children] == [task]
        assert not list(app.query_one("#list-waiting").children)
