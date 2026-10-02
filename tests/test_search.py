from textual.widgets import Input

from sdk_support_hero.app import Board, Card, matches_search
from sdk_support_hero.store import Store
from sdk_support_hero.sync import parse_source


def test_search_matches_exact_card_number_and_visible_labels(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create("Card mentioning #16", description="Saved description")
    task = store.get(task_id)
    task["needs_first_touch"] = True
    task["agent_states"] = ["done"]
    assert matches_search(task, f"  #{task_id}  ")
    assert not matches_search(task, "#16")
    for query in ("NEEDS FIRST TOUCH", "github", "done unread", "Pi", "saved description", ""):
        assert matches_search(task, query, "GitHub")
    task["needs_first_touch"] = False
    assert not matches_search(task, "needs first touch")
    assert not matches_search(task, "support", "GitHub")


def test_fuzzy_search_matches_terms_across_fields_and_repositories(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.get(store.create("Retry capture", sdk="python", kind="issue", priority=1))
    repos = ["posthog/posthog-python", "example/linked-repo"]
    for query in ("pythn P1 issu retry", "P1 python posthog/posthog-python", "lnkd-rep captr", ""):
        assert matches_search(task, query, "GitHub", repos)
    assert not matches_search(task, "python p0", "GitHub", repos)
    assert not matches_search(task, "python absent", "GitHub", repos)
    assert not matches_search(task, "python p1 absent/repo", "GitHub", repos)
    assert matches_search(task, f"#{task['id']} pythn p1", repositories=repos)
    assert not matches_search(task, "#99 pythn", repositories=repos)
    task["description"] = "Investigate p0 reports"
    assert not matches_search(task, "p0")
    task["kind"] = "external_pr"
    assert matches_search(task, "external pr")


async def test_search_combines_terms_and_updates_derived_labels(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    facts = {"state": "open", "last_team_reply_at": None, "needs_team_reply": True}
    task_id = store.observe(source, "External issue", facts)
    other = store.create("Another card")
    app = Board(store, {"repos": []})
    async with app.run_test(size=(150, 45)) as pilot:
        search = app.query_one("#search", Input)
        search.value = "needs first touch"
        await pilot.pause()
        assert [card.task_id for card in app.query(Card)] == [task_id]
        store.observe(
            source,
            "External issue",
            {**facts, "last_team_reply_at": "2026-09-29T12:00:00Z", "needs_team_reply": False},
        )
        await app.refresh_board()
        assert not app.query(Card)
        search.value = f"#{other}"
        await pilot.pause()
        assert [card.task_id for card in app.query(Card)] == [other]
        search.value = f"#{other} p0"
        await pilot.pause()
        assert not app.query(Card)
        search.value = "github org/sdk p2"
        await pilot.pause()
        assert [card.task_id for card in app.query(Card)] == [task_id]
