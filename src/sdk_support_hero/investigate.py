"""Launch a persistent interactive Pi session in an explicitly selected Herdr workspace."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_INSTRUCTION = "Investigate this card and recommend the next step."


class LaunchError(RuntimeError):
    pass


def herdr_command(args, *, timeout=15):
    try:
        process = subprocess.run(
            ["herdr", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise LaunchError(
            "Herdr timed out; the operation may have taken effect. "
            "Inspect the recorded tab before retrying."
        ) from error
    except OSError as error:
        raise LaunchError(f"Cannot run Herdr: {error}") from error
    try:
        response = json.loads(process.stdout if process.returncode == 0 else process.stderr)
    except (ValueError, TypeError) as error:
        detail = process.stderr.strip()[:1000]
        raise LaunchError(
            "Herdr returned an invalid response; inspect the recorded tab before retrying."
            + (f" {detail}" if detail else "")
        ) from error
    if not isinstance(response, dict):
        raise LaunchError("Herdr returned an invalid response object")
    if process.returncode or response.get("error"):
        detail = response.get("error", {})
        raise LaunchError(
            f"{detail.get('code', 'herdr_error')}: {detail.get('message', 'Herdr command failed')}"
        )
    if not isinstance(response.get("result"), dict):
        raise LaunchError("Herdr response is missing its result")
    return response["result"]


class Investigator:
    def __init__(self, store, *, config_file=None, runner=herdr_command, root=None):
        self.store = store
        self.config_file = Path(config_file).expanduser().absolute() if config_file else None
        self.runner = runner
        self.root = Path(root) if root else store.path.absolute().parent / "investigations"

    @property
    def available(self):
        return os.environ.get("HERDR_ENV") == "1" and bool(shutil.which("herdr"))

    def require_herdr(self):
        if not self.available:
            raise LaunchError("Open sdk-hero inside Herdr to launch or open an investigation")

    def workspaces(self):
        self.require_herdr()
        result = self.runner(["workspace", "list"])
        rows = result["workspaces"]
        if not isinstance(rows, list) or not all(
            isinstance(w, dict)
            and isinstance(w.get("workspace_id"), str)
            and isinstance(w.get("label"), str)
            for w in rows
        ):
            raise LaunchError("Unexpected Herdr workspace list")
        # A pane may have moved since its environment was created. Resolve live caller context.
        current = self.runner(["pane", "current", "--current"])["pane"]["workspace_id"]
        return rows, current

    def wait_for_shell(self, pane_id, timeout=10):
        deadline = time.monotonic() + timeout
        # Shell startup briefly returns to its own process group between init commands.
        # Require output and stable samples; Herdr still validates the final agent start.
        ready_samples = 0
        while (remaining := deadline - time.monotonic()) > 0:
            info = self.runner(
                ["pane", "process-info", "--pane", pane_id], timeout=min(5, remaining)
            )["process_info"]
            pane = self.runner(["pane", "get", pane_id], timeout=min(5, remaining))["pane"]
            shell = info.get("shell_pid")
            foreground = info.get("foreground_processes") or []
            if (
                shell
                and info.get("foreground_process_group_id") == shell
                and foreground
                and all(process.get("pid") == shell for process in foreground)
                and pane.get("revision", 0) > 0
                and not pane.get("agent")
            ):
                ready_samples += 1
                if ready_samples >= 3:
                    return
            else:
                ready_samples = 0
            time.sleep(min(0.1, remaining))
        raise LaunchError(
            "The new tab did not become an available shell; inspect it before retrying"
        )

    def card_command(self):
        args = [
            sys.executable,
            "-m",
            "sdk_support_hero.cli",
            "--db",
            str(self.store.path.absolute()),
        ]
        if self.config_file:
            args.extend(["--config", str(self.config_file)])
        return args

    def prompt(self, task_id, instruction):
        command = self.card_command()
        task = instruction if instruction.strip() else DEFAULT_INSTRUCTION
        return (
            f"{task}\n\nSDK Hero card #{task_id} (saved state).\n"
            "Summary and linked references: "
            f"{shlex.join([*command, 'show', str(task_id), '--brief'])}\n"
            f"Updates, if useful: {shlex.join([*command, 'history', str(task_id)])}\n"
            f"Full card and source facts: {shlex.join([*command, 'show', str(task_id)])}\n"
            "Record your outcome in Updates: "
            f"{shlex.join([*command, 'note', str(task_id)])} '<summary>'"
        )

    def launch(self, task_id, workspace_id, instruction=""):
        self.require_herdr()
        task = self.store.get(task_id, brief=True)
        workspaces, _ = self.workspaces()
        workspace = next((w for w in workspaces if w["workspace_id"] == workspace_id), None)
        if workspace is None:
            raise LaunchError("That Herdr workspace is no longer available; choose another")
        run_id = uuid.uuid4().hex
        directory = self.root / run_id
        directory.mkdir(parents=True, mode=0o700)
        session_file = directory / "session.jsonl"
        session_file.touch(mode=0o600, exist_ok=False)
        prompt_file = directory / "prompt.txt"
        prompt = self.prompt(task_id, instruction)
        with prompt_file.open("x", encoding="utf-8") as stream:
            os.chmod(prompt_file, 0o600)
            stream.write(prompt)
        manifest = directory / "launch.json"
        details = {
            "run_id": run_id,
            "card_id": task_id,
            "card_revision": task["revision"],
            "workspace_id": workspace_id,
            "workspace_label": workspace["label"],
            "agent_name": f"hero-{run_id[:16]}",
            "session_file": str(session_file),
            "manifest": str(manifest),
            "prompt_file": str(prompt_file),
            "herdr_socket": os.environ.get("HERDR_SOCKET_PATH", ""),
            "resume_command": shlex.join(["pi", "--session", str(session_file)]),
        }

        def checkpoint(stage):
            details["stage"] = stage
            temporary = directory / "launch.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(details, stream, indent=2)
            temporary.replace(manifest)

        checkpoint("prepared")
        # If the card disappeared, stop before creating any terminal layout.
        self.store.investigation_update(task_id, "Pi investigation launch requested", details)
        try:
            checkpoint("creating_tab")
            created = self.runner(
                [
                    "tab",
                    "create",
                    "--workspace",
                    workspace_id,
                    "--label",
                    f"#{task_id}",
                    "--no-focus",
                ]
            )
            details["tab_id"] = created["tab"]["tab_id"]
            details["pane_id"] = created["root_pane"]["pane_id"]
            checkpoint("waiting_for_shell")
            self.wait_for_shell(details["pane_id"])
            checkpoint("starting_pi")
            self.runner(
                [
                    "agent",
                    "start",
                    details["agent_name"],
                    "--kind",
                    "pi",
                    "--pane",
                    details["pane_id"],
                    "--timeout",
                    "30000",
                    "--",
                    "--session",
                    str(session_file),
                    "--name",
                    f"SDK Hero #{task_id}",
                ],
                timeout=40,
            )
            agent = self.runner(["agent", "get", details["agent_name"]])["agent"]
            session = agent.get("agent_session") or {}
            if agent["pane_id"] != details["pane_id"] or agent.get("agent") != "pi":
                raise LaunchError("The new pane does not contain the expected Pi agent")
            if (
                session.get("kind") == "path"
                and Path(session["value"]).resolve() != session_file.resolve()
            ):
                raise LaunchError("Pi reported a different session; no prompt was submitted")
            with session_file.open(encoding="utf-8") as stream:
                header = json.loads(stream.readline(65536))
            if header.get("type") != "session" or not header.get("id"):
                raise LaunchError("Pi did not initialize the expected persistent session")
            details["session_id"] = header["id"]
            details["cwd"] = header.get("cwd")
            self.store.get(task_id, brief=True)
            checkpoint("submitting_prompt")
            self.runner(
                [
                    "agent",
                    "prompt",
                    details["agent_name"],
                    prompt,
                    "--wait",
                    "--until",
                    "working",
                    "--timeout",
                    "10000",
                ],
                timeout=20,
            )
            checkpoint("started")
            self.store.investigation_update(task_id, "Pi investigation started", details)
            return details
        except Exception as error:
            failed_stage = details["stage"]
            details["error"] = str(error)
            details["failed_stage"] = failed_stage
            checkpoint("needs_attention")
            try:
                self.store.investigation_update(
                    task_id, "Pi investigation launch needs attention", details
                )
            except ValueError:
                pass  # The manifest remains available if the card was deleted during startup.
            raise LaunchError(
                f"Launch needs attention at {failed_stage}: {error}\nDetails: {manifest}"
            ) from error

    def card_states(self):
        """Read live Pi states in one query; never acknowledge or focus a session."""
        if not self.available:
            return {}
        sessions = self.store.investigation_sessions()
        paths = {}
        for details in sessions:
            if details.get("herdr_socket", "") == os.environ.get("HERDR_SOCKET_PATH", ""):
                path = str(Path(details["session_file"]).resolve())
                paths.setdefault(path, set()).add(details["card_id"])
        if not paths:
            return {}
        try:
            agents = self.runner(["agent", "list"])["agents"]
            states = {}
            for agent in agents:
                session = agent.get("agent_session") or {}
                if agent.get("agent") != "pi" or session.get("kind") != "path":
                    continue
                path = str(Path(session["value"]).resolve())
                state = agent.get("agent_status", "unknown")
                if state not in {"working", "done", "idle", "blocked", "unknown"}:
                    state = "unknown"
                for task_id in paths.get(path, ()):
                    states.setdefault(task_id, []).append(state)
            return {task_id: sorted(values) for task_id, values in states.items()}
        except Exception:
            # Do not leave a stale Working or Unread badge after a failed observation.
            return {task_id: ["unavailable"] for ids in paths.values() for task_id in ids}

    def focus_card(self, task_id):
        """Jump to the most recently launched session that is still live."""
        self.require_herdr()
        sessions = [
            details
            for details in self.store.investigation_sessions()
            if details["card_id"] == task_id
            and details.get("herdr_socket", "") == os.environ.get("HERDR_SOCKET_PATH", "")
        ]
        agents = self.runner(["agent", "list"])["agents"]
        for details in reversed(sessions):
            for agent in agents:
                session = agent.get("agent_session") or {}
                if (
                    agent.get("agent") == "pi"
                    and session.get("kind") == "path"
                    and Path(session["value"]).resolve() == Path(details["session_file"]).resolve()
                ):
                    self.focus({**details, "agent_name": agent.get("name") or agent["pane_id"]})
                    return
        raise LaunchError("This card no longer has a live Pi session in Herdr")

    def focus(self, details):
        self.require_herdr()
        if details.get("herdr_socket", "") != os.environ.get("HERDR_SOCKET_PATH", ""):
            raise LaunchError(
                "This investigation belongs to another Herdr session; use its resume command"
            )
        agent = self.runner(["agent", "get", details["agent_name"]])["agent"]
        session = agent.get("agent_session") or {}
        if (
            session.get("kind") != "path"
            or Path(session["value"]).resolve() != Path(details["session_file"]).resolve()
        ):
            raise LaunchError(
                "The recorded Pi session is no longer in that pane; use its resume command"
            )
        focused = self.runner(["agent", "focus", details["agent_name"]])["agent"]
        # Herdr 0.9.0 agent focus selects the pane on the server, but only explicit
        # tab/workspace navigation propagates that selection to attached clients.
        self.runner(["tab", "focus", focused["tab_id"]])
