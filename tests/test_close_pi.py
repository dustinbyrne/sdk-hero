from copy import deepcopy

import pytest
from textual.widgets import Select, SelectionList

from sdk_support_hero.app import Board, ClosePiSessions
from sdk_support_hero.investigate import Investigator
from sdk_support_hero.store import Store
from sdk_support_hero.sync import validate_config


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/fixture/herdr.sock")
    monkeypatch.setattr("sdk_support_hero.investigate.shutil.which", lambda _: "/bin/herdr")
    store = Store(tmp_path / "board.db")
    task = store.create("Card", status="in_progress")
    agents, calls, paths = {}, [], []
    for i, state in enumerate(("idle", "working")):
        path = tmp_path / f"session-{i}.jsonl"
        path.write_text("saved conversation")
        paths.append(path)
        store.investigation_update(
            task,
            "Started",
            {
                "run_id": f"run-{i}",
                "session_file": str(path),
                "herdr_socket": "/fixture/herdr.sock",
                "stage": "started",
            },
        )
        agents[f"pane-{i}"] = {
            "agent": "pi",
            "name": f"session-{i}",
            "pane_id": f"pane-{i}",
            "agent_status": state,
            "agent_session": {"kind": "path", "value": str(path)},
        }

    def runner(args):
        calls.append(args)
        if args == ["agent", "list"]:
            return {"agents": deepcopy(list(agents.values()))}
        if args[:2] == ["agent", "get"]:
            return {"agent": deepcopy(agents[args[2]])}
        assert args[:2] == ["pane", "close"]
        del agents[args[2]]
        return {}

    return store, task, agents, calls, paths, Investigator(store, runner=runner)


@pytest.mark.parametrize(
    "state,closes",
    [
        ("idle", True),
        ("done", True),
        ("working", False),
        ("blocked", False),
        ("unknown", False),
        (None, False),
    ],
)
def test_automatic_close_rechecks_current_state_and_preserves_files(setup, state, closes):
    store, task, agents, calls, paths, investigator = setup
    store.update(task, status="done")
    session = investigator.live_sessions(task)[0]
    agents["pane-0"]["agent_status"] = state
    assert investigator.close_session(session, automatic=True) == closes
    assert (["pane", "close", "pane-0"] in calls) == closes
    assert "pane-1" in agents
    assert all(path.read_text() == "saved conversation" for path in paths)


@pytest.mark.parametrize("change", ["path", "agent", "pane", "reopened", "server"])
def test_close_rejects_stale_identity_or_reopened_card(setup, change, monkeypatch):
    store, task, agents, calls, _, investigator = setup
    store.update(task, status="done")
    session = investigator.live_sessions(task)[0]
    if change == "path":
        agents["pane-0"]["agent_session"]["value"] += "-replacement"
    elif change == "agent":
        agents["pane-0"]["agent"] = "other"
    elif change == "pane":
        agents["pane-0"]["pane_id"] = "other"
    elif change == "server":
        monkeypatch.setenv("HERDR_SOCKET_PATH", "/other/server")
    else:
        store.update(task, status="inbox")
    assert not investigator.close_session(session)
    assert not any(call[:2] == ["pane", "close"] for call in calls)


def test_resumed_runs_are_deduplicated_and_unrelated_agents_ignored(setup):
    store, task, agents, _, _, investigator = setup
    details = store.investigation_sessions()[0]
    store.investigation_update(task, "Resumed", {**details, "run_id": "resumed-run"})
    agents["unrelated"] = {
        **agents["pane-0"],
        "agent_session": {"kind": "path", "value": "/unrelated"},
    }
    assert len(investigator.live_sessions(task)) == 2


@pytest.mark.parametrize("choice", ["keep", "selected", "escape"])
async def test_manual_move_offers_sessions_and_defaults_to_keep_open(setup, choice):
    store, task, agents, calls, _, investigator = setup
    app = Board(store, {"repos": []}, investigator=investigator)
    async with app.run_test(size=(120, 45)) as pilot:
        await app.change_card(store.get(task), status="done")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ClosePiSessions)
        assert app.focused.id == "keep-pi"
        assert not any(call[:2] == ["pane", "close"] for call in calls)
        if choice == "selected":
            selector = app.screen.query_one(SelectionList)
            selector.deselect(0)  # Explicitly close only the working session.
            await pilot.click("#close-pi")
        elif choice == "escape":
            await pilot.press("escape")
        else:
            await pilot.click("#keep-pi")
        await app.workers.wait_for_complete()
        assert "pane-0" in agents
        assert ("pane-1" in agents) == (choice != "selected")
        assert store.get(task)["status"] == "done"


async def test_saving_done_in_details_also_prompts(setup):
    store, task, _, _, _, investigator = setup
    app = Board(store, {"repos": []}, investigator=investigator)
    async with app.run_test(size=(120, 45)) as pilot:
        app.open_card(task)
        await pilot.pause()
        details = app.screen
        details.query_one("#detail-status", Select).value = "done"
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, ClosePiSessions)
        await pilot.press("escape")
        assert app.screen is details


@pytest.mark.parametrize("enabled", [None, True, False])
async def test_refresh_only_closes_idle_sessions_at_the_completion_transition(setup, enabled):
    store, task, agents, calls, _, investigator = setup

    class Sync:
        completed_card_ids = []

        def sync(self, **kwargs):
            self.completed_card_ids = []
            if store.get(task)["status"] != "done":
                store.update(task, status="done")
                self.completed_card_ids = [task]
            return {}

    config = {"repos": []}
    if enabled is not None:
        config["auto_close_pi_on_done"] = enabled
    app = Board(store, config, investigator=investigator)
    app.syncer = Sync()
    async with app.run_test(size=(120, 45)) as pilot:
        app.sync_worker()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert not isinstance(app.screen, ClosePiSessions)
        assert ("pane-0" in agents) == (enabled is False)
        assert "pane-1" in agents
        agents["pane-1"]["agent_status"] = "done"
        app.sync_worker()
        await app.workers.wait_for_complete()
        assert "pane-1" in agents


@pytest.mark.parametrize("value", [None, "false", 0, 1, []])
def test_auto_close_config_requires_boolean(value):
    with pytest.raises(ValueError, match="auto_close_pi_on_done"):
        validate_config({"repos": [], "auto_close_pi_on_done": value})
