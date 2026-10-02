import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, rotation_start

START = datetime.fromisoformat("2026-09-26T00:00:00+00:00")
OLD = "2026-09-25T12:00:00Z"
NEW = "2026-09-27T12:00:00Z"
LATER = "2026-09-28T12:00:00Z"


def comment(date, association="NONE", **extra):
    return {
        "created_at": date,
        "author_association": association,
        "user": {"login": "someone", "type": "User"},
        **extra,
    }


def issue(number, created=OLD, comments=0, **extra):
    return {
        **comment(created),
        "number": number,
        "title": f"Issue {number}",
        "comments": comments,
        "state": "open",
        "labels": [],
        **extra,
    }


def test_local_saturday_boundary_and_dst():
    zone = ZoneInfo("America/New_York")
    for date, expected in [
        ("2026-09-28T12:00", "2026-09-26"),
        ("2026-10-02T23:59", "2026-09-26"),
        ("2026-10-03T00:00", "2026-10-03"),
        ("2026-11-02T12:00", "2026-10-31"),
    ]:
        start = rotation_start(datetime.fromisoformat(date).replace(tzinfo=zone))
        assert start.astimezone(zone).isoformat() == expected + "T00:00:00-04:00"


def test_focused_github_intake_and_history(tmp_path):
    store = Store(tmp_path / "board.db")
    items = [
        issue(1, NEW),
        issue(2),
        issue(3, comments=1),
        issue(4, NEW, 2),
        issue(5, comments=2),
        issue(6, NEW, user={"login": "robot[bot]"}),
        issue(7, NEW, pull_request={}, author_association="MEMBER"),
        issue(8, NEW, author_association="MEMBER"),
    ]
    threads = {
        3: [comment(NEW)],
        4: [comment(NEW), comment(LATER, "MEMBER")],
        5: [comment(NEW), comment(LATER, user={"login": "robot[bot]"})],
    }
    calls = []

    def runner(argv, **kwargs):
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        endpoint = argv[-1]
        calls.append(endpoint)
        if "/comments?" in endpoint:
            return threads[int(endpoint.split("/")[4])]
        if "?" in endpoint:
            assert parse_qs(urlparse(endpoint).query)["since"] == [syncer.since]
            return items
        return next(item for item in items if item["number"] == int(endpoint.rsplit("/", 1)[1]))

    syncer = Syncer(store, {"repos": ["org/sdk"]}, runner, window_start=START)
    assert not any(syncer.sync().values())
    assert {t["title"] for t in store.tasks()} == {"Issue 1", "Issue 3", "Issue 5"}
    task = next(t for t in store.tasks() if t["title"] == "Issue 3")
    store.update(task["id"], description="Verify a fix", status="waiting", priority=0)
    count = len(store.history(task["id"]))
    assert not any(syncer.sync().values())
    assert len(store.history(task["id"])) == count
    threads[3].append(comment(LATER, "MEMBER"))
    items[2]["comments"] = 2
    assert not any(syncer.sync().values())
    current = store.get(task["id"])
    assert current["description"] == "Verify a fix" and current["status"] == "waiting"
    assert current["priority"] == 0 and len(current["updates"]) == count + 1
    assert current["sources"][0]["facts"]["needs_team_reply"] is False
    # Outside the intake window and no longer open, but still tracked.
    items[2]["state"] = "closed"
    syncer = Syncer(
        store,
        {"repos": ["org/sdk"]},
        runner,
        window_start=datetime.fromisoformat("2026-10-03T00:00:00+00:00"),
    )
    assert not any(syncer.sync().values())
    assert store.get(task["id"])["sources"][0]["facts"]["state"] == "closed"


def test_retained_syncer_rolls_window_forward(tmp_path, monkeypatch):
    store = Store(tmp_path / "board.db")
    clock = [START]
    monkeypatch.setattr("sdk_support_hero.sync.rotation_start", lambda: clock[0])
    seen = []
    items = [issue(1, NEW)]

    def runner(argv, **kwargs):
        if "?" in argv[-1]:
            seen.append(parse_qs(urlparse(argv[-1]).query)["since"][0])
            return items
        return items[0]

    syncer = Syncer(store, {"repos": ["org/sdk"]}, runner)
    assert not any(syncer.sync().values())
    clock[0] = datetime.fromisoformat("2026-10-03T00:00:00+00:00")
    items.append(issue(2, NEW))
    assert not any(syncer.sync().values())
    assert seen == [START.isoformat(), clock[0].isoformat()]
    assert [task["title"] for task in store.tasks()] == ["Issue 1"]


