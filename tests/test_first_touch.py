import pytest

from sdk_support_hero.app import Board, needs_first_touch
from sdk_support_hero.store import Store
from sdk_support_hero.sync import conversation_facts, github_role, parse_source

FIRST = "2026-09-28T10:00:00Z"
REPLY = "2026-09-28T11:00:00Z"
LATER = "2026-09-28T12:00:00Z"


def issue(events):
    return {"kind": "issue", "facts": {"state": "open", **conversation_facts(events)}}


def test_external_issue_needs_first_touch_until_first_human_team_reply():
    task = {"status": "inbox"}
    events = [("external", FIRST)]
    bot = {"user": {"login": "helper[bot]", "type": "Bot"}, "author_association": "MEMBER"}
    events.append((github_role(bot), REPLY))
    assert needs_first_touch(task, [issue(events)])
    events.append(("team", REPLY))
    assert not needs_first_touch(task, [issue(events)])
    events.append(("external", LATER))
    assert issue(events)["facts"]["needs_team_reply"]
    assert not needs_first_touch(task, [issue(events)])


@pytest.mark.parametrize("kind", ["pr", "ticket", "run"])
def test_other_source_types_do_not_need_first_touch(kind):
    source = {**issue([("external", FIRST)]), "kind": kind}
    assert not needs_first_touch({"status": "inbox"}, [source])


def test_internal_closed_done_unknown_and_failed_sources_do_not_get_label():
    task = {"status": "ready"}
    untouched = issue([("external", FIRST)])
    assert not needs_first_touch(task, [issue([("team", FIRST)])])
    assert not needs_first_touch({"status": "done"}, [untouched])
    assert not needs_first_touch(task, [{**untouched, "error": "Refresh failed"}])
    assert not needs_first_touch(task, [{"kind": "issue", "facts": {"state": "open"}}])
    assert not needs_first_touch(
        task, [{**untouched, "facts": {**untouched["facts"], "state": "closed"}}]
    )
    assert not needs_first_touch(task, [])


def test_any_untouched_issue_can_label_a_card_with_multiple_sources():
    touched = issue([("external", FIRST), ("team", REPLY)])
    untouched = issue([("external", FIRST)])
    assert needs_first_touch({"status": "waiting"}, [touched, untouched])
    assert not needs_first_touch({"status": "waiting"}, [touched])


async def test_badge_updates_from_refreshed_facts_without_mutating_card(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    task_id = store.observe(source, "External issue", issue([("external", FIRST)])["facts"])
    before = store.get(task_id)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 45)) as pilot:
        assert str(app.query_one(".first-touch").render()) == "Needs first touch"
        assert store.get(task_id) == before
        facts = issue([("external", FIRST), ("team", REPLY)])["facts"]
        store.observe(source, "External issue", facts)
        after_refresh = store.get(task_id)
        await app.refresh_board()
        await pilot.pause()
        assert not app.query(".first-touch")
        assert store.get(task_id) == after_refresh
