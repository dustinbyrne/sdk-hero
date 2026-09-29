import pytest
from textual.widgets import Button, Input, TextArea

from sdk_support_hero.app import Board
from sdk_support_hero.investigate import Investigator, LaunchError
from sdk_support_hero.store import Store


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/fixture/herdr.sock")
    monkeypatch.setattr("sdk_support_hero.investigate.shutil.which", lambda _: "/bin/herdr")
    store = Store(tmp_path / "board.db")
    task = store.create("Card")
    details = {
        "run_id": "test-run",
        "session_file": str(tmp_path / "session.jsonl"),
        "herdr_socket": "/fixture/herdr.sock",
        "stage": "started",
        "agent_name": "original-name",
        "pane_id": "old-pane",
    }
    store.investigation_update(task, "Started", details)
    calls = []
    agents = [
        {
            "agent": "pi",
            "name": "renamed",
            "pane_id": "moved-pane",
            "agent_session": {"kind": "path", "value": details["session_file"]},
            "agent_status": "working",
        }
    ]

    def runner(args):
        calls.append(args)
        assert args == ["agent", "list"]
        return {"agents": agents}

    return store, task, details, agents, calls, Investigator(store, runner=runner)


def test_states_follow_session_identity_and_do_not_write(setup):
    store, task, details, agents, calls, investigator = setup
    store.investigation_update(task, "Repeated checkpoint", details)
    before = store.get(task)
    for state in ("working", "done", "idle", "blocked", "unknown"):
        agents[0]["agent_status"] = state
        assert investigator.card_states() == {task: [state]}
    assert calls == [["agent", "list"]] * 5
    assert store.get(task) == before
    assert len(store.investigation_sessions()) == 1
    agents[0]["agent_session"]["value"] += "-replacement"
    assert investigator.card_states() == {}


def test_multiple_live_sessions_and_closed_session(setup):
    store, task, details, agents, calls, investigator = setup
    second = {**details, "run_id": "second", "session_file": details["session_file"] + "-second"}
    store.investigation_update(task, "Started another", second)
    agents.append(
        {
            **agents[0],
            "agent_session": {"kind": "path", "value": second["session_file"]},
            "agent_status": "done",
        }
    )
    assert investigator.card_states() == {task: ["done", "working"]}
    assert len(calls) == 1
    agents.clear()
    assert investigator.card_states() == {}


def test_no_queries_outside_herdr_or_for_another_server(setup, monkeypatch):
    store, task, details, agents, calls, investigator = setup
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/other/socket")
    assert investigator.card_states() == {}
    monkeypatch.setenv("HERDR_SOCKET_PATH", details["herdr_socket"])
    monkeypatch.setenv("HERDR_ENV", "0")
    assert investigator.card_states() == {}
    assert not calls


def test_failure_is_unavailable_not_stale_working(setup):
    store, task, details, agents, calls, investigator = setup
    assert investigator.card_states() == {task: ["working"]}

    def failed(args):
        raise LaunchError("timeout")

    investigator.runner = failed
    assert investigator.card_states() == {task: ["unavailable"]}


def test_no_query_without_recorded_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr("sdk_support_hero.investigate.shutil.which", lambda _: "/bin/herdr")
    store = Store(tmp_path / "board.db")
    investigator = Investigator(store, runner=lambda _: pytest.fail("Unexpected Herdr query"))
    assert investigator.card_states() == {}


def test_jump_uses_live_identity_and_rechecks_session(setup):
    store, task, details, agents, calls, investigator = setup

    def runner(args):
        calls.append(args)
        if args == ["agent", "list"]:
            return {"agents": agents}
        if args == ["agent", "get", "renamed"]:
            return {"agent": agents[0]}
        if args == ["agent", "focus", "renamed"]:
            return {"agent": {"tab_id": "moved-workspace:current-tab"}}
        assert args == ["tab", "focus", "moved-workspace:current-tab"]
        return {}

    investigator.runner = runner
    investigator.focus_card(task)
    assert calls[-2:] == [
        ["agent", "focus", "renamed"],
        ["tab", "focus", "moved-workspace:current-tab"],
    ]
    agents.clear()
    calls.clear()
    with pytest.raises(LaunchError, match="no longer has a live"):
        investigator.focus_card(task)
    assert calls == [["agent", "list"]]


def test_jump_rejects_replaced_session_before_focusing(setup):
    store, task, details, agents, calls, investigator = setup

    def runner(args):
        calls.append(args)
        if args == ["agent", "list"]:
            return {"agents": agents}
        assert args == ["agent", "get", "renamed"]
        return {"agent": {"agent_session": {"kind": "path", "value": "/another/session"}}}

    investigator.runner = runner
    with pytest.raises(LaunchError, match="no longer in that pane"):
        investigator.focus_card(task)
    assert not any(call[:2] == ["agent", "focus"] for call in calls)


async def test_details_jump_button_preserves_drafts(setup, monkeypatch):
    store, task, details, agents, calls, investigator = setup
    focused = []
    monkeypatch.setattr(investigator, "focus_card", focused.append)
    app = Board(store, {"repos": []}, investigator=investigator)
    before = store.get(task)
    async with app.run_test(size=(150, 45)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("e")
        app.screen.query_one("#detail-title", Input).value = "Unsaved title"
        assert app.screen.query_one("#detail-open-pi", Button).display
        await pilot.click("#detail-open-pi")
        await app.workers.wait_for_complete()
        assert focused == [task]
        assert app.screen.query_one("#detail-title", Input).value == "Unsaved title"
        assert store.get(task) == before
        agents.clear()
        app.poll_agent_states()
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.screen.refresh_evidence()
        assert not app.screen.query_one("#detail-open-pi", Button).display


@pytest.mark.parametrize("inside_herdr", [True, False])
async def test_session_buttons_visibility_without_live_session(setup, monkeypatch, inside_herdr):
    store, task, details, agents, calls, investigator = setup
    agents.clear()
    monkeypatch.setenv("HERDR_ENV", "1" if inside_herdr else "0")
    app = Board(store, {"repos": []}, investigator=investigator)
    async with app.run_test(size=(150, 45)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.press("e")
        assert not app.screen.query_one("#detail-open-pi", Button).display
        assert app.screen.query_one("#detail-investigate", Button).display == inside_herdr


async def test_badges_update_without_changing_cards_or_drafts(setup):
    store, task, details, agents, calls, investigator = setup
    app = Board(store, {"repos": []}, investigator=investigator)
    before = store.get(task)
    async with app.run_test(size=(150, 45)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert str(app.query_one(".agent-badge").render()) == "Pi · Working"
        for state, label in (
            ("done", "Done · unread"),
            ("idle", "Idle · viewed"),
            ("blocked", "Needs input"),
        ):
            agents[0]["agent_status"] = state
            app.poll_agent_states()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one(".agent-badge").render()) == f"Pi · {label}"
        await pilot.press("e")
        app.screen.query_one("#detail-title", Input).value = "Unsaved title"
        app.screen.query_one("#detail-description", TextArea).load_text("Unsaved description")
        agents[0]["agent_status"] = "working"
        app.poll_agent_states()
        await app.workers.wait_for_complete()
        assert app.screen.query_one("#detail-title", Input).value == "Unsaved title"
        assert app.screen.query_one("#detail-description", TextArea).text == "Unsaved description"
        await pilot.press("escape")
        agents.clear()
        app.poll_agent_states()
        await app.workers.wait_for_complete()
        await app.refresh_board()
        assert not app.query(".agent-badge")
        assert store.get(task) == before
