import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Select, TextArea

from sdk_support_hero.app import (
    Board,
    CardDetails,
    InvestigateCard,
    InvestigationOpenButton,
    format_update,
)
from sdk_support_hero.cli import main
from sdk_support_hero.investigate import (
    DEFAULT_INSTRUCTION,
    Investigator,
    LaunchError,
    herdr_command,
)
from sdk_support_hero.store import Store
from sdk_support_hero.sync import parse_source


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.session = None
        self.after_start = lambda: None
        self.session_mismatch = False
        self.busy_checks = 0

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        verb = tuple(args[:2])
        if verb == self.failure:
            raise LaunchError("fixture startup or submission failure")
        if verb == ("workspace", "list"):
            return {
                "workspaces": [
                    {"workspace_id": "w1", "label": "Support"},
                    {"workspace_id": "w2", "label": "SDK fixes"},
                ]
            }
        if verb == ("pane", "current"):
            return {"pane": {"workspace_id": "w1"}}
        if verb == ("pane", "process-info"):
            busy = self.busy_checks > 0
            self.busy_checks -= 1
            return {
                "process_info": {
                    "shell_pid": 10,
                    "foreground_process_group_id": 11 if busy else 10,
                    "foreground_processes": [{"pid": 11 if busy else 10}],
                }
            }
        if verb == ("pane", "get"):
            return {"pane": {"pane_id": "w2:p9", "revision": 1}}
        if verb == ("tab", "create"):
            return {"tab": {"tab_id": "w2:t9"}, "root_pane": {"pane_id": "w2:p9"}}
        if verb == ("agent", "start"):
            self.session = Path(args[args.index("--session") + 1])
            self.session.write_text(
                json.dumps(
                    {
                        "type": "session",
                        "version": 3,
                        "id": "fixture-session-id",
                        "cwd": str(self.session.parent),
                    }
                )
                + "\n"
            )
            self.after_start()
            return {"type": "agent_started"}
        if verb == ("agent", "get"):
            return {
                "agent": {
                    "agent": "pi",
                    "pane_id": "w2:p9",
                    "agent_session": {
                        "kind": "path",
                        "value": str(self.session) + ("-other" if self.session_mismatch else ""),
                    },
                }
            }
        if verb == ("agent", "focus"):
            return {"agent": {"tab_id": "w2:t9"}}
        if verb in (("agent", "prompt"), ("tab", "focus")):
            return {"type": "ok"}
        raise AssertionError(args)


@pytest.fixture
def launch_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/fixture/herdr.sock")
    monkeypatch.setattr("sdk_support_hero.investigate.shutil.which", lambda name: f"/bin/{name}")
    store = Store(tmp_path / "board with spaces.db")
    task = store.create("Investigate flags", description="Saved description")
    fake = FakeHerdr()
    launcher = Investigator(store, runner=fake, config_file=tmp_path / "custom config.json")
    return store, task, fake, launcher


def test_brief_is_progressive_and_existing_full_view_is_unchanged(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "board.db")
    task = store.create(
        "Card", description="Description", status="waiting", delegated_to="SDK team"
    )
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    store.observe(source, "Linked issue", {"state": "open"})
    store.add_note(task, "Historical investigation")
    full = store.get(task)
    assert full["updates"] and full["sources"][0]["facts"]
    monkeypatch.setattr(store, "history", lambda _: pytest.fail("Brief view loaded history"))
    brief = store.get(task, brief=True)
    assert brief["description"] == "Description" and brief["delegated_to"] == "SDK team"
    assert brief["sources"] == [
        {"key": source["key"], "kind": "issue", "url": source["url"], "title": "Linked issue"}
    ]
    assert "updates" not in brief
    assert main(["--db", str(store.path), "show", str(task), "--brief"]) == 0
    assert json.loads(capsys.readouterr().out) == brief
    with pytest.raises(ValueError):
        store.get(999, brief=True)


