from sdk_support_hero.app import format_update
from sdk_support_hero.store import Store, meaningful_facts
from sdk_support_hero.sync import parse_source
from sdk_support_hero.updates import check_summary, source_change_summary


def test_refresh_records_readable_summary_and_keeps_full_audit(tmp_path):
    store = Store(tmp_path / "board.db")
    source = parse_source("https://github.com/org/sdk/pull/610", {})
    facts = {
        "state": "OPEN",
        "reviewDecision": "REVIEW_REQUIRED",
        "review_target_hours": 4,
        "checks": [{"name": "Old check", "conclusion": "SUCCESS"}],
    }
    task = store.observe(source, "Fix surveys", facts)
    original = store.history(task)
    current = {
        **facts,
        "reviewDecision": "APPROVED",
        "review_target_hours": None,
        "checks": (
            [{"name": f"Passing {i}", "conclusion": "SUCCESS"} for i in range(17)]
            + [{"name": f"Running {i}", "status": "IN_PROGRESS"} for i in range(9)]
            + [{"name": "CodeQL", "conclusion": "NEUTRAL"}]
        ),
    }
    store.observe(source, "Fix surveys", current)
    entry = store.history(task)[-1]
    assert (
        entry["summary"]
        == "PR #610: Approved; CI: 17 passed, 9 running, 1 neutral; Review SLA cleared."
    )
    assert entry["details"]["changes"]["checks"] == {
        "before": facts["checks"],
        "after": meaningful_facts(current)["checks"],
    }
    rendered = format_update(entry)
    assert entry["summary"] in rendered and source["url"] in rendered
    assert "Passing 0" not in rendered and "review_target_hours" not in rendered
    assert store.history(task)[:-1] == original
    store.observe(source, "Fix surveys", current)
    assert store.history(task)[-1] == entry


def test_ci_summary_does_not_treat_incomplete_or_unknown_results_as_passes():
    checks = [
        {"conclusion": value}
        for value in ("FAILURE", "TIMED_OUT", "CANCELLED", "SKIPPED", "NEUTRAL", "FUTURE_RESULT")
    ]
    checks += [{"status": "QUEUED"}, {"state": "PENDING"}, {"status": "COMPLETED"}]
    assert (
        check_summary(checks)
        == "CI: 1 failed, 1 timed out, 1 cancelled, 2 pending, 1 neutral, 1 skipped, 2 unknown"
    )
    assert check_summary([]) == "CI: no checks reported"


def test_plain_source_changes_are_concise():
    source = {"kind": "issue", "remote_id": "607"}
    summary = source_change_summary(
        source,
        {
            "state": {"before": "open", "after": "closed"},
            "labels": {"before": [], "after": ["bug"]},
            "needs_team_reply": {"before": True, "after": False},
        },
    )
    assert summary == "Issue #607: State: closed; No team reply pending; Labels updated."


def test_existing_entries_keep_their_original_rendering():
    entry = {
        "created_at": "2026-09-29",
        "actor": "sync",
        "kind": "source_changed",
        "summary": "Source changed: checks",
        "details": {"changes": {"checks": {"before": [], "after": [{"name": "Original check"}]}}},
    }
    assert 'checks: [] → [{"name": "Original check"}]' in format_update(entry)
