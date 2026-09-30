# SDK support hero

A local, mouse-friendly terminal kanban. The TUI and agent CLI share SQLite on the same machine. External services are read-only: moving a card never changes a ticket, issue, PR, or workflow.

## Run

Requires Python 3.11+, `uv`, and existing `gh` / `posthog-cli` authentication for refresh.

```sh
uv tool install --editable .
sdk-hero init --repo owner/repository
sdk-hero
```

Run directly in a Herdr terminal, or use `uv run sdk-hero` without installing. External-source refresh is explicit; there is no automatic import when opening the board. Inside Herdr, linked Pi session statuses are polled locally every three seconds. `init` refuses to overwrite existing configuration.

## Board

- **Click an unselected card** to select it. The selected card has a highlighted background and accent-colored left edge.
- **Click the selected card** to open its title, description, sources, and updates.
- **Click another column's heading, border, or empty space** to move the selected card there. Clicking another card selects that card instead and moves neither card. Buttons and scrollbars keep their own actions. Cards follow the work-order policy within each column.
- **Click P0–P3** on a card to choose its priority (P0 highest).
- **Delete** in card details opens a confirmation. It permanently removes the local card, links, and update history—not the external issue or ticket. Its sources stay excluded from automatic intake, including future rotations. Explicitly linking a source to a new card allows it to refresh again.
- **+ Add card** in a column requires only a title. Enter adds it.
- **Search** is always visible; **Filters** reveals SDK, responsibility and priority filters.
- **Refresh** on the board reads configured external sources in the background. **Refresh** in card details updates only that card's linked sources, preserves unsaved edits, and imports no other items. The details button is disabled when there are no linked sources or a refresh is already running.

The card description is the current plan: what needs to happen, who needs to do it, and any blocker. Edit it inline in the card's details and click **Save** (or Ctrl+S). Sources and updates can refresh while you type without overwriting your draft. If the card changes while its editable fields are untouched, the panel adopts the new values automatically, including an automatic move to Done. Unsaved field edits retain conflict protection; unposted update text is preserved. Close/Escape leaves saved changes intact and cancels any unsaved edits.

The **Updates** section records creation, description/title/label edits, column/priority changes, manual updates, links, and meaningful source changes. Enter text and click **Post** to append a local update; this does not post to GitHub or PostHog. Newest entries appear first in the TUI. The full history is retained, with before/after values, and exported in chronological order. New refresh entries display plain-language summaries, including CI result counts instead of full check lists. Full before/after data remains available through `sdk-hero history ID`. Identical refreshes, timestamp-only changes, unread counts, and reordered checks do not create update noise. A changed reply count is recorded as a count change, not as a claim about who replied or what they said.

External changes preserve descriptions and priorities. On refresh, a **new human external reply still awaiting a team response** returns a **Done or non-delegated Waiting** card to **Inbox**. If that reply comes from a source other than the manually selected SLA source, the selection resets to **Automatic**. Updates records the triggering source and changes. Our replies, bots, private support notes, metadata-only changes, and replies already answered by the team do not reopen cards. The first source observation establishes a baseline rather than reopening historical work. Ready and In progress cards stay where they are.

Use **Waiting** for an outstanding obligation blocked on a reply, decision, or dependency. When another person or team is doing the work, enter their name in **Delegated to** and **Save**. You retain responsibility for monitoring progress. Delegated Waiting cards show **Action with …**; replies update their history and SLAs without moving them to Inbox or resetting their SLA source. Leave the field blank for ordinary waiting-for-a-reply behavior. **Take back** immediately clears delegation and moves the card to Inbox, preserving other unsaved edits. Leaving Waiting, including automatic completion, clears delegation so future work can reopen normally. Use **Done** when nothing further is currently owed, even if the external discussion remains open. Reopening requires a source refresh; startup and local countdown updates do not fetch replies. After a successful refresh of all its linked sources, a PR-only card moves to **Done** once every PR is merged. If it also links issues, all those issues must be closed. Open or unknown issue states, unmerged PRs, failed or incomplete refreshes, and support/workflow links prevent automatic completion. Unanswered human replies posted after merge/closure keep the card in the queue until answered, including on repeated refreshes. Automatic completion records history and preserves titles, descriptions, priorities, and the SLA selection. Local edits made during refresh are not overwritten.

### Pi session status

Cards with a live linked Pi session show **Working**, **Done · unread**, **Idle · viewed**, or **Needs input**, using Herdr's agent status. Viewing the completed session's tab normally clears its unread state. Each live session gets a badge; closed or replaced sessions disappear on the next successful check. **Open Pi** in card details jumps to the card's most recently launched live session, including after the agent is renamed or moved. It does not start a new session. A failed query shows **Status unavailable**, rather than retaining a stale status.

Status checks match the recorded session path in the same Herdr server, so renaming or moving a live agent does not break the association. Checks do not focus sessions, mark them viewed, change card columns, or append Updates.

### Keyboard

