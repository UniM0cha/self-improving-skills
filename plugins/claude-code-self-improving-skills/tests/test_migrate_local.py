"""migrate_local.py 계약 테스트 — 샌드박스 HOME 에서 dry-run 무변경, 대상별 변환,
provenance 오탐 방지, 멱등성을 검증한다."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import migrate_local


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_settings(home, data):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


SETTINGS = {
    "permissions": {
        "allow": [
            "Agent(self-improving-skills:skill-distiller)",
            "Read(~/.claude/skills/**)",
        ]
    },
    "enabledPlugins": {
        "feature@samton-claude": True,
        "self-improving-skills@samton-claude": True,
        "tmap@other-market": True,
    },
    "extraKnownMarketplaces": {
        "samton-claude": {
            "source": {"source": "git",
                       "url": "https://github.com/washcarnewcar/samton-claude.git"}
        }
    },
}


def test_dry_run_reports_but_does_not_write(home):
    path = _write_settings(home, SETTINGS)
    before = path.read_text(encoding="utf-8")
    report = migrate_local.run(apply=False)
    assert report.changes
    assert path.read_text(encoding="utf-8") == before
    assert not list(path.parent.glob("*.bak-migration-*"))


def test_settings_migration(home):
    path = _write_settings(home, SETTINGS)
    migrate_local.run(apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "Agent(claude-code-self-improving-skills:skill-distiller)" in data["permissions"]["allow"]
    assert "Agent(self-improving-skills:skill-distiller)" not in data["permissions"]["allow"]
    assert "Read(~/.claude/skills/**)" in data["permissions"]["allow"]
    assert data["enabledPlugins"] == {
        "feature@samton-plugins": True,
        "claude-code-self-improving-skills@samton-plugins": True,
        "tmap@other-market": True,
    }
    markets = data["extraKnownMarketplaces"]
    assert "samton-claude" not in markets
    assert markets["samton-plugins"]["source"]["url"] == \
        "https://github.com/samton-inc/samton-plugins.git"
    assert list(path.parent.glob("settings.json.bak-migration-*"))


def test_skills_namespace_patched_provenance_kept(home):
    skill = home / ".claude" / "skills" / "some-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: some-skill\ndescription: d\nmetadata:\n"
        "  provenance: self-improving-skills\n---\n"
        'subagent_type="self-improving-skills:skill-distiller" 로 호출.\n'
        "cowork 는 self-improving-skills-cowork:skill-distiller 를 쓴다.\n"
        "참조 구현: plugins/self-improving-skills/scripts/propose_pr.py\n"
        "경로: /Users/x/Desktop/Samton/Repositories/samton-claude/plugins/self-improving-skills/\n",
        encoding="utf-8")
    migrate_local.run(apply=True)
    text = skill.read_text(encoding="utf-8")
    assert "provenance: self-improving-skills\n" in text          # 마커는 유지
    assert 'subagent_type="claude-code-self-improving-skills:skill-distiller"' in text
    assert "claude-cowork-self-improving-skills:skill-distiller" in text
    assert "plugins/claude-code-self-improving-skills/scripts/propose_pr.py" in text
    assert "Repositories/samton-plugins/plugins/claude-code-self-improving-skills/" in text
    assert "self-improving-skills:skill-distiller\" 로" not in text.replace(
        "claude-code-self-improving-skills:skill-distiller", "")


def test_skills_already_migrated_untouched(home):
    skill = home / ".claude" / "skills" / "fresh" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    content = ("---\nname: fresh\nmetadata:\n  provenance: self-improving-skills\n---\n"
               "claude-code-self-improving-skills:skill-distiller 를 호출한다.\n")
    skill.write_text(content, encoding="utf-8")
    report = migrate_local.run(apply=True)
    assert skill.read_text(encoding="utf-8") == content
    assert not any(str(skill) == p for p, _ in report.changes)


def test_codex_config_rewrite(home):
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n\n'
        '[hooks.state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "abc"\n',
        encoding="utf-8")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]' in text
    assert "[marketplaces.samton-plugins]" in text
    assert "https://github.com/samton-inc/samton-plugins.git" in text
    assert "samton-claude" not in text
    assert "codex-self-improvement@" not in text


def test_codex_state_dir_moved_and_provenance_swept(home):
    old = home / ".codex-self-improvement"
    (old / "backups" / "b1").mkdir(parents=True)
    (old / "usage.json").write_text("{}", encoding="utf-8")
    (old / "backups" / "b1" / "SKILL.md").write_text(
        "---\nname: b\nmetadata:\n  provenance: codex-self-improvement\n---\nbody\n",
        encoding="utf-8")
    codex_skill = home / ".codex" / "skills" / "railway-x" / "SKILL.md"
    codex_skill.parent.mkdir(parents=True)
    codex_skill.write_text(
        "---\nname: railway-x\nmetadata:\n  provenance: codex-self-improvement\n---\nbody\n",
        encoding="utf-8")
    migrate_local.run(apply=True)
    new = home / ".self-improving-skills"
    assert not old.exists()
    assert (new / "usage.json").exists()
    assert "provenance: self-improving-skills" in \
        (new / "backups" / "b1" / "SKILL.md").read_text(encoding="utf-8")
    assert "provenance: self-improving-skills" in codex_skill.read_text(encoding="utf-8")
    assert "codex-self-improvement" not in codex_skill.read_text(encoding="utf-8")


def test_state_dir_conflict_warns_and_skips(home):
    (home / ".codex-self-improvement").mkdir()
    (home / ".self-improving-skills").mkdir()
    report = migrate_local.run(apply=True)
    assert (home / ".codex-self-improvement").exists()
    assert any("둘 다 존재" in w for w in report.warnings)


def test_marketplace_registry_warns_but_not_modified(home):
    reg = home / ".claude" / "plugins" / "known_marketplaces.json"
    reg.parent.mkdir(parents=True)
    content = json.dumps({"samton-claude": {"source": {"url": "x"}}})
    reg.write_text(content, encoding="utf-8")
    report = migrate_local.run(apply=True)
    assert reg.read_text(encoding="utf-8") == content
    assert any("marketplace remove" in w for w in report.warnings)


def test_preexisting_allow_duplicates_untouched_and_unreported(home):
    """리네임과 무관한 기존 중복은 보존 — dry-run 미보고 변경이 apply 에서 생기면 안 됨."""
    path = _write_settings(home, {
        "permissions": {"allow": ["Read(x)", "Read(x)", "Read(y)"]},
    })
    before = path.read_text(encoding="utf-8")
    report = migrate_local.run(apply=True)
    assert report.changes == []
    assert path.read_text(encoding="utf-8") == before
    assert not list(path.parent.glob("*.bak-migration-*"))


def test_rename_collision_in_allow_reported(home):
    path = _write_settings(home, {
        "permissions": {"allow": [
            "Agent(self-improving-skills:skill-distiller)",
            "Agent(claude-code-self-improving-skills:skill-distiller)",
        ]},
    })
    report = migrate_local.run(apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["permissions"]["allow"] == \
        ["Agent(claude-code-self-improving-skills:skill-distiller)"]
    assert any("제거" in d for _, d in report.changes)


def test_enabled_plugins_new_key_value_wins(home):
    """사용자가 신 키를 명시적으로 꺼 뒀으면(False) 구 키(True)가 되살리면 안 됨."""
    path = _write_settings(home, {
        "enabledPlugins": {
            "self-improving-skills@samton-claude": True,
            "claude-code-self-improving-skills@samton-plugins": False,
        },
    })
    migrate_local.run(apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == \
        {"claude-code-self-improving-skills@samton-plugins": False}


def test_extra_marketplaces_old_and_new_coexist(home):
    path = _write_settings(home, {
        "extraKnownMarketplaces": {
            "samton-claude": {"source": {"source": "git", "url": "old"}},
            "samton-plugins": {
                "source": {"source": "git",
                           "url": "https://github.com/samton-inc/samton-plugins.git"}
            },
        },
    })
    migrate_local.run(apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["extraKnownMarketplaces"]) == ["samton-plugins"]
    assert data["extraKnownMarketplaces"]["samton-plugins"]["source"]["url"] == \
        "https://github.com/samton-inc/samton-plugins.git"


def test_codex_config_non_key_context_untouched(home):
    """키가 아닌 문맥(주석·이메일)의 @samton-claude, 접두 일치 리포 URL은 오폭 금지."""
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "# maintainer: dev@samton-claude.example\n"
        '# see https://github.com/samton-inc/samton-claude-archive.git\n'
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n",
        encoding="utf-8")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "dev@samton-claude.example" in text
    assert "samton-claude-archive.git" in text
    assert '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]' in text


def test_codex_config_and_state_idempotent(home):
    cfg = home / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[marketplaces.samton-claude]\n'
                   'source = "https://github.com/washcarnewcar/samton-claude.git"\n',
                   encoding="utf-8")
    (home / ".codex-self-improvement").mkdir()
    first = migrate_local.run(apply=True)
    assert first.changes
    second = migrate_local.run(apply=True)
    assert second.changes == []


def test_backup_name_collision_gets_suffix(home):
    skill = home / ".claude" / "skills" / "s" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("self-improving-skills:skill-distiller\n", encoding="utf-8")
    existing = skill.with_name("SKILL.md.bak-migration-" + migrate_local.STAMP)
    existing.write_text("먼저 있던 백업", encoding="utf-8")
    migrate_local.run(apply=True)
    assert existing.read_text(encoding="utf-8") == "먼저 있던 백업"
    assert (skill.parent / ("SKILL.md.bak-migration-" + migrate_local.STAMP + "-2")).exists()


def test_idempotent_second_run(home):
    _write_settings(home, SETTINGS)
    skill = home / ".claude" / "skills" / "s" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("self-improving-skills:skill-distiller\n", encoding="utf-8")
    first = migrate_local.run(apply=True)
    assert first.changes
    second = migrate_local.run(apply=True)
    assert second.changes == []


def test_noop_when_nothing_exists(home):
    report = migrate_local.run(apply=True)
    assert report.changes == []


def test_main_exit_code_and_output(home, capsys):
    _write_settings(home, SETTINGS)
    assert migrate_local.main([]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "--apply" in out
