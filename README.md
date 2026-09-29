# SDK Hero

A local terminal kanban for SDK support, issues, and contributor pull requests. A mouse-friendly [Textual](https://textual.textualize.io/) interface and a JSON CLI share the same SQLite board.

- Track work across Inbox, Ready, In progress, Waiting, and Done.
- Link GitHub issues, PRs, workflow runs, and optionally PostHog support tickets.
- Refresh source facts without losing local descriptions, priorities, or drafts.
- See business-hour response targets, delegate work, and keep an audit history.
- Launch persistent Pi sessions in [Herdr](https://herdr.dev/), see their status, and jump back to them.

Source refresh is read-only: it never posts comments, merges PRs, or changes tickets. Tasks you explicitly give a launched Pi agent are separate from source refresh.

## Install and run

Requires **Python 3.11+**, [uv](https://docs.astral.sh/uv/), and a macOS or Linux terminal. GitHub refresh needs an authenticated [GitHub CLI](https://cli.github.com/). You can use the local board without external credentials.

```sh
git clone https://github.com/dustinbyrne/sdk-hero.git
cd sdk-hero
uv tool install --editable .
sdk-hero init --repo owner/repository
sdk-hero
```

Replace `owner/repository` with a repository you want to track; repeat `--repo` for multiple repositories. Use `sdk-hero init` without repositories for a local-only board. Initialization makes no external requests and refuses to overwrite existing configuration.

Click a card to select it, then click it again to open its details. Click an empty area or heading in another column to move the selected card. Keyboard navigation is also available. **Refresh** explicitly fetches external updates; opening the board does not import a backlog.

See the [usage guide](docs/usage.md) for keyboard shortcuts, source intake, SLA rules, delegation, and lifecycle behavior.

## Optional PostHog support intake

Requires an authenticated `posthog-cli` with access to your project's support tickets. Configure your own host, project ID, support view ID, and the view's exact name:

```sh
sdk-hero init --repo owner/repository --support \
  --support-host https://support.example.com \
  --support-project 4242 \
  --support-view example-view \
  --support-view-name 'Example queue'
```

These are placeholders. Use your PostHog instance's HTTPS origin and actual project/view settings. The selected view must have a role-based assignee filter. Refresh verifies its name and filter type before discovery.

For an existing installation, edit the local configuration instead of rerunning `init`:

```json
{
  "repos": ["owner/repository"],
  "support": {
    "host": "https://support.example.com",
    "project": 4242,
    "view": "example-view",
    "view_name": "Example queue"
  }
}
```

Set `support` to `null` to disable support intake. Credentials remain managed by the external CLIs, not in this configuration.

## Herdr and Pi

Run the board inside Herdr with `herdr` and `pi` on your PATH. In card details, **Investigate** lets you choose a workspace and give Pi a task. It opens a background tab named after the card number and records a persistent session reference in Updates.

Cards show **Working**, **Done · unread**, **Idle · viewed**, or **Needs input**. Status is checked locally every three seconds. **Open Pi** appears in card details when a linked session is live. **Investigate** is hidden outside Herdr.

The task uses saved card data and preserves unsaved edits. A failed launch records available session information; inspect it before retrying because a timeout does not prove that nothing started. Tested with Herdr 0.9.0.

## CLI

```sh
sdk-hero add 'Review contributor fix' --priority 1
sdk-hero list
sdk-hero show 1 --brief
sdk-hero history 1
sdk-hero note 1 'Waiting for CI.'
sdk-hero export > handoff.md
```

Use `sdk-hero --help` for commands and `sdk-hero COMMAND --help` for options. The full `show` command includes linked facts and history; `--brief` provides a lightweight card view.

## Local data

- Configuration: `~/.config/sdk-support-hero/config.json`
- Database: `~/.local/share/sdk-support-hero/board.db`
- Pi launch artifacts: `investigations/` beside the database
- Overrides: `--config`, `--db`, `SDK_HERO_CONFIG`, `SDK_HERO_DB`, or XDG directories

Use a local filesystem: refresh locking uses POSIX file locks. Back up with SQLite's backup API rather than copying an active WAL database. History and Pi sessions can contain sensitive information; keep local data and credentials out of version control. Restart the TUI after updating the installed code.

## Development

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Tests use temporary databases and fake external tools, including Herdr. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