| Key | Action |
| --- | --- |
| h/l or ←/→ | Select column; scrolls horizontally on narrow terminals |
| j/k or ↓/↑ | Select card |
| Enter / e / Shift+E | Open selected card |
| Shift+H/L or Shift+←/→ | Move card one column |
| p | Choose priority |
| n | Add a title-only card in current column |
| Ctrl+S | Save card details |
| / | Focus search |
| f | Toggle filters |
| r | Refresh sources |
| Escape | Close dialog / return to board |
| q | Quit |

Mouse selection and column clicks work within the visible terminal board. For a distant column on a narrow terminal, use the card's column selector or keyboard move controls. Mouse support must be enabled in the terminal; keyboard navigation remains available.

## Response targets and work order

Card badges show business time remaining (`8h`, `30m`) or overdue (`-2h`). Green is more than two hours remaining, amber is two hours or less, and red is overdue. Hover for the deadline and target. Time counts only Monday–Friday, 9 a.m.–5 p.m. in `America/New_York`, including daylight-saving changes; public holidays are not excluded.

- GitHub issues: **Reply · 4h** from the earliest unanswered external message. After a human team reply, an open issue switches to **Follow up · 16h**: deliver a fix, give a progress update, or chase the responsible team. Another team reply restarts that follow-up clock; a new external reply switches back to the 4-hour response clock. Further external messages and bots do not reset an outstanding response clock. Closing the issue clears its target. Follow-up reminders remain visible in Waiting and sort with other follow-ups, after unanswered issues. Internally opened issues without an external conversation do not acquire a follow-up clock.
- Contributor PRs: **8 business hours** for an initial review once ready, then **4 business hours** for contributor updates/comments after a human review. Acknowledgments do not satisfy the initial review. Draft, closed, and merged PRs have no active review target. Bot reviews do not satisfy targets.
- Support: surface the existing `sla_due_at`, never replace it with an SDK-defined deadline.

**SLA source** in card details controls the badge. **Automatic** uses the earliest active PostHog support deadline; otherwise it uses the most recently started active GitHub SLA. Select any linked source to override Automatic, then **Save**. An explicit selection persists across ordinary refreshes and overrides support precedence; reopening a Done/non-delegated Waiting card because of a reply on another source resets it to Automatic. Delegated Waiting cards retain their selection. If that source has no active SLA or is no longer linked, the badge clears without falling back. Choose Automatic to restore default selection. Hover over a badge to see its source URL. Each entry under **Linked sources** shows its type and its own SLA countdown, or **No active SLA**, regardless of the card's selected source. Hover over an individual SLA for its target and deadline. These countdowns update locally without writing history. The selected SLA deadline also controls the card's within-column SLA ordering.

**Resolve SLA** beside a linked source immediately marks that source's SLA locally resolved, without saving or discarding other draft edits. **Reopen SLA** undoes it. Resolution survives restarts, unchanged refreshes, timestamp/read-count changes, and refresh failures. The next meaningful source change recorded in Updates—such as a reply, review, commit, status/check change, or title edit—clears the resolution. Reactivation uses the source's existing clock rules rather than resetting its waiting time. Automatic may use another linked source while one is resolved; an explicitly selected source does not fall back. These controls never modify the external ticket, issue, or PR.

PR head changes use `updatedAt` as an approximate update time, because it can include unrelated later activity. That time is retained for an unchanged head, and new pushes or comments do not reset an outstanding clock. Comments and reviews use their actual timestamps. These are attention indicators, not semantic proof that a response resolved a conversation. Refresh is needed to obtain new conversation facts; the local countdown updates without external calls or Updates entries. Older GitHub observations gain their clocks on the next refresh. Done cards hide badges.

Finish actionable **In progress** work before taking more from **Ready**. Each column sorts by **priority (P0 → P3), then SLA deadline (earliest first)**. Within a priority, the most overdue deadlines come first, followed by upcoming deadlines, then cards without an SLA. The card's SLA source selection determines its deadline. Ties retain category order (support, contributor reviews, unanswered issues, other follow-ups), then oldest outstanding request and card ID. Sorting never moves cards between columns or changes local priorities. If work is blocked, triage can move it to Waiting with the dependency explained.

## Agent CLI

Global flags precede the subcommand. Commands return JSON except `export` (Markdown) and `tui`.

```sh
sdk-hero list
sdk-hero add 'Investigate flag evaluation' --sdk python --kind support --priority 1 \
  --description 'Reproduce against the reported SDK version, then reply.'
sdk-hero show 1
sdk-hero update 1 --status waiting --description 'Waiting for customer diagnostics.' --if-revision 1
sdk-hero note 1 'Customer supplied diagnostics; source thread reviewed.'
sdk-hero history 1
sdk-hero link 1 https://github.com/PostHog/posthog-python/issues/123
sdk-hero link 1 https://support.example.com/project/4242/support/tickets/example-ticket
sdk-hero sync
sdk-hero status
sdk-hero export > handoff.md
```

Use the revision from `show` for read-modify-write operations. Stale revisions are rejected. Without `--if-revision`, only named fields are patched; competing writes to the same field are last-writer-wins. No-op patches do not bump revision or create history. Appending a note does not invalidate an in-progress description edit.

