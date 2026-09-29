import pytest
from textual.widgets import TextArea

from sdk_support_hero.app import Board, SourceSLAButton
from sdk_support_hero.sla import card_schedule
from sdk_support_hero.store import Conflict, Store
from sdk_support_hero.sync import parse_source

FACTS = {
    "state": "open",
    "needs_team_reply": True,
    "awaiting_team_since": "2026-09-28T13:00:00Z",
    "comments": 1,
}


def setup_source(tmp_path):
    store = Store(tmp_path / "board.db")
    task = store.create("Card", description="Saved plan")
    source = parse_source("https://github.com/org/sdk/issues/1", {})
    store.link(task, source)
    store.observe(source, "Issue", FACTS)
    store.update(task, sla_source=source["key"])
    return store, task, source


def snapshot(store, task):
    return store.sources(task_id=task)[0]


def test_resolve_is_durable_local_and_reversible(tmp_path):
    store, task, source = setup_source(tmp_path)
    before = store.get(task)
    assert card_schedule(before, before["sources"])["badge"]
    store.set_sla_resolved(task, snapshot(store, task), True)
    reopened_store = Store(store.path)
    after = reopened_store.get(task)
    assert after["sources"][0]["sla_resolved"]
    assert after["sources"][0]["facts"] == before["sources"][0]["facts"]
    assert {k: v for k, v in after.items() if k not in ("sources", "updates")} == {
        k: v for k, v in before.items() if k not in ("sources", "updates")
    }
    assert not card_schedule(after, after["sources"])["badge"]
    assert card_schedule(after, after["sources"])["due_at"] is None
    assert store.history(task)[-1]["kind"] == "sla_resolved"
    history = store.history(task)
    store.set_sla_resolved(task, snapshot(store, task), True)
    assert store.history(task) == history
    store.set_sla_resolved(task, snapshot(store, task), False)
    assert card_schedule(store.get(task), store.sources(task_id=task))["badge"]
    assert store.history(task)[-1]["kind"] == "sla_reopened"


def test_refresh_noise_and_error_recovery_do_not_reopen_sla(tmp_path):
    store, task, source = setup_source(tmp_path)
    facts = {**FACTS, "checks": [{"name": "a"}, {"name": "b"}]}
    store.observe(source, "Issue", facts)
    store.set_sla_resolved(task, snapshot(store, task), True)
    before = store.history(task)
    store.observe(source, "Issue", facts)
    store.observe(
        source,
        "Issue",
        {
            **facts,
            "updated_at": "2026-09-29T13:00:00Z",
            "updatedAt": "2026-09-29T13:00:00Z",
            "unread_team_count": 4,
            "classification": "changed",
            "checks": list(reversed(facts["checks"])),
        },
    )
    assert snapshot(store, task)["sla_resolved"]
    assert store.history(task) == before
    store.source_error(source["key"], "Unavailable")
    store.observe(source, "Issue", facts)
    assert snapshot(store, task)["sla_resolved"]
    assert not any(u["kind"] == "sla_reopened" for u in store.history(task))


@pytest.mark.parametrize(
    "patch",
    [
        {"comments": 2},
        {"last_external_reply_at": "2026-09-29T13:00:00Z"},
        {"state": "closed"},
        {"title": "Updated title"},
    ],
)
def test_meaningful_source_update_clears_resolution_once(tmp_path, patch):
    store, task, source = setup_source(tmp_path)
    store.set_sla_resolved(task, snapshot(store, task), True)
    title = patch.get("title", "Issue")
    facts = {**FACTS, **{k: v for k, v in patch.items() if k != "title"}}
    store.observe(source, title, facts)
    assert not snapshot(store, task)["sla_resolved"]
    history = store.history(task)
    assert len([u for u in history if u["kind"] == "sla_reopened"]) == 1
    assert history[-1]["actor"] == "sync"
    store.observe(source, title, facts)
    assert store.history(task) == history


def test_source_choice_and_automatic_fallback_are_preserved(tmp_path):
    store, task, source = setup_source(tmp_path)
    pr = parse_source("https://github.com/org/sdk/pull/2", {})
    store.link(task, pr)
    store.observe(
        pr,
        "PR",
        {
            "review_waiting_since": "2026-09-28T14:00:00Z",
            "review_target_hours": 4,
        },
    )
    store.set_sla_resolved(task, snapshot(store, task), True)
    assert not card_schedule(store.get(task), store.sources(task_id=task))["badge"]
    store.update(task, sla_source="")
    assert "PR follow-up" in card_schedule(store.get(task), store.sources(task_id=task))["tooltip"]
    assert store.sources(task_id=task)[1]["sla_resolved"] == 0


@pytest.mark.parametrize("change", ["facts", "title", "resolved", "moved", "deleted"])
def test_stale_source_action_is_rejected(tmp_path, change):
    store, task, source = setup_source(tmp_path)
    stale = snapshot(store, task)
    if change == "facts":
        store.observe(source, "Issue", {**FACTS, "comments": 2})
    elif change == "title":
        store.observe(source, "New title", FACTS)
    elif change == "resolved":
        store.set_sla_resolved(task, stale, True)
    elif change == "moved":
        store.move_source(source["key"], store.create("Other"))
    else:
        store.delete(task, expected_revision=store.get(task)["revision"])
    with pytest.raises(Conflict, match="Source changed or moved"):
        store.set_sla_resolved(task, stale, True)


def test_version_four_migration_preserves_sources_and_history(tmp_path):
    store, task, source = setup_source(tmp_path)
    before = store.get(task)
    with store.connect() as db:
        db.execute("ALTER TABLE sources DROP COLUMN sla_resolved")
        db.execute("PRAGMA user_version=4")
    migrated = Store(store.path)
    assert migrated.get(task) == before
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


async def test_details_resolve_reopen_and_source_update_preserve_drafts(tmp_path):
    store, task, source = setup_source(tmp_path)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("e")
        app.screen.query_one(TextArea).load_text("Unsaved plan")
        button = app.screen.query_one(SourceSLAButton)
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        assert not button.disabled
        await pilot.click(button)
        assert snapshot(store, task)["sla_resolved"]
        assert "Resolved until source update" in str(app.screen.query_one(".source-sla").render())
        assert str(app.screen.query_one(SourceSLAButton).label) == "Reopen SLA"
        assert app.screen.query_one(TextArea).text == "Unsaved plan"
        assert store.get(task)["description"] == "Saved plan"
        await pilot.click(app.screen.query_one(SourceSLAButton))
        assert not snapshot(store, task)["sla_resolved"]
        await pilot.click(app.screen.query_one(SourceSLAButton))
        assert snapshot(store, task)["sla_resolved"]
        store.observe(source, "Issue", {**FACTS, "comments": 2})
        await app.screen.refresh_evidence()
        assert not snapshot(store, task)["sla_resolved"]
        assert str(app.screen.query_one(SourceSLAButton).label) == "Resolve SLA"
        assert "Reply" in str(app.screen.query_one(".source-sla").render())
        assert app.screen.query_one(TextArea).text == "Unsaved plan"
        store.observe(source, "Issue", {"state": "closed"})
        await app.screen.refresh_evidence()
        assert app.screen.query_one(SourceSLAButton).disabled


async def test_deleted_card_disables_source_sla_actions(tmp_path):
    store, task, source = setup_source(tmp_path)
    app = Board(store, {"repos": []})
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.press("e")
        store.delete(task, expected_revision=store.get(task)["revision"])
        await app.screen.refresh_evidence()
        assert app.screen.query_one(SourceSLAButton).disabled
