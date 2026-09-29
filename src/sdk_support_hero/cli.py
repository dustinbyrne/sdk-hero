from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .store import FIELDS, KINDS, STATUSES, Store, default_db, now
from .sync import Syncer, config_path, inventory_repos, load_config, parse_source, validate_config


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))


def export_markdown(store):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        f"# SDK support hero · {now()}",
        "",
        "## Local work queue",
        "",
        "| Task | Status | Priority | SDK / responsibility |",
        "| --- | --- | --- | --- |",
    ]
    tasks = store.tasks()
    for task in tasks:
        lines.append(
            "| "
            + " | ".join(
                cell(v)
                for v in (
                    f"#{task['id']} {task['title']}",
                    task["status"],
                    f"P{task['priority']}",
                    f"{task['sdk']} / {task['kind']}",
                )
            )
            + " |"
        )
    lines.extend(["", "## Cards", ""])
    for task in tasks:
        lines.extend([f"### Task #{task['id']}", ""])
        if task["delegated_to"]:
            lines.extend([f"**Delegated to:** {cell(task['delegated_to'])}", ""])
        lines.extend(["#### Description", ""])
        lines.extend("> " + line for line in task["description"].splitlines())
        lines.extend(["", "#### Updates", ""])
        for entry in store.history(task["id"]):
            lines.append(f"- {entry['created_at']} · {entry['actor']} · {cell(entry['summary'])}")
            if entry["details"]:
                lines.append(f"  - {json.dumps(entry['details'], ensure_ascii=False)}")
        lines.append("")
    lines.extend(["", "## Source evidence", ""])
    for source in store.sources():
        lines.append(
            f"- Task #{source['task_id']}: {source['url']} — checked "
            f"{source['observed_at'] or 'never'}; error: {source['error'] or 'none'}"
        )
        lines.append(f"  - Facts: {json.dumps(source['facts'], ensure_ascii=False)}")
    lines.extend(["", "## Sync coverage", ""])
    lines.extend(
        f"- {s['scope']}: last success {s['succeeded_at'] or 'never'}; "
        f"latest attempt {s['attempted_at']}; error: {s['error'] or 'none'}"
        for s in store.sync_states()
    )
    lines.append("\nSource sync is not a review or release-propagation audit.")
    return "\n".join(lines)


