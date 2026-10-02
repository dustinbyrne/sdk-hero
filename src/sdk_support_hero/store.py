from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .sla import parse_date
from .updates import source_change_summary

STATUSES = ("inbox", "ready", "in_progress", "waiting", "done")
KINDS = ("issue", "support", "dependency", "ci_release", "external_pr", "other")
FIELDS = {"title", "status", "priority", "sdk", "kind", "description", "sla_source", "delegated_to"}
TASK_COLUMNS = (
    "id,title,status,priority,sdk,kind,description,sla_source,delegated_to,"
    "revision,created_at,updated_at"
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return Path(os.environ.get("SDK_HERO_DB", str(base / "sdk-support-hero/board.db"))).expanduser()


class Conflict(ValueError):
    pass


def meaningful_facts(facts):
    # Poll timestamps and read/unread state aren't new work. Check ordering is not semantic.
    result = {
        k: v
        for k, v in facts.items()
        if k not in {"updated_at", "updatedAt", "unread_team_count", "classification"}
    }
    if "checks" in result:
        result["checks"] = sorted(
            result["checks"], key=lambda check: json.dumps(check, sort_keys=True)
        )
    return result


class Store:
    """Short-lived transactions; task changes and their history commit together."""

    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise ValueError(f"Unsupported database version {version}; upgrade sdk-hero")
            if version == 0:
                db.execute("""CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'inbox'
                        CHECK(status IN ('inbox','ready','in_progress','waiting','done')),
                    priority INTEGER NOT NULL DEFAULT 2 CHECK(priority BETWEEN 0 AND 3),
                    sdk TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'other',
                    description TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                db.execute("""CREATE TABLE sources (
                    key TEXT PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                    scope TEXT NOT NULL, kind TEXT NOT NULL, remote_id TEXT NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    facts TEXT NOT NULL DEFAULT '{}', observed_at TEXT, error TEXT, error_at TEXT
                )""")
                db.execute("CREATE INDEX sources_task ON sources(task_id)")
                db.execute("""CREATE TABLE sync_state (
                    scope TEXT PRIMARY KEY, attempted_at TEXT NOT NULL,
                    succeeded_at TEXT, error TEXT
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id),
                created_at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
                summary TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS updates_task ON updates(task_id,id)")
            if version == 1:
                db.execute("ALTER TABLE tasks ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                for task in db.execute("SELECT * FROM tasks").fetchall():
                    parts = [
                        f"{label}: {task[field]}"
                        for field, label in (
                            ("owner", "Next owner"),
                            ("next_action", "Next action"),
                            ("blocker", "Blocker"),
                            ("notes", "Notes"),
                        )
                        if task[field]
                    ]
                    description = "\n\n".join(parts)
                    db.execute(
                        "UPDATE tasks SET description=? WHERE id=?", (description, task["id"])
                    )
                    self._record(
                        db,
                        task["id"],
                        "migration",
                        "migration",
                        "Combined previous fields into description",
                        {"description": description},
                    )
            db.execute("CREATE TABLE IF NOT EXISTS deleted_tasks (id INTEGER PRIMARY KEY)")
            db.execute("CREATE TABLE IF NOT EXISTS dismissed_sources (key TEXT PRIMARY KEY)")
            if version < 4 and "sla_source" not in {
                row["name"] for row in db.execute("PRAGMA table_info(tasks)")
            }:
                db.execute("ALTER TABLE tasks ADD COLUMN sla_source TEXT NOT NULL DEFAULT ''")
            if version < 5 and "sla_resolved" not in {
                row["name"] for row in db.execute("PRAGMA table_info(sources)")
            }:
                db.execute(
                    "ALTER TABLE sources ADD COLUMN sla_resolved INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(sla_resolved IN (0,1))"
                )
            if version < 6 and "delegated_to" not in {
                row["name"] for row in db.execute("PRAGMA table_info(tasks)")
            }:
                db.execute("ALTER TABLE tasks ADD COLUMN delegated_to TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA user_version=6")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def validate(fields):
        if fields.keys() - FIELDS:
            raise ValueError("Unknown task fields")
        if "title" in fields and not fields["title"].strip():
            raise ValueError("Title cannot be empty")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Invalid status")
        if "kind" in fields and fields["kind"] not in KINDS:
            raise ValueError("Invalid responsibility")
        if "priority" in fields and fields["priority"] not in range(4):
            raise ValueError("Priority must be 0–3")
        if "delegated_to" in fields:
            if not isinstance(fields["delegated_to"], str):
                raise ValueError("Delegated to must be a person or team name")
        if "sla_source" in fields and not isinstance(fields["sla_source"], str):
            raise ValueError("SLA source must be a linked source key or empty for Automatic")

    @staticmethod
    def _record(db, task_id, actor, kind, summary, details=None):
        db.execute(
            "INSERT INTO updates(task_id,created_at,actor,kind,summary,details) "
            "VALUES(?,?,?,?,?,?)",
            (task_id, now(), actor, kind, summary, json.dumps(details or {}, ensure_ascii=False)),
        )

    @staticmethod
    def _create(db, fields, actor="cli"):
        Store.validate(fields)
        if "delegated_to" in fields:
            fields = {**fields, "delegated_to": fields["delegated_to"].strip()}
        if fields.get("sla_source"):
            raise ValueError("Link a source before selecting it for the SLA")
        if fields.get("delegated_to") and fields.get("status", "inbox") != "waiting":
            raise ValueError("Delegation requires the Waiting column")
        # Retire deleted IDs so a stale editor cannot overwrite a newly created card.
        next_id = db.execute(
            "SELECT COALESCE(MAX(id), 0) + 1 FROM "
            "(SELECT id FROM tasks UNION ALL SELECT id FROM deleted_tasks)"
        ).fetchone()[0]
        fields = {**fields, "id": next_id, "created_at": now(), "updated_at": now()}
        task_id = db.execute(
            f"INSERT INTO tasks ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
            list(fields.values()),
        ).lastrowid
        Store._record(db, task_id, actor, "created", "Card created", fields)
        return task_id

    def create(self, title: str, *, actor="cli", **fields) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._create(db, {"title": title, **fields}, actor)

    def delete(self, task_id: int, *, expected_revision: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None or task["revision"] != expected_revision:
                raise Conflict("Task missing or changed elsewhere; reload before deleting")
            db.execute("INSERT INTO deleted_tasks(id) VALUES(?)", (task_id,))
            db.execute(
                "INSERT OR IGNORE INTO dismissed_sources(key) "
                "SELECT key FROM sources WHERE task_id=?",
                (task_id,),
            )
            db.execute("DELETE FROM updates WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM sources WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    def update(self, task_id: int, *, expected_revision: int | None = None, actor="cli", **fields):
        self.validate(fields)
        if "delegated_to" in fields:
            fields["delegated_to"] = fields["delegated_to"].strip()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if old is None or (
                expected_revision is not None and old["revision"] != expected_revision
            ):
                raise Conflict("Task missing or changed elsewhere; reload before editing")
            if fields.get("status", old["status"]) != "waiting":
                if fields.get("delegated_to"):
                    raise ValueError("Delegation requires the Waiting column")
                if old["delegated_to"]:
                    fields["delegated_to"] = ""
            changes = {k: {"before": old[k], "after": v} for k, v in fields.items() if old[k] != v}
            if not changes:
                return
            if fields.get("sla_source") and "sla_source" in changes:
                if not db.execute(
                    "SELECT 1 FROM sources WHERE key=? AND task_id=?",
                    (fields["sla_source"], task_id),
                ).fetchone():
                    raise ValueError("SLA source must be linked to this card")
            fields = {k: change["after"] for k, change in changes.items()}
            db.execute(
                f"UPDATE tasks SET {','.join(f'{key}=?' for key in fields)}, "
                "updated_at=?, revision=revision+1 WHERE id=?",
                [*fields.values(), now(), task_id],
            )
            self._record(
                db, task_id, actor, "edited", "Changed " + ", ".join(sorted(fields)), changes
            )

    def add_note(self, task_id, text, *, actor="cli"):
        if not text.strip():
            raise ValueError("Update cannot be empty")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ValueError(f"Task #{task_id} not found")
            self._record(db, task_id, actor, "note", text)

    def investigation_update(self, task_id, summary, details):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ValueError(f"Task #{task_id} not found")
            self._record(db, task_id, "you", "investigation", summary, details)

    def investigation_sessions(self):
        """Latest launch checkpoint per run, without loading unrelated card history."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT task_id, details FROM updates WHERE kind='investigation' ORDER BY id"
            ).fetchall()
        sessions = {}
        for row in rows:
            details = json.loads(row["details"])
            if details.get("run_id") and details.get("session_file"):
                sessions[(row["task_id"], details["run_id"])] = {
                    **details,
                    "card_id": row["task_id"],
                }
        return list(sessions.values())

    def history(self, task_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM updates WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]

    def get(self, task_id: int, *, brief=False) -> dict:
        with self.connect() as db:
            row = db.execute(f"SELECT {TASK_COLUMNS} FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Task #{task_id} not found")
            if brief:
                sources = db.execute(
                    "SELECT key,kind,url,title FROM sources WHERE task_id=?", (task_id,)
                ).fetchall()
                return {**dict(row), "sources": [dict(source) for source in sources]}
        return {
            **dict(row),
            "sources": self.sources(task_id=task_id),
            "updates": self.history(task_id),
        }

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(f"SELECT {TASK_COLUMNS} FROM tasks ORDER BY priority,id")
            ]

    def sources(self, *, task_id=None, scope=None) -> list[dict]:
        where, args = [], []
        for key, value in (("task_id", task_id), ("scope", scope)):
            if value is not None:
                where.append(f"{key}=?")
                args.append(value)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sources" + (" WHERE " + " AND ".join(where) if where else ""), args
            ).fetchall()
        return [{**dict(row), "facts": json.loads(row["facts"])} for row in rows]

    def link(self, task_id: int, source: dict, *, actor="cli"):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ValueError(f"Task #{task_id} not found")
            existing = db.execute(
                "SELECT task_id FROM sources WHERE key=?", (source["key"],)
            ).fetchone()
            if existing:
                if existing[0] != task_id:
                    raise Conflict(f"Already linked to task #{existing[0]}; use source move")
                return
            db.execute(
                "INSERT INTO sources(key,task_id,scope,kind,remote_id,url) VALUES(?,?,?,?,?,?)",
                (
                    source["key"],
                    task_id,
                    source["scope"],
                    source["kind"],
                    source["remote_id"],
                    source["url"],
                ),
            )
            db.execute("DELETE FROM dismissed_sources WHERE key=?", (source["key"],))
            self._record(db, task_id, actor, "linked", "Source linked", {"url": source["url"]})

    def move_source(self, key: str, task_id: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
                raise ValueError(f"Task #{task_id} not found")
            source = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if source is None:
                raise ValueError("Source not found")
            if source["task_id"] == task_id:
                return
            db.execute("UPDATE sources SET task_id=? WHERE key=?", (task_id, key))
            self._record(
                db,
                source["task_id"],
                "cli",
                "unlinked",
                f"Source moved to card #{task_id}",
                {"url": source["url"]},
            )
            self._record(
                db,
                task_id,
                "cli",
                "linked",
                f"Source moved from card #{source['task_id']}",
                {"url": source["url"]},
            )

    def observe(self, source: dict, title: str, facts: dict, *, kind="other", sdk="") -> int | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM dismissed_sources WHERE key=?", (source["key"],)
            ).fetchone():
                return None
            old = db.execute("SELECT * FROM sources WHERE key=?", (source["key"],)).fetchone()
            task_id = (
                old["task_id"]
                if old
                else self._create(db, {"title": title, "kind": kind, "sdk": sdk}, "sync")
            )
            previous = (
                {"title": old["title"], **meaningful_facts(json.loads(old["facts"]))} if old else {}
            )
            current = {"title": title, **meaningful_facts(facts)}
            changes = {
                k: {"before": previous.get(k), "after": current.get(k)}
                for k in sorted(previous.keys() | current.keys())
                if previous.get(k) != current.get(k)
            }
            if old and source["kind"] == "pr" and "needs_team_reply" not in previous:
                # Backfilling conversation metadata is not itself new source activity.
                observed = parse_date(old["observed_at"])
                activity = [
                    parse_date(facts.get(key))
                    for key in ("last_external_reply_at", "last_team_reply_at")
                ]
                if observed and not any(date and date > observed for date in activity):
                    for key in (
                        "awaiting_team_since",
                        "last_external_reply_at",
                        "last_team_reply_at",
                        "needs_team_reply",
                    ):
                        changes.pop(key, None)
            if old is None or old["observed_at"] is None:
                self._record(
                    db,
                    task_id,
                    "sync",
                    "observed",
                    "First source observation",
                    {"url": source["url"], "facts": current},
                )
            elif changes:
                self._record(
                    db,
                    task_id,
                    "sync",
                    "source_changed",
                    source_change_summary(source, changes),
                    {"url": source["url"], "changes": changes, "display": "summary"},
                )
            if old and old["error"]:
                self._record(
                    db,
                    task_id,
                    "sync",
                    "recovered",
                    "Source refresh recovered",
                    {"url": source["url"]},
                )
            db.execute(
                """INSERT INTO sources
                    (key,task_id,scope,kind,remote_id,url,title,facts,observed_at)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    url=excluded.url,title=excluded.title,facts=excluded.facts,
                    observed_at=excluded.observed_at,error=NULL,error_at=NULL""",
                (
                    source["key"],
                    task_id,
                    source["scope"],
                    source["kind"],
                    source["remote_id"],
                    source["url"],
                    title,
                    json.dumps(facts),
                    now(),
                ),
            )
            if old and old["sla_resolved"] and changes:
                db.execute("UPDATE sources SET sla_resolved=0 WHERE key=?", (source["key"],))
                self._record(
                    db,
                    task_id,
                    "sync",
                    "sla_reopened",
                    "Cleared local SLA resolution after source update",
                    {"url": source["url"]},
                )
            if old and old["observed_at"] and facts.get("needs_team_reply"):
                previous_facts = json.loads(old["facts"])
                previous_reply = parse_date(previous_facts.get("last_external_reply_at"))
                baseline = previous_reply or parse_date(old["observed_at"])
                reply = parse_date(facts.get("last_external_reply_at"))
                if reply and baseline and reply > baseline:
                    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if task["status"] == "done" or (
                        task["status"] == "waiting" and not task["delegated_to"]
                    ):
                        selection = task["sla_source"]
                        changes = {"status": {"before": task["status"], "after": "inbox"}}
                        if selection and selection != source["key"]:
                            changes["sla_source"] = {"before": selection, "after": ""}
                            selection = ""
                        db.execute(
                            "UPDATE tasks SET status='inbox',sla_source=?,updated_at=?,"
                            "revision=revision+1 WHERE id=?",
                            (selection, now(), task_id),
                        )
                        self._record(
                            db,
                            task_id,
                            "sync",
                            "reopened",
                            "Returned to Inbox: new external reply awaiting us",
                            {
                                "url": source["url"],
                                "reply_at": reply.isoformat(),
                                "changes": changes,
                            },
                        )
            return task_id

    def complete_merged_cards(self, observations, snapshots):
        """Complete verified work and return IDs transitioned by this transaction."""
        completed = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for snapshot in snapshots:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (snapshot["id"],)).fetchone()
                if not task or task["status"] == "done" or task["revision"] != snapshot["revision"]:
                    continue
                sources = db.execute(
                    "SELECT * FROM sources WHERE task_id=?", (task["id"],)
                ).fetchall()
                keys = {source["key"] for source in sources}
                if keys != snapshot["source_keys"] or not keys <= observations.keys():
                    continue
                if not any(s["kind"] in ("pr", "ticket") for s in sources):
                    continue
                complete = True
                for source in sources:
                    facts = json.loads(source["facts"])
                    observed = observations[source["key"]]
                    state = str(
                        facts.get("status" if source["kind"] == "ticket" else "state", "")
                    ).lower()
                    if (
                        source["error"]
                        or observed != {"title": source["title"], "facts": facts}
                        or (source["kind"], state)
                        not in (("pr", "merged"), ("issue", "closed"), ("ticket", "resolved"))
                    ):
                        complete = False
                        break
                    if source["kind"] != "ticket" and facts.get("needs_team_reply"):
                        ended = parse_date(
                            facts.get("mergedAt" if source["kind"] == "pr" else "closed_at")
                        )
                        reply = parse_date(facts.get("last_external_reply_at"))
                        if not ended or not reply or reply >= ended:
                            complete = False
                            break
                if not complete:
                    continue
                changes = {"status": {"before": task["status"], "after": "done"}}
                if task["delegated_to"]:
                    changes["delegated_to"] = {"before": task["delegated_to"], "after": ""}
                db.execute(
                    "UPDATE tasks SET status='done',delegated_to='',updated_at=?,"
                    "revision=revision+1 WHERE id=?",
                    (now(), task["id"]),
                )
                self._record(
                    db,
                    task["id"],
                    "sync",
                    "completed",
                    "Moved to Done: all linked work finished",
                    {
                        "changes": changes,
                        "sources": [s["url"] for s in sources],
                    },
                )

                completed.append(task["id"])
        return completed

    def set_sla_resolved(self, task_id, source, resolved, *, actor="you"):
        """Change only the displayed source snapshot's local SLA resolution."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sources WHERE key=?", (source["key"],)).fetchone()
            if (
                old is None
                or old["task_id"] != task_id
                or old["title"] != source["title"]
                or meaningful_facts(json.loads(old["facts"])) != meaningful_facts(source["facts"])
                or bool(old["sla_resolved"]) != bool(source.get("sla_resolved"))
            ):
                raise Conflict("Source changed or moved; check its latest state and try again")
            if bool(old["sla_resolved"]) == resolved:
                return
            db.execute("UPDATE sources SET sla_resolved=? WHERE key=?", (resolved, source["key"]))
            self._record(
                db,
                task_id,
                actor,
                "sla_resolved" if resolved else "sla_reopened",
                "Marked source SLA resolved until its next update"
                if resolved
                else "Cleared local SLA resolution",
                {"url": old["url"]},
            )

    def source_error(self, key, error):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if old and old["error"] != error:
                self._record(
                    db,
                    old["task_id"],
                    "sync",
                    "error",
                    "Source refresh failed",
                    {"url": old["url"], "error": error},
                )
            db.execute("UPDATE sources SET error=?,error_at=? WHERE key=?", (error, now(), key))

    def sync_result(self, scope, error=None):
        with self.connect() as db:
            db.execute(
                """INSERT INTO sync_state(scope,attempted_at,succeeded_at,error) VALUES(?,?,?,?)
                ON CONFLICT(scope) DO UPDATE SET attempted_at=excluded.attempted_at,
                succeeded_at=COALESCE(excluded.succeeded_at,sync_state.succeeded_at),
                error=excluded.error""",
                (scope, now(), None if error else now(), error),
            )

    def sync_states(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM sync_state ORDER BY scope")]