def test_launch_records_session_and_passes_custom_prompt_as_data(launch_setup):
    store, task, fake, launcher = launch_setup
    before = store.get(task)
    instruction = "--review this\nImplement the fix and run the tests. $(not-a-shell-command)"
    result = launcher.launch(task, "w2", instruction)
    assert result["session_id"] == "fixture-session-id" and result["stage"] == "started"
    create = next(c for c in fake.calls if c[:2] == ["tab", "create"])
    assert create[create.index("--workspace") + 1] == "w2" and "--no-focus" in create
    assert create[create.index("--label") + 1] == f"#{task}"
    start = next(c for c in fake.calls if c[:2] == ["agent", "start"])
    assert start[start.index("--pane") + 1] == "w2:p9"
    assert start[start.index("--kind") + 1] == "pi"
    submission = next(c for c in fake.calls if c[:2] == ["agent", "prompt"])
    prompt = submission[3]
    assert prompt.startswith(instruction + "\n\n")
    assert submission[submission.index("--until") + 1] == "working"
    brief_cmd = next(
        line.split(": ", 1)[1]
        for line in prompt.splitlines()
        if line.startswith("Summary and linked")
    )
    args = shlex.split(brief_cmd)
    assert args[args.index("--db") + 1] == str(store.path)
    assert args[args.index("--config") + 1] == str(launcher.config_file)
    assert args[-3:] == ["show", str(task), "--brief"]
    assert "history" in prompt and "Record your outcome" in prompt
    assert "Saved description" not in prompt
    assert Path(result["prompt_file"]).read_text() == prompt
    assert json.loads(Path(result["manifest"]).read_text())["stage"] == "started"
    for key in ("session_file", "manifest", "prompt_file"):
        assert Path(result[key]).stat().st_mode & 0o777 == 0o600
    assert Path(result["manifest"]).parent.stat().st_mode & 0o777 == 0o700
    after = store.get(task)
    assert {k: v for k, v in after.items() if k != "updates"} == {
        k: v for k, v in before.items() if k != "updates"
    }
    entries = [u for u in after["updates"] if u["kind"] == "investigation"]
    assert [u["details"]["stage"] for u in entries] == ["prepared", "started"]
    assert result["session_file"] in format_update(entries[-1])
    assert result["resume_command"] in format_update(entries[-1])
    assert launcher.prompt(task, " ").startswith(DEFAULT_INSTRUCTION)


@pytest.mark.parametrize("failure", [("tab", "create"), ("agent", "start"), ("agent", "prompt")])
def test_partial_launch_is_recorded_and_never_retried(launch_setup, failure):
    store, task, fake, launcher = launch_setup
    fake.failure = failure
    with pytest.raises(LaunchError, match="needs attention"):
        launcher.launch(task, "w2")
    assert sum(tuple(c[:2]) == failure for c in fake.calls) == 1
    event = store.history(task)[-1]
    assert event["details"]["stage"] == "needs_attention"
    manifest = json.loads(Path(event["details"]["manifest"]).read_text())
    assert manifest["stage"] == "needs_attention"
    if failure != ("tab", "create"):
        assert manifest["pane_id"] == "w2:p9"
    assert not any(c[:2] in (["tab", "close"], ["pane", "run"]) for c in fake.calls)


def test_unavailable_workspace_and_outside_herdr_have_no_launch_side_effects(
    launch_setup, monkeypatch
):
    store, task, fake, launcher = launch_setup
    with pytest.raises(LaunchError, match="no longer available"):
        launcher.launch(task, "gone")
    assert not any(c[0] == "tab" for c in fake.calls)
    assert not launcher.root.exists()
    fake.calls.clear()
    monkeypatch.setenv("HERDR_ENV", "0")
    with pytest.raises(LaunchError, match="inside Herdr"):
        launcher.launch(task, "w2")
    assert not fake.calls


