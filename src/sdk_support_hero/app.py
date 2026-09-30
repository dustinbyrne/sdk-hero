from __future__ import annotations

import json

from textual import events, on, work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    Label,
    Link,
    ListItem,
    ListView,
    Select,
    Static,
    TextArea,
)

from .investigate import DEFAULT_INSTRUCTION, Investigator
from .sla import card_schedule, work_key
from .store import FIELDS, KINDS, STATUSES, Conflict
from .sync import Syncer, support_scope

TITLES = ("Inbox", "Ready", "In progress", "Waiting", "Done")
SOURCE_TITLES = {
    "ticket": "Support ticket",
    "issue": "Issue",
    "pr": "Pull request",
    "run": "Workflow run",
}


AGENT_LABELS = {
    "working": "Working",
    "done": "Done · unread",
    "idle": "Idle · viewed",
    "blocked": "Needs input",
    "unknown": "Status unknown",
    "unavailable": "Status unavailable",
}


def matches_search(task, query, source_labels=""):
    query = query.strip().lower()
    if query.startswith("#") and query[1:].isdecimal():
        return task["id"] == int(query[1:])
    labels = [source_labels, task.get("sla", {}).get("badge", "")]
    if task.get("needs_first_touch"):
        labels.append("Needs first touch")
    labels.extend(f"Pi · {AGENT_LABELS[state]}" for state in task.get("agent_states", []))
    text = " ".join([*(str(task[k]) for k in FIELDS), *labels]).lower()
    return query in text or query in text.replace(" · ", " ")


def needs_first_touch(task, sources):
    if task["status"] == "done":
        return False
    return any(
        source["kind"] == "issue"
        and not source.get("error")
        and str(source["facts"].get("state", "")).lower() == "open"
        and "last_team_reply_at" in source["facts"]
        and source["facts"]["last_team_reply_at"] is None
        and source["facts"].get("needs_team_reply") is True
        for source in sources
    )


