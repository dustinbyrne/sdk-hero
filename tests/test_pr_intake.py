from datetime import datetime
from urllib.parse import parse_qs, urlparse

import pytest

from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, parse_source

START = datetime.fromisoformat("2026-09-26T00:00:00+00:00")
OLD = "2026-09-20T12:00:00Z"
NEW = "2026-09-27T12:00:00Z"


def pr(number=1, **extra):
    return {
        "number": number,
        "title": f"Contribution {number}",
        "state": "open",
        "pull_request": {},
        "created_at": OLD,
        "updated_at": NEW,
        "author_association": "CONTRIBUTOR",
        "user": {"login": "contributor", "type": "User"},
        **extra,
    }


def detail(**extra):
    return {"state": "OPEN", "isDraft": False, "headRefOid": "head", "updatedAt": NEW, **extra}


def syncer(store, items, details, timeline=None, start=START):
    def runner(argv, **kwargs):
        if argv[:3] == ["gh", "pr", "view"]:
            return details[int(argv[3])]
        assert argv[:4] == ["gh", "api", "--method", "GET"]
        endpoint = argv[-1]
        if "/timeline?" in endpoint:
            return timeline or []
        if "?" in endpoint:
            page = int(parse_qs(urlparse(endpoint).query)["page"][0])
            return items[(page - 1) * 100 : page * 100]
        return next(i for i in items if i["number"] == int(endpoint.rsplit("/", 1)[1]))

    return Syncer(store, {"repos": ["org/sdk"]}, runner, window_start=start)


@pytest.mark.parametrize(
    "item_changes,detail_changes,eligible",
    [
        ({}, {}, True),
        ({"author_association": "NONE"}, {}, True),
        ({"author_association": "FIRST_TIME_CONTRIBUTOR"}, {}, True),
        ({"author_association": "MEMBER"}, {}, False),
        ({"author_association": "OWNER"}, {}, False),
        ({"author_association": "COLLABORATOR"}, {}, False),
        ({"user": {"login": "automation", "type": "Bot"}}, {}, False),
        ({"user": {"login": "automation[bot]", "type": "User"}}, {}, False),
        ({}, {"isDraft": True}, False),
        ({}, {"isDraft": None}, False),
        ({}, {"state": "MERGED"}, False),
        ({}, {"state": "CLOSED"}, False),
        ({"updated_at": OLD}, {"updatedAt": OLD}, False),
        ({}, {"updatedAt": START.isoformat()}, True),
    ],
)
def test_pr_intake_eligibility(tmp_path, item_changes, detail_changes, eligible):
    store = Store(tmp_path / "board.db")
    sync = syncer(store, [pr(**item_changes)], {1: detail(**detail_changes)})
    assert not any(sync.sync().values())
    assert len(store.tasks()) == int(eligible)
    if eligible:
        task = store.get(store.tasks()[0]["id"])
        assert task["status"] == "inbox" and task["priority"] == 2
        assert task["kind"] == "external_pr"
        assert task["sources"][0]["url"] == "https://github.com/org/sdk/pull/1"
        facts = task["sources"][0]["facts"]
        assert facts["review_waiting_since"] == "2026-09-20T12:00:00+00:00"
        assert facts["review_target_hours"] == 8


def test_pr_pagination_and_local_choices_survive_refresh(tmp_path):
    store = Store(tmp_path / "board.db")
    items = [pr(n, author_association="MEMBER") for n in range(1, 101)] + [pr(101)]
    details = {101: detail()}
    sync = syncer(store, items, details)
    assert not any(sync.sync().values())
    task_id = store.tasks()[0]["id"]
    store.update(
        task_id, title="My review", description="Existing plan", status="waiting", priority=1
    )
    history_count = len(store.history(task_id))
    assert not any(sync.sync().values())
    assert len(store.tasks()) == 1
    assert len(store.history(task_id)) == history_count
    # Tracked sources bypass intake, including after a new rotation and draft/closure changes.
    for changes in [{"isDraft": True}, {"isDraft": False, "state": "MERGED"}]:
        details[101].update(changes)
        sync = syncer(
            store, items, details, start=datetime.fromisoformat("2026-10-03T00:00:00+00:00")
        )
        assert not any(sync.sync().values())
        task = store.get(task_id)
        assert (task["title"], task["description"], task["status"], task["priority"]) == (
            "My review",
            "Existing plan",
            "done" if details[101]["state"] == "MERGED" else "waiting",
            1,
        )
        assert task["sources"][0]["facts"]["review_waiting_since"] is None


def test_draft_becoming_ready_uses_original_ready_timestamp(tmp_path):
    store = Store(tmp_path / "board.db")
    items, details = [pr()], {1: detail(isDraft=True)}
    timeline = [{"event": "ready_for_review", "created_at": NEW}]
    sync = syncer(store, items, details, timeline)
    assert not any(sync.sync().values()) and not store.tasks()
    details[1]["isDraft"] = False
    assert not any(sync.sync().values())
    assert store.sources()[0]["facts"]["review_waiting_since"] == "2026-09-27T12:00:00+00:00"


def test_existing_issue_card_link_and_dismissal(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Already tracking the problem")
    source = parse_source("https://github.com/org/sdk/pull/1", {})
    store.link(task_id, source)
    sync = syncer(store, [pr()], {1: detail()})
    assert not any(sync.sync().values())
    assert len(store.tasks()) == 1 and store.tasks()[0]["id"] == task_id
    store.delete(task_id, expected_revision=store.get(task_id)["revision"])
    assert not any(sync.sync().values())
    assert not store.tasks()


def test_team_pr_still_refreshes_when_explicitly_linked(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Team dependency")
    store.link(task_id, parse_source("https://github.com/org/sdk/pull/1", {}))
    sync = syncer(store, [pr(author_association="MEMBER")], {1: detail(isDraft=True)})
    assert not any(sync.sync().values())
    assert store.get(task_id)["sources"][0]["facts"]["isDraft"] is True
