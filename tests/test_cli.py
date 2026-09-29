import json
import os
import subprocess
import sys

from sdk_support_hero.cli import export_markdown, main
from sdk_support_hero.store import Store


def test_cli_roundtrip(tmp_path, capsys):
    base = ["--db", str(tmp_path / "board.db"), "--config", str(tmp_path / "config.json")]
    assert (
        main(
            [
                *base,
                "init",
                "--repo",
                "PostHog/posthog-js",
                "--support",
                "--support-host",
                "https://support.example.com",
                "--support-project",
                "4242",
                "--support-view",
                "example-view",
                "--support-view-name",
                "Example queue",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "add", "Customer fix", "--priority", "0", "--kind", "support"]) == 0
    task = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                *base,
                "update",
                str(task["id"]),
                "--status",
                "waiting",
                "--description",
                "Wait for release",
                "--if-revision",
                "1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main([*base, "link", str(task["id"]), "https://github.com/PostHog/posthog-js/pull/42"]) == 0
    )
    linked = json.loads(capsys.readouterr().out)
    assert linked["sources"][0]["remote_id"] == "42"
    assert main([*base, "note", str(task["id"]), "Published; notify customer"]) == 0
    capsys.readouterr()
    assert main([*base, "history", str(task["id"])]) == 0
    assert json.loads(capsys.readouterr().out)[-1]["summary"] == "Published; notify customer"
    assert main([*base, "export"]) == 0
    report = capsys.readouterr().out
    assert "Wait for release" in report and "https://github.com/" in report
    assert main([*base, "init"]) == 1
    assert "File exists" in capsys.readouterr().err


def test_public_cli_focused_sync(tmp_path):
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
from datetime import datetime,timezone
from urllib.parse import parse_qs,urlparse
assert sys.argv[1:4] == ['api','--method','GET']
assert 'since' in parse_qs(urlparse(sys.argv[-1]).query)
print(json.dumps([{'number': n, 'title': f'Issue {n}', 'state': 'open', 'comments': 0,
 'created_at': date, 'author_association': 'NONE', 'user': {'login': 'external'}, 'labels': []}
 for n,date in [(1,datetime.now(timezone.utc).isoformat()),(2,'2000-01-01T00:00:00Z')]]))
"""
    )
    gh.chmod(0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"repos": ["org/sdk"]}))
    command = [
        sys.executable,
        "-c",
        "from sdk_support_hero.cli import main; raise SystemExit(main())",
        "--db",
        str(tmp_path / "board.db"),
        "--config",
        str(config),
    ]
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([*command, "sync"], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    cards = json.loads(subprocess.check_output([*command, "list"], env=env, text=True))
    assert [card["title"] for card in cards] == ["Issue 1"]


def test_export_preserves_multiline_promises_and_evidence(tmp_path):
    store = Store(tmp_path / "board.db")
    task_id = store.create(
        "Follow up",
        description="Promised follow-up: Friday\n\nRelease verified: v1.2 | commit abc",
    )
    report = export_markdown(store)
    assert f"### Task #{task_id}" in report
    assert "> Promised follow-up: Friday\n> \n> Release verified: v1.2 | commit abc" in report
