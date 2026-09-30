import json

import pytest

from sdk_support_hero.cli import main
from sdk_support_hero.store import Store
from sdk_support_hero.sync import Syncer, SyncError, config_path, load_config, validate_config

SUPPORT = {
    "host": "https://support.example.com",
    "project": 4242,
    "view": "example-view",
    "view_name": "Example queue",
}


def test_yaml_init_and_comments(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SDK_HERO_CONFIG", raising=False)
    path = tmp_path / "sdk-support-hero/config.yml"
    assert config_path() == path
    assert main(["init", "--repo", "example/sdk"]) == 0
    assert path.read_text().startswith("repos:\n")
    path.write_text("# Local board configuration\n" + path.read_text())
    assert load_config(path) == {"repos": ["example/sdk"], "support": None}


def test_legacy_fallback_yaml_precedence_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("SDK_HERO_CONFIG", raising=False)
    legacy = tmp_path / "sdk-support-hero/config.json"
    legacy.parent.mkdir()
    legacy.write_text('{"repos": [], "support": null}')
    assert config_path() == legacy
    assert load_config(legacy)["repos"] == []
    current = legacy.with_suffix(".yml")
    current.write_text("repos: [example/sdk]\nsupport: null\n")
    assert config_path() == current
    assert load_config(current)["repos"] == ["example/sdk"]
    monkeypatch.setenv("SDK_HERO_CONFIG", str(legacy))
    assert config_path() == legacy


@pytest.mark.parametrize("content", ["repos: [", "!!python/object/apply:builtins.str [hello]"])
def test_invalid_or_unsafe_yaml_is_rejected(tmp_path, content):
    path = tmp_path / "config.yml"
    path.write_text(content)
    with pytest.raises(ValueError, match="Invalid YAML configuration"):
        load_config(path)


@pytest.mark.parametrize("content", ["", "[]", "hello"])
def test_yaml_root_must_be_a_mapping(tmp_path, content):
    path = tmp_path / "config.yml"
    path.write_text(content)
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(path)


def test_init_support_requires_explicit_configuration(tmp_path, capsys):
    path = tmp_path / "config.json"
    assert main(["--config", str(path), "init", "--support"]) == 1
    assert not path.exists()
    assert "support.host" in capsys.readouterr().err
    args = ["--config", str(path), "init", "--support"]
    for key, value in SUPPORT.items():
        args += ["--support-" + key.replace("_", "-"), str(value)]
    assert main(args) == 0
    assert load_config(path) == {"repos": [], "support": SUPPORT}
    assert json.loads(path.read_text())["support"] == SUPPORT
    assert main(args) == 1
    assert load_config(path)["support"] == SUPPORT


@pytest.mark.parametrize(
    "key,value",
    [
        ("host", "http://support.example.com"),
        ("host", "https://user:pass@example.com"),
        ("host", "https://example.com/?token=example"),
        ("project", 0),
        ("project", True),
        ("view", ""),
        ("view_name", ""),
        ("view_name", None),
    ],
)
def test_invalid_support_configuration(key, value):
    with pytest.raises(ValueError):
        validate_config({"repos": [], "support": {**SUPPORT, key: value}})


def test_support_options_without_enable_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    assert main(["--config", str(path), "init", "--support-project", "4242"]) == 1
    assert not path.exists()


@pytest.mark.parametrize("name,role", [("Other queue", "role"), ("Example queue", "user")])
def test_configured_queue_guard_prevents_wrong_intake(tmp_path, monkeypatch, name, role):
    syncer = Syncer(Store(tmp_path / "board.db"), {"repos": [], "support": SUPPORT})
    calls = []

    def posthog(tool, args):
        calls.append(tool)
        assert args == {"short_id": "example-view"}
        return {"name": name, "filters": {"assignee": {"id": "example-role", "type": role}}}

    monkeypatch.setattr(syncer, "posthog", posthog)
    with pytest.raises(SyncError, match="Support view name or assignment role changed"):
        syncer.support()
    assert calls == ["conversations-views-retrieve"]