def format_update(entry):
    text = f"{entry['created_at']} · {entry['actor']}\n{entry['summary']}"
    details = entry["details"]
    if entry["kind"] == "source_changed" and details.get("display") == "summary":
        return text + ("\n" + details["url"] if details.get("url") else "")
    changes = details if entry["kind"] == "edited" else details.get("changes", {})
    for field, change in changes.items():
        before, after = change["before"], change["after"]
        if isinstance(before, (dict, list)) or isinstance(after, (dict, list)):
            before, after = (
                json.dumps(before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
            )
        text += (
            f"\n{field}: {before if before is not None else '—'}"
            f" → {after if after is not None else '—'}"
        )
    if details.get("url"):
        text += "\n" + details["url"]
    if details.get("error"):
        text += "\n" + details["error"]
    if entry["kind"] == "investigation":
        for label, key in (
            ("Workspace", "workspace_label"),
            ("Stage", "stage"),
            ("Tab", "tab_id"),
            ("Agent", "agent_name"),
            ("Pi session", "session_id"),
            ("Session file", "session_file"),
            ("Resume", "resume_command"),
            ("Launch details", "manifest"),
        ):
            if details.get(key):
                text += f"\n{label}: {details[key]}"
    return text


class AddCard(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AddCard, PriorityPicker { align: center middle; background: $background 75%; }
    .small-dialog { width: 60; max-width: 95%; height: auto; padding: 1 2;
        border: round $accent; }
    .small-dialog Horizontal { height: 3; }
    """

    def __init__(self, status):
        super().__init__()
        self.status = status

    def compose(self):
        with Vertical(classes="small-dialog"):
            yield Label(f"Add card to {TITLES[STATUSES.index(self.status)]}")
            yield Input(placeholder="What needs to be done?", id="new-title")
            with Horizontal():
                yield Button("Add card", variant="primary", id="add-save")
                yield Button("Cancel", id="add-cancel")

    @on(Input.Submitted)
    @on(Button.Pressed, "#add-save")
    def save(self):
        title = self.query_one(Input).value.strip()
        if title:
            self.dismiss(title)
        else:
            self.notify("Give the card a title", severity="warning")

    @on(Button.Pressed, "#add-cancel")
    def action_cancel(self):
        self.dismiss(None)


class PriorityPicker(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PriorityPicker { align: center middle; background: $background 75%; }
    #priorities { width: 48; max-width: 95%; height: auto; padding: 1;
        border: round $accent; }
    #priorities Button { width: 100%; }
    """

    def compose(self):
        with Vertical(id="priorities"):
            yield Label("Priority")
            for i, label in enumerate(("Urgent", "High", "Normal", "Low")):
                yield Button(f"P{i} · {label}", id=f"choose-p{i}")

    @on(Button.Pressed)
    def choose(self, event):
        self.dismiss(int(event.button.id[-1]))

    def action_cancel(self):
        self.dismiss(None)


class DeleteCard(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    DeleteCard { align: center middle; background: $background 75%; }
    #delete-dialog { width: 60; max-width: 95%; height: auto;
        padding: 1 2; border: round $error; }
    #delete-dialog Static { height: auto; margin-bottom: 1; }
    #delete-dialog Horizontal { height: 3; }
    """

    def __init__(self, title):
        super().__init__()
        self.card_title = title

    def compose(self):
        with Vertical(id="delete-dialog"):
            yield Static(f'Delete "{self.card_title}"?', markup=False)
            yield Static(
                "Permanently removes this local card and its history. Linked sources "
                "will not be re-imported by refresh. Nothing changes externally.",
                markup=False,
            )
            with Horizontal():
                yield Button("Cancel", id="delete-cancel")
                yield Button("Delete card", variant="error", id="delete-confirm")

    def on_mount(self):
        self.query_one("#delete-cancel", Button).focus()

    @on(Button.Pressed, "#delete-cancel")
    def action_cancel(self):
        self.dismiss(False)

    @on(Button.Pressed, "#delete-confirm")
    def confirm(self):
        self.dismiss(True)


class InvestigateCard(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    InvestigateCard { align: center middle; background: $background 75%; }
    #investigate-dialog { width: 80; max-width: 95%; height: auto; max-height: 95%;
        padding: 1 2; border: round $accent; }
    #investigate-dialog Label { margin-top: 1; }
    #investigate-dialog Horizontal { height: 3; }
    #investigate-prompt { height: 8; }
    #investigate-error { height: auto; color: $warning; }
    """

    def __init__(self, investigator):
        super().__init__()
        self.investigator = investigator

    def compose(self):
        with Vertical(id="investigate-dialog"):
            yield Label("Launch Pi in Herdr")
            yield Static("Uses the saved card. Other unsaved edits stay in this dialog.")
            yield Label("Workspace")
            yield Select(
                [], prompt="Loading workspaces…", disabled=True, id="investigate-workspace"
            )
            yield Label("Optional prompt")
            yield TextArea("", placeholder=DEFAULT_INSTRUCTION, id="investigate-prompt")
            yield Static("", id="investigate-error", markup=False)
            with Horizontal():
                yield Button("Launch Pi", variant="primary", id="investigate-launch", disabled=True)
                yield Button("Cancel", id="investigate-cancel")

    def on_mount(self):
        self.load_workspaces()

    @work(thread=True)
    def load_workspaces(self):
        try:
            workspaces, current = self.investigator.workspaces()
            error = "" if workspaces else "No Herdr workspaces are available"
        except Exception as exc:
            workspaces, current, error = [], None, str(exc)
        if self.is_mounted and self.app.is_running:
            self.app.call_from_thread(self.loaded, workspaces, current, error)

    def loaded(self, workspaces, current, error):
        if not self.is_mounted:
            return
        self.query_one("#investigate-error", Static).update(error)
        if error:
            return
        selector = self.query_one("#investigate-workspace", Select)
        selector.set_options([(w["label"], w["workspace_id"]) for w in workspaces])
        ids = {w["workspace_id"] for w in workspaces}
        selector.value = current if current in ids else workspaces[0]["workspace_id"]
        selector.disabled = False
        self.query_one("#investigate-launch", Button).disabled = False

    @on(Button.Pressed, "#investigate-launch")
    def launch(self):
        workspace = self.query_one("#investigate-workspace", Select).value
        if workspace is not Select.BLANK:
            self.dismiss((workspace, self.query_one("#investigate-prompt", TextArea).text))

    @on(Button.Pressed, "#investigate-cancel")
    def action_cancel(self):
        self.dismiss(None)


class InvestigationOpenButton(Button):
    def __init__(self, details):
        super().__init__("Open Pi session", classes="investigation-open")
        self.details = details


class SourceSLAButton(Button):
    def __init__(self, source):
        super().__init__(
            "Reopen SLA" if source.get("sla_resolved") else "Resolve SLA",
            classes="source-sla-toggle",
        )
        self.source = source


class CardDetails(ModalScreen):
    BINDINGS = [("escape", "close", "Close"), ("ctrl+s", "save", "Save")]
    DEFAULT_CSS = """
    CardDetails { align: center middle; background: $background 75%; }
    #card-detail { width: 90; max-width: 95%; height: 94%; border: round $accent; padding: 1 2; }
    #detail-actions { height: 3; }
    #detail-actions Button { min-width: 10; margin-right: 1; }
    #detail-title { width: 100%; }
    #detail-open-pi, #detail-investigate { display: none; }
    #detail-scroll { height: 1fr; }
    #detail-description { height: 9; min-height: 5; border: round $panel; }
    #detail-options { height: 3; }
    #detail-options Select { width: 1fr; }
    #delegation-row { height: 3; }
    #detail-delegated-to { width: 1fr; }
    #take-back { min-width: 12; }
    #detail-scroll Label { margin-top: 1; text-style: bold; }
    #detail-sources, #detail-updates { height: auto; }
    #detail-updates Static { height: auto; padding: 1; border-bottom: solid $panel; }
    #detail-sources Static { height: auto; color: $text-muted; }
    #detail-sources Link { height: auto; }
    #detail-sources .source-sla-row { height: auto; min-height: 1; }
    #detail-sources .source-sla { width: 1fr; }
    #detail-sources .source-sla-toggle {
        height: 1; border: none; width: 14; min-width: 14; padding: 0;
    }
    #detail-sources .source-sla.overdue { color: $error; }
    #detail-sources .source-sla.soon { color: $warning; }
    #detail-sources .source-sla.normal { color: $success; }
    #post-row { height: 3; }
    #post-row Input { width: 1fr; }
    #detail-warning { height: auto; color: $warning; }
    """

    def __init__(self, store, task_id):
        super().__init__()
        self.store = store
        self.task_id = task_id
        self.snapshot = store.get(task_id)
        self.history_fingerprint = None
        self.sla_options_fingerprint = None

    @staticmethod
    def sla_source_options(sources, selected):
        options = [("Automatic · support first", "")]
        options.extend(
            (f"{SOURCE_TITLES.get(s['kind'], s['kind'])} · {s['url']}", s["key"]) for s in sources
        )
        if selected and selected not in {value for _, value in options}:
            options.append((f"No longer linked · {selected}", selected))
        return options

    def compose(self):
        task = self.snapshot
        with Vertical(id="card-detail") as detail:
            detail.border_title = f"Card #{self.task_id}"
            yield Input(task["title"], id="detail-title")
            with Horizontal(id="detail-actions"):
                yield Button("Save", variant="primary", id="detail-save")
                yield Button(
                    "Refresh", id="detail-refresh", tooltip="Refresh this card's linked sources"
                )
                yield Button(
                    "Investigate",
                    id="detail-investigate",
                    disabled=not self.app.investigator.available,
                )
                yield Button("Open Pi", id="detail-open-pi", disabled=True)
                yield Button("Close", id="detail-close")
                yield Button("Delete", variant="error", id="detail-delete")
            yield Static("", id="detail-warning", markup=False)
            with VerticalScroll(id="detail-scroll"):
                with Horizontal(id="detail-options"):
                    yield Select(
                        list(zip(TITLES, STATUSES)),
                        value=task["status"],
                        allow_blank=False,
                        id="detail-status",
                    )
                    yield Select(
                        [(f"P{i}", i) for i in range(4)],
                        value=task["priority"],
                        allow_blank=False,
                        id="detail-priority",
                    )
                yield Label("Delegated to · Waiting only")
                with Horizontal(id="delegation-row"):
                    yield Input(
                        task["delegated_to"],
                        placeholder="Person or team doing the work",
                        disabled=task["status"] != "waiting",
                        id="detail-delegated-to",
                    )
                    yield Button("Take back", id="take-back", disabled=not task["delegated_to"])
                yield Label("SLA source")
                yield Select(
                    self.sla_source_options(task["sources"], task["sla_source"]),
                    value=task["sla_source"],
                    allow_blank=False,
                    id="detail-sla-source",
                )
                yield Label("Description")
                yield TextArea(
                    task["description"],
                    id="detail-description",
                    placeholder="What needs to happen now?",
                    soft_wrap=True,
                )
                with Collapsible(title="Labels", collapsed=True):
                    yield Input(task["sdk"], placeholder="SDK", id="detail-sdk")
                    yield Select(
                        [(kind.replace("_", " "), kind) for kind in KINDS],
                        value=task["kind"],
                        allow_blank=False,
                        id="detail-kind",
                    )
                yield Label("Linked sources")
                yield Vertical(id="detail-sources")
                yield Label("Updates")
                with Horizontal(id="post-row"):
                    yield Input(placeholder="Add an update…", id="new-update")
                    yield Button("Post", id="post-update")
                yield Vertical(id="detail-updates")

    async def on_mount(self):
        await self.refresh_evidence()
        self.set_interval(2, self.refresh_evidence)

    def form_values(self):
        values = {
            key: self.query_one(f"#detail-{key.replace('_', '-')}", Input).value
            for key in ("title", "sdk", "delegated_to")
        }
        values["description"] = self.query_one("#detail-description", TextArea).text
        values.update(
            {
                key: self.query_one(f"#detail-{key.replace('_', '-')}", Select).value
                for key in ("status", "priority", "kind", "sla_source")
            }
        )
        return values

    def adopt_task(self, task):
        selector = self.query_one("#detail-sla-source", Select)
        options = self.sla_source_options(task["sources"], task["sla_source"])
        if options != self.sla_options_fingerprint:
            selector.set_options(options)
            self.sla_options_fingerprint = options
        current = self.form_values()
        self.snapshot = task
        for key, value in current.items():
            if value == task[key]:
                continue
            widget = self.query_one(f"#detail-{key.replace('_', '-')}")
            if key == "description":
                widget.load_text(task[key])
            else:
                widget.value = task[key]
        self.query_one("#detail-delegated-to", Input).disabled = task["status"] != "waiting"
        self.query_one("#detail-warning", Static).update("")

    async def refresh_evidence(self):
        try:
            task = self.store.get(self.task_id)
        except ValueError:
            self.query_one("#detail-warning", Static).update(
                "Card deleted elsewhere. Close to return to the board."
            )
            for selector in (
                "#detail-save",
                "#detail-refresh",
                "#detail-delete",
                "#post-update",
                "#detail-investigate",
                "#detail-open-pi",
            ):
                self.query_one(selector, Button).disabled = True
            self.query_one("#take-back", Button).disabled = True
            for button in self.query(SourceSLAButton):
                button.disabled = True
            return
        open_pi = self.query_one("#detail-open-pi", Button)
        open_pi.display = self.app.investigator.available and any(
            state != "unavailable" for state in self.app.agent_states.get(self.task_id, [])
        )
        open_pi.disabled = not open_pi.display
        self.query_one("#detail-investigate", Button).display = self.app.investigator.available
        self.query_one("#detail-refresh", Button).disabled = self.app.syncing or not task["sources"]
        self.query_one("#detail-investigate", Button).disabled = (
            not self.app.investigator.available or self.task_id in self.app.investigating
        )
        if task["revision"] != self.snapshot["revision"]:
            if all(value == self.snapshot[key] for key, value in self.form_values().items()):
                self.adopt_task(task)
            else:
                self.query_one("#detail-warning", Static).update(
                    "Card changed elsewhere. Draft preserved; close and reopen before saving."
                )
        self.query_one("#take-back", Button).disabled = not (
            task["status"] == "waiting"
            and task["delegated_to"]
            and task["revision"] == self.snapshot["revision"]
        )
        selector = self.query_one("#detail-sla-source", Select)
        selected = selector.value
        options = self.sla_source_options(task["sources"], selected)
        if options != self.sla_options_fingerprint:
            selector.set_options(options)
            selector.value = selected
            self.sla_options_fingerprint = options
        fingerprint = json.dumps([task["sources"], task["updates"]])
        if fingerprint == self.history_fingerprint:
            self.refresh_source_slas(task["sources"])
            return
        self.history_fingerprint = fingerprint
        sources = self.query_one("#detail-sources", Vertical)
        await sources.remove_children()
        if not task["sources"]:
            await sources.mount(Static("No linked sources", markup=False))
        for source in task["sources"]:
            source_type = SOURCE_TITLES.get(source["kind"], source["kind"])
            await sources.mount(
                Link(f"{source_type} · {source['title'] or source['url']}", url=source["url"])
            )
            state = source["facts"].get("state") or source["facts"].get("status") or "Not checked"
            await sources.mount(
                Static(
                    f"{state} · checked {source['observed_at'] or 'never'}"
                    + (f" · {source['error']}" if source["error"] else ""),
                    markup=False,
                )
            )
            await sources.mount(
                Horizontal(
                    Static("", classes="source-sla", markup=False),
                    SourceSLAButton(source),
                    classes="source-sla-row",
                )
            )
        self.refresh_source_slas(task["sources"])
        history = self.query_one("#detail-updates", Vertical)
        await history.remove_children()
        for entry in reversed(task["updates"]):
            await history.mount(Static(format_update(entry), markup=False))
            if entry["kind"] == "investigation" and entry["details"].get("stage") == "started":
                await history.mount(InvestigationOpenButton(entry["details"]))

    def refresh_source_slas(self, sources):
        for label, button, source in zip(
            self.query(".source-sla"), self.query(SourceSLAButton), sources
        ):
            schedule = card_schedule({"kind": "other", "status": "ready"}, [source])
            resolved = bool(source.get("sla_resolved"))
            text = (
                "Resolved until source update" if resolved else schedule["badge"] or "No active SLA"
            )
            label.update(f"SLA: {text}")
            label.tooltip = schedule["tooltip"] or None
            button.disabled = not resolved and not schedule["badge"]
            for level in ("overdue", "soon", "normal"):
                label.set_class(bool(schedule["badge"]) and schedule["level"] == level, level)

    @on(Button.Pressed, ".source-sla-toggle")
    async def toggle_source_sla(self, event):
        event.stop()
        source = event.button.source
        try:
            self.store.set_sla_resolved(self.task_id, source, not bool(source.get("sla_resolved")))
        except ValueError as error:
            self.notify(str(error), severity="warning")
        await self.refresh_evidence()

    @on(Button.Pressed, "#detail-open-pi")
    def open_live_pi(self):
        self.app.focus_card_pi(self.task_id)

    @on(Button.Pressed, "#detail-investigate")
    def investigate(self):
        def selected(result):
            if result:
                self.app.start_investigation(self.task_id, *result)

        self.app.push_screen(InvestigateCard(self.app.investigator), selected)

    @on(Button.Pressed, ".investigation-open")
    def open_investigation(self, event):
        event.stop()
        self.app.focus_investigation(event.button.details)

    @on(Button.Pressed, "#detail-refresh")
    def refresh_card(self):
        if self.app.start_sync(self.task_id):
            self.query_one("#detail-refresh", Button).disabled = True

    @on(Button.Pressed, "#detail-delete")
    def delete_card(self):
        def confirmed(result):
            if not result:
                return
            try:
                self.store.delete(self.task_id, expected_revision=self.snapshot["revision"])
            except ValueError as error:
                self.query_one("#detail-warning", Static).update(str(error))
                return
            self.dismiss(None)

        self.app.push_screen(DeleteCard(self.snapshot["title"]), confirmed)

    @on(Select.Changed, "#detail-status")
    def delegation_status_changed(self, event):
        self.query_one("#detail-delegated-to", Input).disabled = event.value != "waiting"

    @on(Button.Pressed, "#take-back")
    def take_back(self):
        patch = {"status": "inbox", "delegated_to": ""}
        try:
            self.store.update(
                self.task_id, expected_revision=self.snapshot["revision"], actor="you", **patch
            )
        except ValueError as error:
            self.query_one("#detail-warning", Static).update(str(error))
            return
        changed = any(self.snapshot[key] != value for key, value in patch.items())
        self.snapshot = {**self.snapshot, **patch, "revision": self.snapshot["revision"] + changed}
        self.query_one("#detail-status", Select).value = "inbox"
        self.query_one("#detail-delegated-to", Input).value = ""
        self.query_one("#detail-warning", Static).update(
            "Taken back to Inbox; other draft edits preserved"
        )
        self.call_after_refresh(self.refresh_evidence)

    @on(Button.Pressed, "#detail-save")
    def action_save(self):
        values = self.form_values()
        values["delegated_to"] = (
            values["delegated_to"].strip() if values["status"] == "waiting" else ""
        )
        patch = {key: value for key, value in values.items() if value != self.snapshot[key]}
        try:
            self.store.update(
                self.task_id, expected_revision=self.snapshot["revision"], actor="you", **patch
            )
        except ValueError as error:
            self.query_one("#detail-warning", Static).update(str(error))
            return
        self.snapshot = {
            **self.snapshot,
            **patch,
            "revision": self.snapshot["revision"] + bool(patch),
        }
        self.query_one("#detail-delegated-to", Input).value = self.snapshot["delegated_to"]
        self.query_one("#detail-warning", Static).update("Saved")
        self.call_after_refresh(self.refresh_evidence)

    @on(Button.Pressed, "#post-update")
    @on(Input.Submitted, "#new-update")
    async def post_update(self):
        entry = self.query_one("#new-update", Input)
        if entry.value.strip():
            try:
                self.store.add_note(self.task_id, entry.value, actor="you")
            except ValueError as error:
                self.query_one("#detail-warning", Static).update(str(error))
                return
            entry.value = ""
            await self.refresh_evidence()

    @on(Button.Pressed, "#detail-close")
    def action_close(self):
        self.dismiss(self.task_id)


class BoardList(ListView):
    def focus_on_click(self):
        # Column background clicks must retain the source selection until handled.
        return False

    BINDINGS = [
        Binding("left", "app.column(-1)", show=False),
        Binding("right", "app.column(1)", show=False),
    ]


class Column(Vertical):
    class MoveSelected(Message):
        def __init__(self, status):
            super().__init__()
            self.status = status

    def on_click(self, event: events.Click):
        if event.button == 1 and (
            event.widget is self
            or isinstance(event.widget, BoardList)
            or event.widget.id == f"heading-{self.status}"
        ):
            event.prevent_default()
            event.stop()
            self.post_message(self.MoveSelected(self.status))

    def __init__(self, status, title):
        super().__init__(classes="column", id=f"column-{status}")
        self.status, self.title = status, title

    def compose(self):
        yield Label(self.title, classes="heading", id=f"heading-{self.status}")
        yield Button("+ Add card", classes="add-card", id=f"add-{self.status}")
        yield BoardList(id=f"list-{self.status}")


class Card(ListItem):
    class Clicked(Message):
        def __init__(self, task):
            super().__init__()
            self.task = task

    def __init__(self, task, source_labels):
        self.values = task
        self.task_id = task["id"]
        self.source_labels = source_labels
        super().__init__(id=f"card-{self.task_id}")

    def compose(self):
        with Horizontal(classes="card-top"):
            yield Static(
                f"#{self.task_id} · {self.values['title']}", classes="card-title", markup=False
            )
            yield Button(
                f"P{self.values['priority']}",
                classes=f"priority p{self.values['priority']}",
                id=f"priority-{self.task_id}",
                tooltip="Change priority",
            )
        if self.values.get("needs_first_touch"):
            yield Static("Needs first touch", classes="first-touch", markup=False)
        for state in self.values.get("agent_states", []):
            yield Static(
                f"Pi · {AGENT_LABELS[state]}", classes=f"agent-badge agent-{state}", markup=False
            )
        schedule = self.values.get("sla", {})
        if schedule.get("badge"):
            badge = Static(
                schedule["badge"], classes=f"sla-badge {schedule['level']}", markup=False
            )
            badge.tooltip = schedule["tooltip"]
            yield badge
        if self.values["status"] == "waiting" and self.values["delegated_to"]:
            yield Static(
                f"Action with {self.values['delegated_to']}",
                classes="card-delegation",
                markup=False,
            )
        labels = " · ".join(filter(None, [self.values["sdk"], self.source_labels]))
        if labels:
            yield Static(labels, classes="card-labels", markup=False)

    def on_click(self, event: events.Click):
        event.prevent_default()
        event.stop()
        if event.button == 1 and not isinstance(event.widget, Button):
            self.post_message(self.Clicked(self.values))


class Board(App):
    TITLE = "SDK support hero"
    CSS = """
    Screen { layout: vertical; }
    #toolbar { height: 3; padding: 0 1; }
    #board-title { width: 20; content-align: left middle; text-style: bold; }
    #search { width: 1fr; }
    #toolbar Button { min-width: 10; margin-left: 1; }
    #filters { height: 3; padding: 0 1; display: none; }
    #filters Input, #filters Select { width: 1fr; }
    #board { height: 1fr; }
    .column { width: 1fr; min-width: 26; height: 100%; border: round $panel; margin: 0 1; }
    .column:focus-within { border: round $accent; }
    .heading { height: 2; content-align: center middle; text-style: bold; }
    .add-card { width: 100%; border: none; height: 2; background: $panel; }
    ListView { height: 1fr; background: $surface; padding: 1 1 0 1; }
    Card { height: auto; padding: 1; margin-bottom: 1; background: $panel; }
    ListView > Card, ListView > Card.-highlight, ListView:focus > Card.-highlight {
        background: $panel; color: $text; text-style: none; border-left: tall transparent;
    }
    ListView > Card.card-selected, ListView:focus > Card.card-selected {
        background: $primary 20%; border-left: tall $accent;
    }
    .card-top { height: auto; min-height: 1; }
    .card-title { width: 1fr; height: auto; max-height: 4; }
    .priority { min-width: 4; width: 4; height: 1; border: none; padding: 0; }
    .priority.p0 { background: $error; color: $text; }
    .priority.p1 { background: $warning; color: $background; }
    .sla-badge { width: auto; height: 1; color: $success; text-style: bold; }
    .sla-badge.soon { color: $warning; }
    .sla-badge.overdue { color: $error; }
    .first-touch { height: auto; color: $warning; text-style: bold; }
    .agent-badge { height: auto; color: $text-muted; }
    .agent-working { color: $accent; }
    .agent-done { color: $success; text-style: bold; }
    .agent-blocked { color: $warning; text-style: bold; }
    .card-labels { height: auto; max-height: 2; color: $text-muted; margin-top: 1; }
    .card-delegation { height: auto; max-height: 2; color: $warning; }
    #sync-status { height: 1; padding: 0 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("n", "new", "New"),
        Binding("e,E", "edit", "Open"),
        Binding("h,left", "column(-1)", "←", show=False),
        Binding("l,right", "column(1)", "→", show=False),
        Binding("j", "row(1)", "Down", show=False),
        Binding("k", "row(-1)", "Up", show=False),
        Binding("H,shift+left", "move(-1)", "Move ←"),
        Binding("L,shift+right", "move(1)", "Move →"),
        Binding("p", "priority", "Priority"),
        Binding("r", "sync", "Refresh"),
        Binding("slash", "search", "Search"),
        Binding("f", "filters", "Filters"),
        Binding("escape", "board", "Board", show=False),
    ]

    def __init__(self, store, config, syncer=None, *, investigator=None, config_file=None):
        super().__init__()
        self.store, self.config = store, config
        self.syncer = syncer or Syncer(store, config)
        self.investigator = investigator or Investigator(store, config_file=config_file)
        self.investigating = set()
        self.agent_states = {}
        self.polling_agents = False
        self.selected = None
        self.column_index = 0
        self.fingerprint = None
        self.syncing = self.rebuilding = False

    def compose(self):
        with Horizontal(id="toolbar"):
            yield Static("Support hero", id="board-title")
            yield Input(placeholder="Search cards, labels, or #number…", id="search")
            yield Button("Filters", id="toggle-filters")
            yield Button("Refresh", id="refresh")
        with Horizontal(id="filters"):
            yield Input(placeholder="SDK", id="sdk-filter")
            yield Select(
                [("All types", "all")] + [(k.replace("_", " "), k) for k in KINDS],
                value="all",
                allow_blank=False,
                id="kind-filter",
            )
            yield Select(
                [("All priorities", -1)] + [(f"P{i}", i) for i in range(4)],
                value=-1,
                allow_blank=False,
                id="priority-filter",
            )
        with HorizontalScroll(id="board"):
            for status, title in zip(STATUSES, TITLES):
                yield Column(status, title)
        yield Static("", markup=False, id="sync-status")
        yield Footer()

    async def on_mount(self):
        await self.refresh_board()
        self.action_board()
        self.set_interval(2, self.refresh_board)
        self.poll_agent_states()
        self.set_interval(3, self.poll_agent_states)

    def poll_agent_states(self):
        if not self.polling_agents:
            self.polling_agents = True
            self.agent_state_worker()

    @work(thread=True, group="agent-states")
    def agent_state_worker(self):
        try:
            states = self.investigator.card_states()
        except Exception:
            states = {task_id: ["unavailable"] for task_id in self.agent_states}
        self.call_from_thread(self.agent_states_received, states)

    async def agent_states_received(self, states):
        self.polling_agents = False
        if states != self.agent_states:
            self.agent_states = states
            await self.refresh_board()

    @on(Input.Changed, "#search")
    @on(Input.Changed, "#sdk-filter")
    @on(Select.Changed, "#kind-filter")
    @on(Select.Changed, "#priority-filter")
    async def filters_changed(self):
        await self.refresh_board()

    def current_list(self):
        return self.query_one(f"#list-{STATUSES[self.column_index]}", ListView)

    async def refresh_board(self):
        if not self.is_mounted or self.rebuilding or isinstance(self.screen, ModalScreen):
            return
        search = self.query_one("#search", Input).value.lower()
        sdk = self.query_one("#sdk-filter", Input).value.lower()
        kind = self.query_one("#kind-filter", Select).value
        priority = self.query_one("#priority-filter", Select).value
        tasks = self.store.tasks()
        labels = {}
        sources = {}
        for source in self.store.sources():
            sources.setdefault(source["task_id"], []).append(source)
            labels.setdefault(source["task_id"], set()).add(
                "Support" if source["kind"] == "ticket" else "GitHub"
            )
        labels = {key: ", ".join(sorted(value)) for key, value in labels.items()}
        for task in tasks:
            task["sla"] = card_schedule(task, sources.get(task["id"], []))
            task["agent_states"] = self.agent_states.get(task["id"], [])
            task["needs_first_touch"] = needs_first_touch(task, sources.get(task["id"], []))
        tasks = [
            task
            for task in tasks
            if matches_search(task, search, labels.get(task["id"], ""))
            and sdk in task["sdk"].lower()
            and (kind == "all" or task["kind"] == kind)
            and (priority == -1 or task["priority"] == priority)
        ]
        tasks.sort(key=lambda task: work_key(task, task["sla"]))
        fingerprint = json.dumps([tasks, labels])
        if fingerprint != self.fingerprint:
            self.rebuilding = True
            try:
                self.fingerprint = fingerprint
                selected_task = next((t for t in tasks if t["id"] == self.selected), None)
                if selected_task:
                    self.column_index = STATUSES.index(selected_task["status"])
                else:
                    self.selected = None
                for status, title in zip(STATUSES, TITLES):
                    listing = self.query_one(f"#list-{status}", ListView)
                    cards = [t for t in tasks if t["status"] == status]
                    old_index = listing.index or 0
                    await listing.clear()
                    await listing.extend(Card(t, labels.get(t["id"], "")) for t in cards)
                    listing.index = (
                        next(
                            (i for i, t in enumerate(cards) if t["id"] == self.selected),
                            min(old_index, len(cards) - 1),
                        )
                        if cards
                        else None
                    )
                    if status == STATUSES[self.column_index] and self.selected is None:
                        listing.index = None
                    self.query_one(f"#heading-{status}", Label).update(f"{title} · {len(cards)}")
                self.select_current()
            finally:
                self.rebuilding = False
        if not self.syncing:
            states = self.store.sync_states()
            failed = sum(bool(s["error"]) for s in states)
            expected = {f"github:{r.lower()}" for r in self.config["repos"]}
            expected.update(s["scope"] for s in self.store.sources())
            if self.config.get("support"):
                expected.add(support_scope(self.config["support"]))
            unchecked = expected - {s["scope"] for s in states if s["succeeded_at"]}
            oldest = min((s["succeeded_at"] or "never" for s in states), default="never")
            self.sync_message(
                f"External services read-only · checked {oldest}"
                + (f" · {failed} failed (sdk-hero status)" if failed else "")
                + (f" · {len(unchecked)} unchecked" if unchecked else "")
            )

    def select_current(self):
        child = self.current_list().highlighted_child
        self.selected = child.task_id if isinstance(child, Card) else None
        for card in self.query(Card):
            card.set_class(card.task_id == self.selected, "card-selected")

    def on_descendant_focus(self, event: events.DescendantFocus):
        if isinstance(event.widget, ListView) and not self.rebuilding:
            self.column_index = STATUSES.index(event.widget.id.removeprefix("list-"))
            self.select_current()

    @on(ListView.Highlighted)
    def highlight(self, event):
        if not self.rebuilding and event.list_view.has_focus:
            self.column_index = STATUSES.index(event.list_view.id.removeprefix("list-"))
            self.select_current()

    @on(ListView.Selected)
    def selected_card(self, event):
        self.open_card(event.item.task_id)

    @on(Card.Clicked)
    def clicked_card(self, event):
        if self.selected == event.task["id"]:
            self.open_card(event.task["id"])
        else:
            self.selected = event.task["id"]
            self.column_index = STATUSES.index(event.task["status"])
            self.action_board()

    def open_card(self, task_id):
        self.selected = task_id

        async def closed(_):
            try:
                self.column_index = STATUSES.index(self.store.get(task_id)["status"])
            except ValueError:
                self.selected = None
            await self.refresh_board()
            self.action_board()

        try:
            details = CardDetails(self.store, task_id)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return
        self.push_screen(details, closed)

    @on(Column.MoveSelected)
    async def move_selected(self, event):
        if self.rebuilding:
            return
        for card in self.query(Card):
            if card.task_id == self.selected:
                if card.values["status"] != event.status:
                    await self.change_card(card.values, status=event.status)
                return

    async def change_card(self, task, **patch):
        try:
            self.store.update(task["id"], expected_revision=task["revision"], actor="you", **patch)
            self.selected = task["id"]
            self.column_index = STATUSES.index(patch.get("status", task["status"]))
        except (Conflict, ValueError) as error:
            self.notify(str(error), severity="warning")
        await self.refresh_board()
        self.action_board()

    @on(Button.Pressed, ".priority")
    def click_priority(self, event):
        event.stop()
        self.pick_priority(int(event.button.id.removeprefix("priority-")))

    def pick_priority(self, task_id):
        try:
            task = self.store.get(task_id)
        except ValueError as error:
            self.notify(str(error), severity="warning")
            return

        async def picked(value):
            if value is not None:
                await self.change_card(task, priority=value)

        self.push_screen(PriorityPicker(), picked)

    @on(Button.Pressed, ".add-card")
    def click_add(self, event):
        self.new_card(event.button.id.removeprefix("add-"))

    def new_card(self, status):
        async def saved(title):
            if title is not None:
                self.selected = self.store.create(title, status=status, actor="you")
                self.column_index = STATUSES.index(status)
                await self.refresh_board()
                self.action_board()

        self.push_screen(AddCard(status), saved)

    @on(Button.Pressed, "#toggle-filters")
    def action_filters(self):
        filters = self.query_one("#filters")
        filters.display = not filters.display

    def action_column(self, delta):
        self.column_index = (self.column_index + delta) % len(STATUSES)
        self.action_board()

    def action_board(self):
        listing = self.current_list()
        if listing.index is None and listing.children:
            listing.index = 0
        for index, card in enumerate(listing.children):
            if card.task_id == self.selected:
                listing.index = index
                break
        listing.focus()
        self.select_current()

    def action_row(self, delta):
        listing = self.current_list()
        if listing.children:
            listing.index = max(0, min(len(listing.children) - 1, (listing.index or 0) + delta))

    def action_search(self):
        self.query_one("#search", Input).focus()

    async def action_move(self, delta):
        if self.selected is not None:
            try:
                task = self.store.get(self.selected)
            except ValueError as error:
                self.notify(str(error), severity="warning")
                return
            index = max(0, min(4, STATUSES.index(task["status"]) + delta))
            await self.change_card(task, status=STATUSES[index])

    def action_priority(self):
        if self.selected is not None:
            self.pick_priority(self.selected)

    def action_edit(self):
        if self.selected is not None:
            self.open_card(self.selected)

    def action_new(self):
        self.new_card(STATUSES[self.column_index])

    def start_investigation(self, task_id, workspace, instruction):
        if task_id in self.investigating:
            self.notify("This card already has a launch in progress")
            return
        self.investigating.add(task_id)
        if isinstance(self.screen, CardDetails):
            self.screen.query_one("#detail-investigate", Button).disabled = True
        self.investigation_worker(task_id, workspace, instruction)

    @work(thread=True)
    def investigation_worker(self, task_id, workspace, instruction):
        try:
            self.investigator.launch(task_id, workspace, instruction)
            message, severity = "Pi started in Herdr; session recorded in Updates", "information"
        except Exception as error:
            message, severity = str(error), "error"
        if self.is_running:
            self.call_from_thread(self.investigation_finished, task_id, message, severity)

    def investigation_finished(self, task_id, message, severity):
        self.investigating.discard(task_id)
        self.notify(message, severity=severity)
        if isinstance(self.screen, CardDetails) and self.screen.task_id == task_id:
            self.screen.call_later(self.screen.refresh_evidence)

    @work(thread=True)
    def focus_card_pi(self, task_id):
        try:
            self.investigator.focus_card(task_id)
        except Exception as error:
            if self.is_running:
                self.call_from_thread(self.notify, str(error), severity="warning")
        if self.is_running:
            self.call_from_thread(self.poll_agent_states)

    @work(thread=True)
    def focus_investigation(self, details):
        try:
            self.investigator.focus(details)
        except Exception as error:
            if self.is_running:
                self.call_from_thread(self.notify, str(error), severity="warning")

    @on(Button.Pressed, "#refresh")
    def action_sync(self):
        self.start_sync()

    def start_sync(self, task_id=None):
        if self.syncing:
            self.notify("Refresh already running")
            return False
        self.syncing = True
        self.sync_worker(task_id)
        return True

    def sync_message(self, text):
        self.query_one("#sync-status", Static).update(text)

    @work(thread=True)
    def sync_worker(self, task_id=None):
        try:

            def progress(text):
                self.call_from_thread(self.sync_message, text)

            if task_id is None:
                results = self.syncer.sync(progress=progress)
                unit = "scopes"
            else:
                results = self.syncer.sync_card(task_id, progress=progress)
                unit = "sources"
            failures = sum(error is not None for error in results.values())
            self.call_from_thread(
                self.notify, f"Refreshed {len(results)} {unit}; {failures} failed"
            )
        except Exception as error:
            self.call_from_thread(self.notify, str(error), severity="error")
        finally:
            self.syncing = False
            self.call_from_thread(self.refresh_after_sync)

    async def refresh_after_sync(self):
        await self.refresh_board()
        if isinstance(self.screen, CardDetails):
            self.screen.call_later(self.screen.refresh_evidence)