def test_github_reads_all_comment_pages_before_deciding(tmp_path):
    store = Store(tmp_path / "board.db")

    def runner(argv, **kwargs):
        endpoint = argv[-1]
        if "/comments?" not in endpoint:
            return [issue(1, NEW, 101)]
        if endpoint.endswith("page=1"):
            return [comment(NEW)] * 100
        return [comment(LATER, "MEMBER")]

    assert not any(
        Syncer(store, {"repos": ["org/sdk"]}, runner, window_start=START).sync().values()
    )
    assert store.tasks() == []


def message(author, date, private=False):
    return {
        "author_type": author,
        "created_at": date,
        "is_private": private,
        "content": "SECRET",
        "author_email": "SECRET",
    }


def test_support_intake_private_notes_ai_pagination_and_history(tmp_path):
    store = Store(tmp_path / "board.db")
    support = {
        "host": "https://support.example.com",
        "project": 4242,
        "view": "sdk",
        "view_name": "Example queue",
    }
    threads = {
        "new": [message("customer", NEW)],
        "old": [message("customer", OLD)],
        "answered": [message("customer", NEW), message("support", LATER)],
        "private": [message("customer", NEW), message("support", LATER, True)],
        "ai": [message("customer", NEW), message("AI", LATER)],
        "internal": [message("customer", OLD), message("support", NEW, True)],
        "resolved": [message("customer", NEW)],
        "second-page": [message("customer", NEW), message("support", LATER)],
        "pending": [message("customer", OLD), message("support", NEW)],
        "on_hold": [message("customer", OLD)],
        "empty": [],
    }

    def runner(argv, **kwargs):
        tool, args = argv[6], json.loads(argv[7])
        assert kwargs["env"] == {"POSTHOG_CLI_PROJECT_ID": "4242"}
        if tool == "conversations-views-retrieve":
            return {
                "name": "Example queue",
                "filters": {"assignee": {"id": "role", "type": "role"}},
            }
        if tool == "conversations-tickets-list":
            assert args["date_from"] == "all"
            assert args["assignee"] == "role:role"
            assert args["status"] == "new,open,pending,on_hold"
            return {"results": [{"id": key} for key in threads], "next": None}
        key = args["id"]
        if tool == "conversations-tickets-messages-retrieve":
            if key == "second-page":
                return {
                    "results": [threads[key][0 if args["offset"] == 0 else 1]],
                    "next": "more" if args["offset"] == 0 else None,
                }
            return {"results": threads[key], "next": None}
        assert tool == "conversations-tickets-retrieve"
        return {
            "id": key,
            "ticket_number": key,
            "status": key if key in {"resolved", "pending", "on_hold"} else "open",
            "message_count": len(threads[key]),
            "_posthogUrl": f"https://support.example.com/project/4242/support/tickets/{key}",
        }

    syncer = Syncer(store, {"repos": [], "support": support}, runner, window_start=START)
    assert not any(syncer.sync().values())
    assert {s["remote_id"] for s in store.sources()} == set(threads) - {"resolved"}
    card = next(s["task_id"] for s in store.sources() if s["remote_id"] == "new")
    count = len(store.history(card))
    assert not any(syncer.sync().values())
    assert len(store.history(card)) == count
    threads["new"].append(message("support", LATER))
    assert not any(syncer.sync().values())
    assert len(store.history(card)) == count + 1
    assert store.get(card)["sources"][0]["facts"]["needs_team_reply"] is False
    assert "SECRET" not in json.dumps(store.get(card))
    answered = next(s["task_id"] for s in store.sources() if s["remote_id"] == "answered")
    store.update(
        answered, status="waiting", title="Local plan", description="Await diagnostics", priority=1
    )
    deleted = next(s["task_id"] for s in store.sources() if s["remote_id"] == "old")
    store.delete(deleted, expected_revision=store.get(deleted)["revision"])
    before = store.get(answered)
    assert not any(syncer.sync().values())
    assert store.get(answered) == before
    assert "old" not in {s["remote_id"] for s in store.sources()}


@pytest.mark.parametrize("bad", [None, "not-a-date", "2026-09-28"])
def test_bad_timestamps_do_not_import_or_claim_success(tmp_path, bad):
    store = Store(tmp_path / "board.db")

    def runner(argv, **kwargs):
        return [issue(1, bad)]

    results = Syncer(store, {"repos": ["org/sdk"]}, runner, window_start=START).sync()
    assert results["github:org/sdk"]
    assert store.tasks() == []
    assert store.sync_states()[0]["succeeded_at"] is None
