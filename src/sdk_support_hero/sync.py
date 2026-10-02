from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import yaml

from .store import Store

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PR_FIELDS = (
    "title,url,state,isDraft,headRefOid,mergedAt,reviewDecision,statusCheckRollup,"
    "updatedAt,author,labels"
)


class SyncError(ValueError):
    pass


def run_json(argv, env=None):
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, **(env or {}), "GH_PROMPT_DISABLED": "1"},
        )
    except FileNotFoundError:
        raise SyncError(f"{argv[0]} not installed") from None
    except subprocess.TimeoutExpired:
        raise SyncError(f"{argv[0]} timed out after 90s") from None
    if result.returncode:
        # Do not persist raw stderr: it can contain customer data or credentials.
        detail = (
            "authentication/permission denied"
            if any(word in result.stderr.lower() for word in ("401", "403", "auth", "permission"))
            else "command failed; inspect CLI authentication and access"
        )
        raise SyncError(f"{argv[0]} exit {result.returncode}: {detail}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SyncError(f"{argv[0]} returned invalid JSON; check CLI version") from None


def inventory_repos(path: Path) -> list[str]:
    text = path.expanduser().read_text()
    try:
        inventory = text.split("<!-- posthog-sdk-inventory:start -->")[1].split(
            "<!-- posthog-sdk-inventory:end -->"
        )[0]
    except IndexError:
        raise ValueError("Canonical SDK inventory markers not found") from None
    repos = sorted(set(re.findall(r"https://github.com/(PostHog/[\w-]+)", inventory)))
    if not repos:
        raise ValueError("Canonical SDK inventory is empty")
    return repos


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if override := os.environ.get("SDK_HERO_CONFIG"):
        return Path(override)
    default = base / "sdk-support-hero/config.yml"
    legacy = default.with_suffix(".json")
    return legacy if not default.exists() and legacy.exists() else default


def load_config(path: Path) -> dict:
    if not path.exists():
        return {"repos": [], "support": None}
    try:
        config = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ValueError("Invalid YAML configuration") from error
    validate_config(config)
    return config


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    if not isinstance(config.get("repos"), list) or not all(
        isinstance(repo, str) and REPO.fullmatch(repo) for repo in config["repos"]
    ):
        raise ValueError("config.repos must contain owner/repository names")
    if type(config.get("auto_close_pi_on_done", True)) is not bool:
        raise ValueError("config.auto_close_pi_on_done must be a boolean")
    support = config.get("support")
    if support is not None:
        if not isinstance(support, dict):
            raise ValueError("config.support must be an object or null")
        for key in ("host", "view", "view_name"):
            if not isinstance(support.get(key), str) or not support[key].strip():
                raise ValueError(f"config.support.{key} must be a non-empty string")
        host = urlparse(support["host"])
        if (
            host.scheme != "https"
            or not host.netloc
            or host.username
            or host.path not in ("", "/")
            or host.query
            or host.fragment
        ):
            raise ValueError("Support host must be an HTTPS origin")
        if type(support.get("project")) is not int or support["project"] <= 0:
            raise ValueError("Support project must be a positive integer")


def parse_source(url: str, config: dict) -> dict:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.query
        or parsed.fragment
        or parsed.params
    ):
        raise ValueError("Use a canonical HTTPS item URL without query or fragment")
    if parsed.netloc == "github.com":
        match = re.fullmatch(r"/([^/]+/[^/]+)/(issues|pull|actions/runs)/(\d+)", path)
        if match and REPO.fullmatch(match[1]):
            repo, route, number = match.groups()
            kind = {"issues": "issue", "pull": "pr", "actions/runs": "run"}[route]
            # GitHub issue/PR numbers share a namespace.
            item_type = "run" if kind == "run" else "item"
            return {
                "key": f"github:{repo.lower()}:{item_type}:{number}",
                "scope": f"github:{repo.lower()}",
                "kind": kind,
                "remote_id": number,
                "url": f"https://github.com/{repo}/{route}/{number}",
            }
    support = config.get("support")
    if support and parsed.netloc == urlparse(support["host"]).netloc:
        match = re.fullmatch(rf"/project/{support['project']}/support/tickets/([\w-]+)", path)
        if match:
            scope = support_scope(support)
            return {
                "key": f"{scope}:{match[1]}",
                "scope": scope,
                "kind": "ticket",
                "remote_id": match[1],
                "url": url,
            }
    raise ValueError(
        "Supported URLs: GitHub issue, pull, actions/runs, or configured support ticket"
    )