A source belongs to one card. To consolidate related cards, use its source key:

```sh
sdk-hero source-move 'github:posthog/posthog-python:item:123' 1
sdk-hero update 2 --status done --description 'Consolidated into card #1.'
```

Both cards retain their existing histories and receive a source-move entry. Future source updates appear on the destination card. Source associations are managed through the CLI; card details show the links.

To delete from the CLI, inspect `show` first, then confirm with its current revision:

```sh
sdk-hero delete 12 --if-revision 3 --yes
```

Deletion cannot be undone through the app. Deleted card IDs and suppressed source keys remain as bookkeeping to prevent stale edits and re-imports; card content and history are removed from the active database. Existing backups are unchanged.

## Storage and migration

- Database: `~/.local/share/sdk-support-hero/board.db`
- Configuration: `~/.config/sdk-support-hero/config.yml`. YAML supports comments; existing JSON configurations remain readable. Without an override, `config.json` is used only when `config.yml` does not exist.
- Overrides: `--db PATH`, `--config PATH`, `SDK_HERO_DB`, `SDK_HERO_CONFIG`, or XDG directories.

SQLite uses WAL, short transactions, and a busy timeout. Task mutations and history entries commit atomically; concurrent identical source observations produce one change entry. Only one refresh runs per database, using an OS file lock. Use a local disk, not a network share.

The schema-v2 migration combines previous owner, next-action, blocker, and notes text into one description, preserving labels and line breaks. It records a migration entry rather than inventing historical activity. Previous columns remain on disk for preservation but are not part of the current CLI. Use `--description` and `note` instead of the old field flags. Restart any running TUI after upgrading.

Schema v3 adds deletion bookkeeping without changing existing cards. Restart running TUIs after upgrading, before using deletion from another window or the CLI.

For backups use SQLite's backup command/API, or stop all writers before copying; copying only `board.db` while WAL is active can omit recent changes. Updates contain historical descriptions, so removing text from the current description does not remove it from history. Do not put secrets or unnecessary customer data in either descriptions or updates. Sync does not store ticket messages, names/emails, session context, or raw CLI errors.

## Refresh coverage

Use repeated `--repo owner/name` arguments to configure repositories. `init --inventory PATH` can also read a Markdown file containing GitHub repository links between `<!-- posthog-sdk-inventory:start -->` and `<!-- posthog-sdk-inventory:end -->` markers. Support intake requires an explicitly configured host, project, view ID, and expected view name; see [setup](../README.md#optional-posthog-support-intake). Configuration determines discovery scope; use `status` to inspect it.

The intake window starts at the preceding **Saturday, local midnight**. Monday intake therefore includes outstanding weekend conversations without requiring weekend work. Each refresh computes the current window; existing cards remain across rotations.

- **GitHub intake:** open issues with an external opening message or reply since Saturday, with no later human team reply. Reads all comment pages. OWNER/MEMBER/COLLABORATOR associations approximate team identity; bot messages are ignored. An issue's age or updated timestamp alone does not qualify it. Open, non-draft human external contributor PRs updated since Saturday also enter Inbox at P2. OWNER/MEMBER/COLLABORATOR authors and bots are excluded from PR intake; explicitly linked PRs still refresh. PR activity selects candidates, not review responsibility. Review clocks use source history, not import time. Existing cards and dismissed sources are preserved.
- **Support intake:** verifies the configured view name and its role-based assignee filter and scans recently updated unresolved tickets (including pending and snoozed). Reads the full paginated thread and applies the same unanswered-since-Saturday rule. Private notes and AI messages do not count as team replies. Titles start as `Support ticket #123` to avoid copying customer messages.
- **Existing cards:** refresh regardless of age, closure, or queue membership. Tracked PR facts include head, draft state, reviews, checks, and human conversation timing. New unanswered external replies reopen Done/Waiting cards as described above; descriptions and priorities remain unchanged. Merged PRs can automatically complete a card once every linked PR is merged and every linked issue is verified closed, subject to the completion rules above.
- **Workflow runs:** explicitly linked `https://github.com/OWNER/REPO/actions/runs/ID` URLs refresh status, conclusion, attempt and head SHA.
- **Product-repo bumps:** link selected `PostHog/posthog` PRs. Linked items in an otherwise unconfigured repo refresh individually, without discovering its whole backlog.

Manual triage can add justified exceptions: unresolved handoff work, promised follow-ups despite an acknowledgment, customer blockers, selected PRs, and release work. Record the evidence and why the item needs attention now. A refreshed board is not a complete backlog audit; response ownership and priority decisions still need human review. Source timestamps and failures appear in card details; `status` has full scope diagnostics. Failures preserve previous observations and successful checkpoints; partial successes remain visible but do not advance the failed scope's checkpoint. Missing items are not deleted or marked Done.

```sh
sdk-hero sync --scope github:posthog/posthog-python
sdk-hero sync --scope posthog:https://support.example.com:4242
```

## Development

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Tests cover card selection, click-to-move safety, keyboard use, small terminals, concurrent edits/observations, migration, history, pagination, partial failures, and privacy. Tests use synthetic data and fake external responses.