def test_card_deleted_during_startup_prevents_prompt_and_keeps_manifest(launch_setup):
    store, task, fake, launcher = launch_setup
    fake.after_start = lambda: store.delete(task, expected_revision=store.get(task)["revision"])
    with pytest.raises(LaunchError, match="needs attention"):
        launcher.launch(task, "w2")
    assert not any(c[:2] == ["agent", "prompt"] for c in fake.calls)
    assert not store.tasks()
    manifest = next(launcher.root.glob("*/launch.json"))
    assert json.loads(manifest.read_text())["session_id"] == "fixture-session-id"


def test_wrong_session_is_never_prompted_or_focused(launch_setup):
    store, task, fake, launcher = launch_setup
    fake.session_mismatch = True
    with pytest.raises(LaunchError, match="different session"):
        launcher.launch(task, "w2")
    assert not any(c[:2] == ["agent", "prompt"] for c in fake.calls)
    fake.session_mismatch = False
    result = launcher.launch(task, "w2")
    launcher.focus(result)
    assert fake.calls[-2:] == [["agent", "focus", result["agent_name"]], ["tab", "focus", "w2:t9"]]
    fake.session_mismatch = True
    fake.calls.clear()
    with pytest.raises(LaunchError, match="no longer"):
        launcher.focus(result)
    assert not any(c[:2] == ["agent", "focus"] for c in fake.calls)


@pytest.mark.parametrize("value", ["[]", "not JSON", "{}"])
def test_bad_herdr_responses_are_errors(monkeypatch, value):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=value, stderr="")
    )
    with pytest.raises(LaunchError):
        herdr_command(["workspace", "list"])


def test_new_tab_waits_for_shell_before_starting_agent(launch_setup, monkeypatch):
    store, task, fake, launcher = launch_setup
    monkeypatch.setattr("sdk_support_hero.investigate.time.sleep", lambda _: None)
    fake.busy_checks = 2
    launcher.launch(task, "w2")
    checks = [i for i, c in enumerate(fake.calls) if c[:2] == ["pane", "process-info"]]
    start = next(i for i, c in enumerate(fake.calls) if c[:2] == ["agent", "start"])
    assert len(checks) == 5 and max(checks) < start


def test_herdr_timeout_is_ambiguous_not_retryable(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("herdr", 15)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(LaunchError, match="may have taken effect"):
        herdr_command(["agent", "prompt"])


async def test_dialog_launches_into_selected_workspace_and_preserves_draft(launch_setup):
    store, task, fake, launcher = launch_setup
    app = Board(store, {"repos": []}, investigator=launcher)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.press("e")
        app.screen.query_one("#detail-description", TextArea).load_text("Unsaved description")
        await pilot.click("#detail-investigate")
        assert isinstance(app.screen, InvestigateCard)
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one("#investigate-workspace", Select).value == "w1"
        app.screen.query_one("#investigate-workspace", Select).value = "w2"
        app.screen.query_one("#investigate-prompt", TextArea).load_text("Draft a response")
        await pilot.click("#investigate-launch")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(app.screen, CardDetails)
        assert app.screen.query_one("#detail-description", TextArea).text == "Unsaved description"
        assert store.get(task)["description"] == "Saved description"
        assert not app.screen.query_one("#detail-investigate", Button).disabled
        assert not app.investigating
        button = app.screen.query_one(InvestigationOpenButton)
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        await pilot.click(button)
        await app.workers.wait_for_complete()
        assert fake.calls[-1] == ["tab", "focus", "w2:t9"]


async def test_cancel_and_workspace_load_failure_do_not_launch(launch_setup):
    store, task, fake, launcher = launch_setup
    app = Board(store, {"repos": []}, investigator=launcher)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.press("e")
        await pilot.click("#detail-investigate")
        await app.workers.wait_for_complete()
        await pilot.click("#investigate-cancel")
        assert isinstance(app.screen, CardDetails)
        assert not any(c[0] == "tab" for c in fake.calls)
        fake.failure = ("workspace", "list")
        await pilot.click("#detail-investigate")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.screen.query_one("#investigate-launch", Button).disabled
        await pilot.press("escape")
        assert not any(u["kind"] == "investigation" for u in store.history(task))