def support_scope(config):
    return f"posthog:{config['host'].rstrip('/')}:{config['project']}"


@contextmanager
def sync_lock(path: Path):
    with path.with_suffix(".sync.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("Another sync is already running") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def rotation_start(moment=None):
    """Most recent Saturday midnight in the host's local timezone."""
    moment = moment or datetime.now()
    saturday = moment - timedelta(days=(moment.weekday() - 5) % 7)
    return saturday.replace(hour=0, minute=0, second=0, microsecond=0).astimezone()


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except (AttributeError, TypeError, ValueError):
        raise SyncError("Missing or invalid conversation timestamp") from None


def conversation_facts(events):
    latest = {}
    for role, date in events:
        if role is not None:
            instant = timestamp(date)
            latest[role] = max(instant, latest.get(role, instant))
    external, team = latest.get("external"), latest.get("team")
    unanswered = [
        timestamp(date)
        for role, date in events
        if role == "external" and (not team or timestamp(date) > team)
    ]
    return {
        "awaiting_team_since": min(unanswered).isoformat() if unanswered else None,
        "last_external_reply_at": external.isoformat() if external else None,
        "last_team_reply_at": team.isoformat() if team else None,
        "needs_team_reply": bool(external and (not team or external > team)),
    }


def github_role(item):
    user = item.get("user") or {}
    if user.get("type") == "Bot" or user.get("login", "").endswith("[bot]"):
        return None
    association = item.get("author_association")
    if association in ("OWNER", "MEMBER", "COLLABORATOR"):
        return "team"
    if association in ("NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "MANNEQUIN"):
        return "external"
    raise SyncError("Unknown GitHub author association; verify conversation manually")


def pr_review_facts(item, data, timeline, previous=None):
    """Replay human review cycles; acknowledgments do not satisfy an initial review."""
    replies = []
    for entry in timeline:
        if entry["event"] == "commented":
            replies.append((github_role(entry), entry["created_at"]))
        elif entry["event"] == "reviewed" and entry.get("state", "").lower() in (
            "approved",
            "changes_requested",
            "commented",
            "dismissed",
        ):
            replies.append((github_role(entry), entry["submitted_at"]))
    reply_facts = conversation_facts(replies)
    if github_role(item) != "external":
        return {**reply_facts, "review_waiting_since": None, "review_target_hours": None}
    previous = previous or {}
    timeline = [
        {**e, "event": "convert_to_draft"} if e["event"] == "converted_to_draft" else e
        for e in timeline
    ]
    reviews = [
        e
        for e in timeline
        if e["event"] == "reviewed"
        and e.get("state", "").lower()
        in ("approved", "changes_requested", "commented", "dismissed")
        and github_role(e) == "team"
    ]
    latest_review = max(reviews, key=lambda e: timestamp(e["submitted_at"]), default=None)
    head_date = None
    if latest_review and latest_review.get("commit_id") != data.get("headRefOid"):
        if (
            previous.get("headRefOid") == data.get("headRefOid")
            and "review_head_updated_at" in previous
        ):
            head_date = previous["review_head_updated_at"]
        else:
            head_date = data.get("updatedAt") or item.get("updated_at")
    transitions = [e for e in timeline if e["event"] in ("ready_for_review", "convert_to_draft")]
    initially_draft = bool(transitions and transitions[0]["event"] == "ready_for_review")
    if not transitions:
        initially_draft = bool(data.get("isDraft"))
    events = [(timestamp(item["created_at"]), "opened")]
    for entry in timeline:
        kind = entry["event"]
        if kind in ("ready_for_review", "convert_to_draft"):
            events.append((timestamp(entry["created_at"]), kind))
        elif kind == "reviewed" and entry.get("state", "").lower() in (
            "approved",
            "changes_requested",
            "commented",
            "dismissed",
        ):
            role = github_role(entry)
            if role:
                events.append(
                    (timestamp(entry["submitted_at"]), "review" if role == "team" else "external")
                )
        elif kind == "commented":
            role = github_role(entry)
            if role:
                events.append((timestamp(entry["created_at"]), role))
    if head_date:
        events.append((timestamp(head_date), "commit"))
    waiting, reviewed, draft = None, False, initially_draft
    last_response = None
    for date, kind in sorted(events, key=lambda event: (event[0], event[1] != "commit")):
        if kind == "opened":
            if not draft:
                waiting = date
        elif kind == "convert_to_draft":
            draft, waiting, last_response = True, None, date
        elif kind == "ready_for_review":
            draft = False
            waiting = waiting or date
        elif kind == "review":
            reviewed, waiting, last_response = True, None, date
        elif kind == "team" and reviewed:
            waiting, last_response = None, date
        elif kind in ("external", "commit") and reviewed and not draft:
            waiting = waiting or date
    prior_wait = previous.get("review_waiting_since")
    if prior_wait and waiting and (not last_response or timestamp(prior_wait) > last_response):
        waiting = min(waiting, timestamp(prior_wait))
    if data.get("isDraft") or data.get("state", "").lower() != "open":
        waiting = None
    return {
        **reply_facts,
        "review_head_updated_at": head_date,
        "review_waiting_since": waiting.isoformat() if waiting else None,
        "review_target_hours": (4 if reviewed else 8) if waiting else None,
    }


class Syncer:
    def __init__(self, store: Store, config: dict, runner=run_json, *, window_start=None):
        self.store, self.config, self.runner = store, config, runner
        self._window_start_override = window_start
        self.window_start = window_start or rotation_start()
        self.since = self.window_start.astimezone(timezone.utc).isoformat()
        self.refreshed = {}
        self.completed_card_ids = []

    def link_source(self, task_id, url, *, actor="cli"):
        source = parse_source(url.strip(), self.config)
        if source["kind"] == "ticket":
            # Numeric ticket URLs and UUID URLs must share one local source key.
            data = self.posthog("conversations-tickets-retrieve", {"id": source["remote_id"]})
            source["remote_id"] = data["id"]
            source["key"] = f"{source['scope']}:{data['id']}"
            source["url"] = data["_posthogUrl"]
        self.store.link(task_id, source, actor=actor)

    def completion_snapshots(self):
        self.refreshed = {}
        self.completed_card_ids = []
        tasks = self.store.tasks()
        sources = self.store.sources()
        return [
            {
                "id": task["id"],
                "revision": task["revision"],
                "source_keys": {s["key"] for s in sources if s["task_id"] == task["id"]},
            }
            for task in tasks
        ]

    def qualifies(self, facts):
        return (
            facts["needs_team_reply"]
            and timestamp(facts["last_external_reply_at"]) >= self.window_start
        )

    def github_conversation(self, repo, number, item):
        events = [(github_role(item), item.get("created_at"))]
        if item.get("comments", 0):
            for page in range(1, 1001):
                comments = self.gh(
                    f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}"
                )
                if not isinstance(comments, list):
                    raise SyncError("Unexpected GitHub comments response")
                events.extend(
                    (github_role(comment), comment.get("created_at")) for comment in comments
                )
                if len(comments) < 100:
                    break
            else:
                raise SyncError("GitHub comment page limit reached; conversation is incomplete")
        return conversation_facts(events)

    def github_timeline(self, repo, number):
        entries = []
        for page in range(1, 1001):
            batch = self.gh(f"repos/{repo}/issues/{number}/timeline?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise SyncError("Unexpected GitHub timeline response")
            entries.extend(batch)
            if len(batch) < 100:
                return entries
        raise SyncError("GitHub timeline page limit reached; review history is incomplete")

    def ticket_conversation(self, ticket_id):
        events = []
        for offset in range(0, 100000, 200):
            data = self.posthog(
                "conversations-tickets-messages-retrieve",
                {"id": str(ticket_id), "limit": 200, "offset": offset},
            )
            messages = data.get("results")
            if not isinstance(messages, list):
                raise SyncError("Unexpected support messages response")
            for message in messages:
                if not isinstance(message.get("is_private"), bool):
                    raise SyncError(
                        "Missing support message visibility; verify conversation manually"
                    )
                if message["is_private"]:
                    continue
                author = message.get("author_type")
                if author == "customer":
                    role = "external"
                elif author == "support":
                    role = "team"
                elif author == "AI":
                    continue
                else:
                    raise SyncError("Unknown support author type; verify conversation manually")
                events.append((role, message.get("created_at")))
            if not data.get("next"):
                break
            if not messages:
                raise SyncError("Support message pagination did not advance")
        else:
            raise SyncError("Support message page limit reached; conversation is incomplete")
        return conversation_facts(events)

    def gh(self, endpoint):
        return self.runner(["gh", "api", "--method", "GET", endpoint])

    def posthog(self, tool, args):
        support = self.config["support"]
        return self.runner(
            [
                "posthog-cli",
                "--host",
                support["host"],
                "api",
                "call",
                "--json",
                tool,
                json.dumps(args),
            ],
            env={"POSTHOG_CLI_PROJECT_ID": str(support["project"])},
        )

    def github_item(self, source, item=None, *, discover=False):
        repo = source["scope"].removeprefix("github:")
        number = source["remote_id"]
        if source["kind"] == "run":
            data = self.gh(f"repos/{repo}/actions/runs/{number}")
            facts = {
                key: data.get(key)
                for key in (
                    "status",
                    "conclusion",
                    "head_sha",
                    "head_branch",
                    "run_attempt",
                    "updated_at",
                )
            }
            title = f"{data['name']} · run {number}"
            kind = "ci_release"
        else:
            item = item or self.gh(f"repos/{repo}/issues/{number}")
            is_pr = "pull_request" in item or source["kind"] == "pr"
            facts = {
                key: item.get(key)
                for key in ("state", "updated_at", "closed_at", "comments", "author_association")
            }
            facts["labels"] = [label["name"] for label in item.get("labels", [])]
            facts["author"] = item.get("user", {}).get("login")
            title, kind = item["title"], "issue"
            if not is_pr:
                facts.update(self.github_conversation(repo, number, item))
                if discover and (item["state"] != "open" or not self.qualifies(facts)):
                    return
            if is_pr:
                if discover and github_role(item) != "external":
                    return
                data = self.runner(
                    ["gh", "pr", "view", number, "--repo", repo, "--json", PR_FIELDS]
                )
                if discover and (
                    data.get("state", "").lower() != "open"
                    or data.get("isDraft") is not False
                    or timestamp(data.get("updatedAt") or item.get("updated_at"))
                    < self.window_start
                ):
                    return
                facts.update(
                    {
                        key: data.get(key)
                        for key in (
                            "state",
                            "isDraft",
                            "headRefOid",
                            "mergedAt",
                            "reviewDecision",
                            "updatedAt",
                        )
                    }
                )
                facts["checks"] = [
                    {
                        key: check.get(key)
                        for key in ("name", "context", "status", "conclusion", "state")
                    }
                    for check in (data.get("statusCheckRollup") or [])
                ]
                author = facts.get("author") or ""
                external = facts.get("author_association") not in (
                    "OWNER",
                    "MEMBER",
                    "COLLABORATOR",
                )
                kind = "external_pr" if external and not author.endswith("[bot]") else "other"
                if any(word in title.lower() for word in ("chore(deps)", "bump ", "upgrade ")):
                    kind = "dependency"
                facts["classification"] = "Heuristic; verify responsibility from diff"
                previous = next(
                    (
                        s["facts"]
                        for s in self.store.sources(scope=source["scope"])
                        if s["key"] == source["key"]
                    ),
                    {},
                )
                facts.update(
                    pr_review_facts(item, data, self.github_timeline(repo, number), previous)
                )
        if self.store.observe(source, title, facts, kind=kind, sdk=repo.split("/")[1]) is not None:
            self.refreshed[source["key"]] = {"title": title, "facts": facts}

    def github(self, repo, discover=True):
        scope = f"github:{repo.lower()}"
        seen = set()
        tracked = {source["key"] for source in self.store.sources(scope=scope)}
        if discover:
            for page in range(1, 1001):
                query = urlencode(
                    {"state": "open", "since": self.since, "per_page": 100, "page": page}
                )
                items = self.gh(f"repos/{repo}/issues?{query}")
                if not isinstance(items, list):
                    raise SyncError("Unexpected GitHub issue-list response")
                for item in items:
                    route = "pull" if "pull_request" in item else "issues"
                    source = parse_source(
                        f"https://github.com/{repo}/{route}/{item['number']}", self.config
                    )
                    try:
                        self.github_item(source, item, discover=source["key"] not in tracked)
                    except (SyncError, KeyError, TypeError) as error:
                        self.store.source_error(source["key"], str(error))
                        raise
                    seen.add(source["key"])
                if len(items) < 100:
                    break
            else:
                raise SyncError("GitHub page limit reached; discovery is incomplete")
        # Closed/merged/transferred items must not vanish when absent from an open listing.
        for source in self.store.sources(scope=scope):
            if source["key"] not in seen:
                try:
                    self.github_item(source)
                except (SyncError, KeyError, TypeError) as error:
                    self.store.source_error(source["key"], str(error))
                    raise

    def ticket(self, ticket_id, *, discover=False):
        data = self.posthog("conversations-tickets-retrieve", {"id": str(ticket_id)})
        scope = support_scope(self.config["support"])
        source = {
            "key": f"{scope}:{data['id']}",
            "scope": scope,
            "kind": "ticket",
            "remote_id": data["id"],
            "url": data["_posthogUrl"],
        }
        facts = {
            key: data.get(key)
            for key in (
                "status",
                "priority",
                "message_count",
                "unread_team_count",
                "updated_at",
                "sla_due_at",
            )
        }
        assignee = data.get("assignee") or {}
        facts["assignee"] = {key: assignee.get(key) for key in ("id", "type")}
        facts.update(self.ticket_conversation(ticket_id))
        if discover and data["status"] not in ("new", "open", "pending", "on_hold"):
            return source["key"]
        # Keep customer bodies, names, emails and session context out of the database.
        title = f"Support ticket #{data['ticket_number']}"
        if self.store.observe(source, title, facts, kind="support") is not None:
            self.refreshed[source["key"]] = {"title": title, "facts": facts}
        return source["key"]

    def support(self):
        support = self.config["support"]
        view = self.posthog("conversations-views-retrieve", {"short_id": support["view"]})
        assignee = view["filters"]["assignee"]
        if view["name"] != support["view_name"] or assignee.get("type") != "role":
            raise SyncError("Support view name or assignment role changed; verify configuration")
        params = {
            "assignee": f"role:{assignee['id']}",
            "date_from": "all",
            "status": "new,open,pending,on_hold",
            "limit": 100,
        }
        seen = set()
        tracked = {
            str(source["remote_id"]) for source in self.store.sources(scope=support_scope(support))
        }
        for offset in range(0, 100000, 100):
            data = self.posthog("conversations-tickets-list", {**params, "offset": offset})
            if not isinstance(data.get("results"), list):
                raise SyncError("Unexpected support ticket-list response")
            for item in data["results"]:
                seen.add(self.ticket(item["id"], discover=str(item["id"]) not in tracked))
            if not data.get("next"):
                break
            if not data["results"]:
                raise SyncError("Support pagination did not advance")
        else:
            raise SyncError("Support page limit reached; discovery is incomplete")
        for source in self.store.sources(scope=support_scope(support)):
            if source["key"] not in seen:
                try:
                    self.ticket(source["remote_id"])
                except (SyncError, KeyError, TypeError) as error:
                    self.store.source_error(source["key"], str(error))
                    raise

    def sync_card(self, task_id, progress=lambda message: None):
        """Refresh linked sources without discovery or advancing scope checkpoints."""
        results = {}
        failed_scopes = {}
        with sync_lock(self.store.path):
            snapshots = self.completion_snapshots()
            for source in self.store.get(task_id)["sources"]:
                progress(f"Refreshing {source['url']}…")
                error = failed_scopes.get(source["scope"])
                if error is None:
                    try:
                        if source["scope"].startswith("github:"):
                            self.github_item(source)
                        elif self.config.get("support") and source["scope"] == support_scope(
                            self.config["support"]
                        ):
                            self.ticket(source["remote_id"])
                        else:
                            raise SyncError("Linked source does not match configured support scope")
                    except (SyncError, KeyError, TypeError, ValueError) as exc:
                        error = (
                            str(exc) if isinstance(exc, SyncError) else "Unexpected response schema"
                        )
                        failed_scopes[source["scope"]] = error
                if error:
                    self.store.source_error(source["key"], error)
                results[source["key"]] = error
            self.completed_card_ids = self.store.complete_merged_cards(self.refreshed, snapshots)
        return results

    def sync(self, only=None, progress=lambda message: None):
        results = {}
        with sync_lock(self.store.path):
            snapshots = self.completion_snapshots()
            self.window_start = self._window_start_override or rotation_start()
            self.since = self.window_start.astimezone(timezone.utc).isoformat()
            repos = {repo.lower(): True for repo in self.config["repos"]}
            for source in self.store.sources():
                if source["scope"].startswith("github:"):
                    repos.setdefault(source["scope"].removeprefix("github:"), False)
            scopes = {f"github:{repo}": (repo, discover) for repo, discover in repos.items()}
            if self.config.get("support"):
                scopes[support_scope(self.config["support"])] = None
            if only and only not in scopes:
                raise ValueError(f"Unknown sync scope: {only}")
            for scope, args in scopes.items():
                if only and scope != only:
                    continue
                progress(f"Refreshing {scope}…")
                error = None
                try:
                    if args:
                        self.github(*args)
                    else:
                        self.support()
                except (SyncError, KeyError, TypeError, ValueError) as exc:
                    error = str(exc) if isinstance(exc, SyncError) else "Unexpected response schema"
                self.store.sync_result(scope, error)
                results[scope] = error
            self.completed_card_ids = self.store.complete_merged_cards(self.refreshed, snapshots)
        return results