def parser():
    root = argparse.ArgumentParser(
        description="Local support-hero kanban; external services read-only"
    )
    root.add_argument("--db", type=Path, default=default_db())
    root.add_argument("--config", type=Path, default=config_path())
    commands = root.add_subparsers(dest="command")
    commands.add_parser("tui", help="Open board (default)")
    init = commands.add_parser("init", help="Create config; does not contact external services")
    init.add_argument("--repo", action="append", default=[])
    init.add_argument("--inventory", type=Path, help="Read canonical SDK repos from AGENTS.md")
    init.add_argument("--support", action="store_true", help="Enable configured support intake")
    init.add_argument("--support-host", help="PostHog HTTPS origin")
    init.add_argument("--support-project", type=int, help="PostHog project ID")
    init.add_argument("--support-view", help="Support view ID")
    init.add_argument("--support-view-name", help="Expected support view name")
    commands.add_parser("list", help="List local cards as JSON")
    show = commands.add_parser("show", help="Show one card and source facts as JSON")
    show.add_argument("id", type=int)
    show.add_argument(
        "--brief",
        action="store_true",
        help="Card fields and linked references, without source facts or updates",
    )
    note = commands.add_parser(
        "note", help="Append a local update without changing the description"
    )
    note.add_argument("id", type=int)
    note.add_argument("text")
    history = commands.add_parser("history", help="Read the chronological card update history")
    history.add_argument("id", type=int)
    add = commands.add_parser("add", help="Create a local card")
    add.add_argument("title")
    update = commands.add_parser("update", help="Patch local fields only")
    update.add_argument("id", type=int)
    update.add_argument("--if-revision", type=int)
    delete = commands.add_parser(
        "delete", help="Delete a local card and suppress its sources from intake"
    )
    delete.add_argument("id", type=int)
    delete.add_argument("--yes", action="store_true", help="Confirm permanent local deletion")
    delete.add_argument("--if-revision", type=int, required=True)
    for sub in (add, update):
        if sub == update:
            sub.add_argument("--title")
        sub.add_argument("--status", choices=STATUSES)
        sub.add_argument("--priority", type=int, choices=range(4))
        sub.add_argument("--kind", choices=KINDS)
        for field in ("sdk", "description", "delegated_to"):
            sub.add_argument("--" + field.replace("_", "-"))
    link = commands.add_parser(
        "link", help="Attach an external URL to a card for read-only refresh"
    )
    link.add_argument("id", type=int)
    link.add_argument("url")
    move = commands.add_parser("source-move", help="Relink a source to another local card")
    move.add_argument("key")
    move.add_argument("id", type=int)
    sync = commands.add_parser("sync", help="Discover open items and refresh tracked evidence")
    sync.add_argument("--scope", help="Only this scope (see status)")
    commands.add_parser("status", help="Show configuration and sync coverage")
    commands.add_parser("export", help="Print a Markdown handoff/report")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            repos = args.repo + (inventory_repos(args.inventory) if args.inventory else [])
            support = {
                "host": args.support_host,
                "project": args.support_project,
                "view": args.support_view,
                "view_name": args.support_view_name,
            }
            if not args.support and any(value is not None for value in support.values()):
                raise ValueError("Support options require --support")
            config = {"repos": sorted(set(repos)), "support": support if args.support else None}
            validate_config(config)
            args.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with args.config.open("x") as file:
                json.dump(config, file, indent=2)
                file.write("\n")
            emit(
                {"config": str(args.config), "message": "Configured. Run sdk-hero sync or press r."}
            )
            return 0
        store = Store(args.db)
        config = load_config(args.config)
        if args.command in (None, "tui"):
            from .app import Board

            Board(store, config, config_file=args.config).run()
        elif args.command == "list":
            emit(store.tasks())
        elif args.command == "show":
            emit(store.get(args.id, brief=args.brief))
        elif args.command == "note":
            store.add_note(args.id, args.text)
            emit(store.get(args.id))
        elif args.command == "history":
            emit(store.get(args.id)["updates"])
        elif args.command in ("add", "update"):
            fields = {k: getattr(args, k) for k in FIELDS if getattr(args, k, None) is not None}
            if args.command == "add":
                task_id = store.create(**fields)
            else:
                task_id = args.id
                store.update(task_id, expected_revision=args.if_revision, **fields)
            emit(store.get(task_id))
        elif args.command == "delete":
            if not args.yes:
                raise ValueError("Deletion requires --yes; use show to inspect the card first")
            store.delete(args.id, expected_revision=args.if_revision)
            emit({"deleted": args.id})
        elif args.command == "link":
            source = parse_source(args.url, config)
            if source["kind"] == "ticket":
                # Resolve numeric/UUID aliases before assigning a unique local source key.
                data = Syncer(store, config).posthog(
                    "conversations-tickets-retrieve", {"id": source["remote_id"]}
                )
                source["remote_id"] = data["id"]
                source["key"] = f"{source['scope']}:{data['id']}"
                source["url"] = data["_posthogUrl"]
            store.link(args.id, source)
            emit(store.get(args.id))
        elif args.command == "source-move":
            store.move_source(args.key, args.id)
            emit(store.get(args.id))
        elif args.command == "sync":
            result = Syncer(store, config).sync(
                only=args.scope, progress=lambda message: print(message, file=sys.stderr)
            )
            emit(result)
            return int(any(result.values()))
        elif args.command == "status":
            emit({"db": str(store.path), "config": config, "sync": store.sync_states()})
        elif args.command == "export":
            print(export_markdown(store))
    except (ValueError, OSError, KeyError) as error:
        print(f"sdk-hero: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
