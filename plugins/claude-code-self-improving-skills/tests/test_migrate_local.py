"""migrate_local.py 계약 테스트 — 샌드박스 HOME 에서 dry-run 무변경, 대상별 변환,
provenance 오탐 방지, 멱등성을 검증한다."""

import json
import math
import os
import sys

import pytest

from conftest import _sandbox_home_env

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 이하 — codex config 마이그레이션은 skip 대상
    tomllib = None

needs_tomllib = pytest.mark.skipif(tomllib is None, reason="tomllib(Python 3.11+) 필요")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import migrate_local


@pytest.fixture
def home(tmp_path, monkeypatch):
    # migrate_local resolves everything through Path.home(), which on Windows
    # reads USERPROFILE (not HOME) — a HOME-only sandbox would leak onto the
    # real profile and every migration would silently no-op there.
    for key, value in _sandbox_home_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    return tmp_path


def _write_settings(home, data):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_codex_config(home, content):
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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


@needs_tomllib
def test_codex_config_rewrite(home):
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n\n'
        '[hooks.state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "abc"\n')
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


@needs_tomllib
def test_codex_config_non_key_context_untouched(home):
    """키가 아닌 문맥(주석·이메일)의 @samton-claude, 접두 일치 리포 URL은 오폭 금지."""
    cfg = _write_codex_config(
        home,
        "# maintainer: dev@samton-claude.example\n"
        '# see https://github.com/samton-inc/samton-claude-archive.git\n'
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "dev@samton-claude.example" in text
    assert "samton-claude-archive.git" in text
    assert '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]' in text


@needs_tomllib
def test_codex_config_old_and_new_sections_coexist(home):
    """구·새 이름 섹션이 공존하면 구 섹션은 개명 대신 삭제 — 같은 테이블 2번 선언으로
    TOML 파싱이 깨지면 안 됨 (2026-07-20 codex 기동 장애 재발 방지). 신 값이 권위값."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n\n"
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n\n'
        "[marketplaces.samton-plugins]\n"
        'source = "https://github.com/samton-inc/samton-plugins.git"\n\n'
        '[hooks.state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "old"\n\n'
        '[hooks.state."chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "new"\n')
    report = migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))  # 파싱 통과 = 중복 선언 없음
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}
    assert data["marketplaces"] == {
        "samton-plugins": {"source": "https://github.com/samton-inc/samton-plugins.git"}}
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "new"}}
    assert any("제거" in d for _, d in report.changes)
    second = migrate_local.run(apply=True)
    assert second.changes == []


@needs_tomllib
def test_codex_config_partial_collision_renames_rest(home):
    """충돌 삭제는 섹션 단위 — 신 counterpart 가 없는 구 섹션은 기존대로 개명된다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n\n"
        '[hooks.state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "abc"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "abc"}}


@needs_tomllib
def test_codex_config_invalid_toml_skipped(home):
    """이미 깨진 config.toml 은 손대지 않고 경고만 — 추가 훼손 금지."""
    broken = ('[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
              "enabled = true\n"
              '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
              "enabled = true\n")
    cfg = _write_codex_config(home, broken)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == broken
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("유효한 TOML" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_header_lookalike_in_comment_not_collision(home):
    """주석 속 신 테이블 헤더 문자열은 충돌이 아니다 — 유일한 구 테이블을 삭제하면
    안 되고, 기존대로 개명되어야 한다 (충돌 판정은 파싱된 실제 테이블 키 기준)."""
    cfg = _write_codex_config(
        home,
        "# target: [marketplaces.samton-plugins]\n"
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_partially_migrated_key_not_corrupted(home):
    """이미 신 플러그인 이름 + 구 마켓인 키는 마켓만 갈아끼워야 한다 — 구 플러그인
    이름과의 접미 일치로 존재하지 않는 ID 가 만들어지면 안 됨."""
    cfg = _write_codex_config(
        home,
        '[plugins."chatgpt-codex-self-improving-skills@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "chatgpt-codex-claude-code" not in text
    data = tomllib.loads(text)
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_lookalike_prefix_suffix_ids(home):
    """접두/접미만 일치하는 이름은 오폭하지 않는다 — 단 플러그인·마켓 구성 요소는
    settings.json 의 _rename_plugin_id 처럼 독립적으로 치환된다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude-archive"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins-archive"]\n'
        "enabled = false\n\n"
        '[plugins."myfork-codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["plugins"]) == {
        # 플러그인 부분만 리네임 — '-archive' 접미 마켓 이름은 그대로
        "chatgpt-codex-self-improving-skills@samton-claude-archive",
        "chatgpt-codex-self-improving-skills@samton-plugins-archive",
        # 'myfork-' 접두 플러그인 이름은 그대로 — 마켓 잔여 키만 리네임
        "myfork-codex-self-improvement@samton-plugins",
    }


@needs_tomllib
def test_codex_config_convergent_keys_without_final_withheld(home):
    """구 키와 '플러그인만 신 이름'인 키가 같은 최종 키로 수렴하는데 완전한 최종
    키가 없으면, 어느 값이 권위인지 판정할 수 없다 — 임의 선택 대신 경고 후 보류."""
    content = ('[plugins."codex-self-improvement@samton-claude"]\n'
               "enabled = true\n\n"
               '[plugins."chatgpt-codex-self-improving-skills@samton-claude"]\n'
               "enabled = false\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("수렴" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_convergent_keys_with_final_pruned(home):
    """수렴 충돌이라도 완전한 최종 키가 함께 있으면 그 값이 권위값 — 나머지 구 키
    들은 삭제되고 마이그레이션이 진행된다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    report = migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}
    assert sum("제거" in d for _, d in report.changes) == 2


@needs_tomllib
def test_codex_config_old_plugin_on_new_marketplace_renamed(home):
    """마켓만 먼저 신 이름이 된 반쪽 상태(codex-self-improvement@samton-plugins)도
    구 플러그인 이름을 마저 리네임한다 — 고아로 남기면 안 됨."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-plugins"]\n'
        "enabled = true\n\n"
        '[hooks.state."codex-self-improvement@samton-plugins:hooks/hooks.json:stop:0:0"]\n'
        'trusted = "abc"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "abc"}}


@needs_tomllib
def test_codex_config_comment_with_key_reference_untouched(home):
    """주석 라인 안의 구 키 참조는 치환하지 않는다."""
    cfg = _write_codex_config(
        home,
        '# [plugins."codex-self-improvement@samton-claude"]\n'
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '# [plugins."codex-self-improvement@samton-claude"]' in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_nan_value_not_blocking(home):
    """TOML nan 값이 있어도 기대 모델 비교가 오탐하지 않고 마이그레이션된다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n"
        "score = nan\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    entry = data["plugins"]["chatgpt-codex-self-improving-skills@samton-plugins"]
    assert entry["enabled"] is True
    assert math.isnan(entry["score"])


@needs_tomllib
def test_codex_config_escaped_key_spelling_collision_warned_not_silent(home):
    """이스케이프 표기(\\u0063) 키는 정규식이 본문에서 못 찾는다 — 조용히 지나치지
    말고 보류 경고를 남겨야 한다."""
    content = (
        '[plugins."\\u0063odex-self-improvement@samton-\\u0063laude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("보류" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_collision_stale_url_in_new_table_still_migrated(home):
    """충돌에서 살아남는 신 테이블 안의 stale URL 은 정상 치환 대상 — 값 검증이
    이를 변형으로 오인해 마이그레이션을 보류하면 안 된다."""
    cfg = _write_codex_config(
        home,
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n\n'
        "[marketplaces.samton-plugins]\n"
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n')
    report = migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}
    assert any("제거" in d for _, d in report.changes)


@needs_tomllib
def test_codex_config_multiline_lookalike_boundary_refused(home):
    """여러 줄 문자열 속 '[unrelated]' 라인이 블록 경계로 오인되는 경우 — 훼손된
    결과를 쓰지 말고 보류해야 한다."""
    content = (
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "note = '''\n"
        "[unrelated]\n"
        "'''\n"
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("보류" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_quoted_parent_header_collision_deleted(home):
    """["plugins"."..."] 처럼 부모 세그먼트가 따옴표여도 충돌 구 테이블을 삭제한다."""
    cfg = _write_codex_config(
        home,
        '["plugins"."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}


@needs_tomllib
def test_codex_config_spaced_marketplace_header_renamed(home):
    """[ marketplaces . "samton-claude" ] 같은 표기 변형도 리네임된다 — URL 변경이
    없다는 이유로 구 키가 조용히 남으면 안 됨."""
    cfg = _write_codex_config(
        home,
        '[ marketplaces . "samton-claude" ]\n'
        'source = "https://github.com/samton-inc/samton-plugins.git"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_comment_above_new_table_preserved(home):
    """충돌 구 블록을 지울 때 다음 테이블 바로 위에 붙은 주석은 남긴다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        "# 신 테이블 설명 주석\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "# 신 테이블 설명 주석" in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}


@needs_tomllib
def test_codex_config_value_and_inline_comment_ids_untouched(home):
    """식별자 치환은 키 문맥(헤더·대입 좌변)에만 적용 — 문자열 값과 인라인 주석 속
    참조는 건드리지 않는다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true # codex-self-improvement@samton-plugins\n"
        'note = "codex-self-improvement@samton-plugins"\n')
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "enabled = true # codex-self-improvement@samton-plugins" in text
    entry = tomllib.loads(text)["plugins"][
        "chatgpt-codex-self-improving-skills@samton-plugins"]
    assert entry["note"] == "codex-self-improvement@samton-plugins"


@needs_tomllib
def test_codex_config_escaped_key_no_collision_warned(home):
    """충돌 상대가 없어도, 이스케이프 표기 때문에 본문에서 못 찾은 구 키를 조용히
    고아로 남기지 않는다 — 보류 경고 필수."""
    content = ('[plugins."\\u0063odex-self-improvement@samton-\\u0063laude"]\n'
               "enabled = true\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert any("보류" in w for w in report.warnings)


def test_main_warning_not_reported_as_up_to_date(home, capsys):
    """변경 0건이라도 경고(보류·수동 확인)가 있으면 '이미 최신 상태' 를 출력하지
    않는다 — 보류를 성공으로 오인시키지 말 것."""
    (home / ".codex-self-improvement").mkdir()
    (home / ".self-improving-skills").mkdir()  # 둘 다 존재 → 경고만, 변경 0건
    assert migrate_local.main([]) == 0
    out = capsys.readouterr().out
    assert "이미 최신 상태" not in out
    assert "주의" in out


@needs_tomllib
def test_codex_config_non_target_section_ids_untouched(home):
    """plugins/hooks.state 밖 섹션(mcp_servers 등)의 id 모양 키는 마이그레이션
    대상이 아니다 — 기존 참조가 깨지지 않게 그대로 둔다."""
    cfg = _write_codex_config(
        home,
        '[mcp_servers."codex-self-improvement@samton-claude"]\n'
        'command = "x"\n\n'
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcp_servers"] == {
        "codex-self-improvement@samton-claude": {"command": "x"}}
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_id_only_renamed_at_key_head(home):
    """식별자 치환은 따옴표 직후(키 선두)만 — 훅 경로 중간(:hooks/...)이나 '_'
    접두 등 다른 위치의 이름 모양은 건드리지 않는다."""
    cfg = _write_codex_config(
        home,
        '[hooks.state."feature@samton-claude:hooks/codex-self-improvement@samton-claude:stop:0:0"]\n'
        'trusted = "abc"\n\n'
        '[plugins."_codex-self-improvement@samton-plugins"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "feature@samton-plugins:hooks/codex-self-improvement@samton-claude:stop:0:0":
            {"trusted": "abc"}}
    assert data["plugins"] == {
        "_codex-self-improvement@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_inline_table_keys_renamed(home):
    """plugins = { "키" = ... } 인라인 테이블 표기의 플러그인 키도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = { enabled = true } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_inline_table_trailing_comment_untouched(home):
    """인라인 테이블 뒤 꼬리 주석 속 키 모양 텍스트는 치환하지 않는다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = { enabled = true } } '
        '# "codex-self-improvement@samton-claude" = example\n')
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '# "codex-self-improvement@samton-claude" = example' in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_relative_dotted_key_under_other_table_untouched(home):
    """[other] 아래의 상대 점 키 plugins."..." 는 루트 plugins 가 아니다 — 오폭
    없이 루트 섹션만 마이그레이션되어야 한다."""
    cfg = _write_codex_config(
        home,
        "[other]\n"
        'plugins."codex-self-improvement@samton-claude".enabled = true\n\n'
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["other"]["plugins"] == {
        "codex-self-improvement@samton-claude": {"enabled": True}}
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}


@needs_tomllib
def test_codex_config_quote_inside_key_path_not_anchor(home):
    """키 경로 내부의 아포스트로피는 키 시작 앵커가 아니다 — 경로 부분 오폭 금지."""
    cfg = _write_codex_config(
        home,
        '[hooks.state."feature@samton-claude:hooks/it\'s-codex-self-improvement@samton-claude:0:0"]\n'
        'trusted = "abc"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "feature@samton-plugins:hooks/it's-codex-self-improvement@samton-claude:0:0":
            {"trusted": "abc"}}


@needs_tomllib
def test_codex_config_inline_collision_withheld_safely(home):
    """헤더 없는 표기(인라인 테이블)의 구·신 충돌은 블록 삭제가 불가능하다 —
    훼손해서 쓰지 말고 보류해야 한다."""
    content = ('plugins = { "codex-self-improvement@samton-claude" = { enabled = true }, '
               '"chatgpt-codex-self-improving-skills@samton-plugins" = { enabled = false } }\n')
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("보류" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_embedded_quote_in_key_not_anchor(home):
    """이중 따옴표 키 내부의 «공백+아포스트로피» 는 키 시작이 아니다 — 키 선두
    구성 요소만 치환되고 내부 조각은 그대로."""
    cfg = _write_codex_config(
        home,
        '[plugins."owner \'dev@samton-claude"]\n'
        "enabled = true\n\n"
        '[hooks.state."feature@samton-claude:hooks/ \'codex-self-improvement@samton-claude:0:0"]\n'
        'trusted = "abc"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    # plugins 키의 선두 plugin 부분("owner 'dev")은 미지의 이름이라 유지, 마켓만 갱신
    assert data["plugins"] == {"owner 'dev@samton-plugins": {"enabled": True}}
    assert data["hooks"]["state"] == {
        "feature@samton-plugins:hooks/ 'codex-self-improvement@samton-claude:0:0":
            {"trusted": "abc"}}


@needs_tomllib
def test_codex_config_hooks_table_relative_state_forms(home):
    """[hooks] 아래 state."키"... 상대 점 키 표기도 리네임된다."""
    cfg = _write_codex_config(
        home,
        "[hooks]\n"
        'state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0".trusted = "a"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "a"}}


@needs_tomllib
def test_codex_config_root_inline_nested_hooks_state(home):
    """hooks = { state = { "키" = ... } } 중첩 인라인 표기도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'hooks = { state = { "codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0" '
        '= { trusted = "a" } } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "a"}}


@needs_tomllib
def test_codex_config_inline_triple_quote_comment_untouched(home):
    """인라인 테이블 값의 삼중 따옴표 문자열이 있어도 꼬리 주석은 치환되지 않는다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = { note = """a"b""" } } '
        '# "codex-self-improvement@samton-claude" = x\n')
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '# "codex-self-improvement@samton-claude" = x' in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"note": 'a"b'}}


@needs_tomllib
def test_codex_config_inline_dotted_key_renamed(home):
    """인라인 테이블의 점 키 연속("키".enabled = true) 표기도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude".enabled = true }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_quad_quote_terminator_comment_untouched(home):
    """4연속 따옴표(마지막 1개는 내용)로 끝나는 여러 줄 문자열 뒤의 주석도
    치환되지 않는다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = { note = """a"""" } } '
        '# "codex-self-improvement@samton-claude" = x\n')
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '# "codex-self-improvement@samton-claude" = x' in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"note": 'a"'}}


@needs_tomllib
def test_codex_config_multiline_value_keeps_root_context(home):
    """루트 여러 줄 문자열 안의 '[example]' 라인은 테이블 헤더가 아니다 — 이후
    루트 점 키 마이그레이션이 계속 동작해야 한다."""
    cfg = _write_codex_config(
        home,
        'banner = """\n'
        "[example]\n"
        '"""\n'
        'plugins."codex-self-improvement@samton-claude".enabled = true\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["banner"] == "[example]\n"
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_equals_inside_key_lhs_split(home):
    """따옴표 키 내부의 '=' 는 대입 구분자가 아니다 — 좌변 분리가 어긋나 리네임을
    놓치면 안 된다."""
    cfg = _write_codex_config(
        home,
        'hooks.state."codex-self-improvement@samton-claude:hooks/a=b.json:stop:0:0".trusted = "x"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/a=b.json:stop:0:0":
            {"trusted": "x"}}


@needs_tomllib
def test_codex_config_inline_string_value_not_key(home):
    """인라인 테이블의 문자열 값 안에 있는 키 모양 텍스트는 치환되지 않는다 —
    정상 키 리네임까지 보류시키면 안 된다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = { note = '
        "'\"codex-self-improvement@samton-claude\" = example' } }\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {
            "note": '"codex-self-improvement@samton-claude" = example'}}


@needs_tomllib
def test_codex_config_multiline_array_keeps_root_context(home):
    """루트 여러 줄 배열의 '[1, 2],' 요소 라인은 테이블 헤더가 아니다 — 이후 루트
    점 키 마이그레이션이 계속 동작해야 한다."""
    cfg = _write_codex_config(
        home,
        "arr = [\n"
        "    [1, 2],\n"
        "]\n"
        'plugins."codex-self-improvement@samton-claude".enabled = true\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["arr"] == [[1, 2]]
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_option_key_inside_plugin_table_untouched(home):
    """[plugins."x"] 자식 테이블 안의 옵션 키는 id 가 아니다 — 치환 금지."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        '"dev@samton-claude" = true\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {
            "dev@samton-claude": True}}


@needs_tomllib
def test_codex_config_inline_nested_non_target_sibling_untouched(home):
    """hooks 인라인의 state 밖 형제 테이블(metadata 등) 키는 치환하지 않는다."""
    cfg = _write_codex_config(
        home,
        'hooks = { metadata = { "dev@samton-claude" = true }, '
        'state = { "codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0" '
        '= { trusted = "a" } } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["metadata"] == {"dev@samton-claude": True}
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "a"}}


@needs_tomllib
def test_codex_config_multiline_array_without_trailing_comma(home):
    """후행 쉼표 없는 마지막 배열 요소 '[1, 2]' 라인도 헤더가 아니다."""
    cfg = _write_codex_config(
        home,
        "arr = [\n"
        "    [1, 2]\n"
        "]\n"
        'plugins."codex-self-improvement@samton-claude".enabled = true\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["arr"] == [[1, 2]]
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_inline_dotted_state_key_renamed(home):
    """hooks = { state."키".trusted = ... } 점 표기 인라인도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'hooks = { state."codex-self-improvement@samton-claude:hooks/hooks.json:stop:0:0".trusted = "a" }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/hooks.json:stop:0:0":
            {"trusted": "a"}}


@needs_tomllib
def test_codex_config_header_subpath_tokens_preserved(home):
    """헤더의 하위 경로 세그먼트(옵션 키 등)는 치환하지 않는다 — 첫 키만."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude".options."dev@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {
            "options": {"dev@samton-claude": {"enabled": True}}}}


@needs_tomllib
def test_codex_config_dotted_option_after_id_preserved(home):
    """[plugins] 아래 점 키 연속에서 id 뒤의 옵션 세그먼트는 치환하지 않는다."""
    cfg = _write_codex_config(
        home,
        "[plugins]\n"
        '"codex-self-improvement@samton-claude"."dev@samton-claude" = true\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {
            "dev@samton-claude": True}}


@needs_tomllib
def test_codex_config_bracket_inside_key_header_renamed(home):
    """따옴표 키 안의 ']' 가 있어도 헤더 대괄호부를 올바르게 잘라 리네임한다."""
    cfg = _write_codex_config(
        home,
        '[hooks.state."codex-self-improvement@samton-claude:hooks/a]b.json:stop:0:0"]\n'
        'trusted = "x"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["hooks"]["state"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins:hooks/a]b.json:stop:0:0":
            {"trusted": "x"}}


@needs_tomllib
def test_codex_config_bare_subkey_header_not_renamed(home):
    """[plugins.options."dev@…"] 처럼 섹션 직결 세그먼트가 bare 면 그 안의 중첩
    따옴표 키는 id 가 아니다 — 옆의 진짜 구 키만 개명돼야 한다."""
    cfg = _write_codex_config(
        home,
        '[plugins.options."dev@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"]["options"] == {"dev@samton-claude": {"enabled": True}}
    assert data["plugins"][
        "chatgpt-codex-self-improving-skills@samton-plugins"] == {
            "enabled": False}


@needs_tomllib
def test_codex_config_marketplaces_table_key_renamed(home):
    """[marketplaces] 아래 bare 키 선언도 마켓 이름 리네임 대상이다."""
    cfg = _write_codex_config(
        home,
        "[marketplaces]\n"
        'samton-claude = { source = "https://github.com/washcarnewcar/samton-claude.git" }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_root_dotted_marketplace_renamed(home):
    """루트 점 키 marketplaces.구이름.… 표기도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'marketplaces.samton-claude.source = "https://github.com/washcarnewcar/samton-claude.git"\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_marketplace_subtable_header_renamed(home):
    """[marketplaces.구이름.하위] 헤더의 마켓 세그먼트도 리네임된다."""
    cfg = _write_codex_config(
        home,
        "[marketplaces.samton-claude]\n"
        'source = "https://github.com/samton-inc/samton-plugins.git"\n\n'
        "[marketplaces.samton-claude.extra]\n"
        "x = 1\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git",
            "extra": {"x": 1}}}


@needs_tomllib
def test_codex_config_inline_marketplaces_renamed(home):
    """marketplaces = { 구이름 = ... } 인라인 선언도 리네임된다."""
    cfg = _write_codex_config(
        home,
        'marketplaces = { samton-claude = { source = '
        '"https://github.com/washcarnewcar/samton-claude.git" } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_multiline_market_header_lookalike_untouched(home):
    """여러 줄 문자열 값 속 [marketplaces.구이름] 모양 라인은 치환·경고 대상이
    아니다 — 이미 최신인 설정은 무변경·무경고여야 한다."""
    content = ('note = """\n'
               "[marketplaces.samton-claude]\n"
               '"""\n')
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert report.warnings == []


@needs_tomllib
def test_codex_config_header_trailing_comment_lookalike_untouched(home):
    """헤더 뒤 꼬리 주석 속 헤더 모양 텍스트는 치환하지 않는다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"] '
        '# [plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert '# [plugins."codex-self-improvement@samton-claude"]' in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": True}}


@needs_tomllib
def test_codex_config_inline_marketplaces_nested_key_preserved(home):
    """인라인 marketplaces 의 중첩 테이블 속 동명 키는 보존된다 — 직접 키만 개명."""
    cfg = _write_codex_config(
        home,
        'marketplaces = { samton-claude = { aliases = { samton-claude = true }, '
        'source = "https://github.com/washcarnewcar/samton-claude.git" } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["marketplaces"] == {
        "samton-plugins": {
            "aliases": {"samton-claude": True},
            "source": "https://github.com/samton-inc/samton-plugins.git"}}


@needs_tomllib
def test_codex_config_comment_stale_url_preserved(home):
    """주석 속 stale URL 은 값이 아니다 — 치환·백업 없이 그대로 보존한다."""
    content = ("# mirror: https://github.com/washcarnewcar/samton-claude.git\n"
               '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
               "enabled = true\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert report.changes == []


@needs_tomllib
def test_codex_config_inline_escaped_triple_quote_value(home):
    """인라인 값의 이스케이프된 3연속 따옴표를 문자열 종료로 오인하지 않는다."""
    cfg = _write_codex_config(
        home,
        'plugins = { "codex-self-improvement@samton-claude" = '
        '{ note = """a\\"""b""" } }\n')
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {
            "note": 'a"""b'}}


def test_codex_state_always_withheld_without_tomllib(home, monkeypatch):
    """파서 없는 환경에서 config.toml 이 존재하면 검증 자체가 불가 — 내용이 최신으로
    보여도 상태 이동은 항상 보류한다."""
    monkeypatch.setattr(migrate_local, "tomllib", None)
    _write_codex_config(
        home,
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = true\n")
    (home / ".codex-self-improvement").mkdir()
    report = migrate_local.run(apply=True)
    assert (home / ".codex-self-improvement").exists()
    assert not (home / ".self-improving-skills").exists()
    assert any("함께 보류" in w for w in report.warnings)
    assert any("Python 3.11" in w for w in report.warnings)  # 전제 조건 안내


def test_codex_state_moves_without_config_without_tomllib(home, monkeypatch):
    """config.toml 자체가 없으면 파서 부재와 무관 — 상태 디렉토리는 정상 이동한다."""
    monkeypatch.setattr(migrate_local, "tomllib", None)
    (home / ".codex-self-improvement").mkdir()
    report = migrate_local.run(apply=True)
    assert not (home / ".codex-self-improvement").exists()
    assert (home / ".self-improving-skills").exists()
    assert not any("Python 3.11" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_collision_prunes_descendant_tables(home):
    """충돌 구 키의 자식 테이블([plugins."구키".하위])도 함께 삭제된다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n\n"
        '[plugins."codex-self-improvement@samton-claude".metadata]\n'
        "x = 1\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}


@needs_tomllib
def test_codex_config_collision_block_comments_preserved(home):
    """충돌 구 블록 안의 사용자 주석은 단독 라인·헤더/값 꼬리 주석 모두 남긴다."""
    cfg = _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"] # 설치 메모\n'
        "# 사용자 메모: 이 플러그인은 수동 설치함\n"
        "enabled = true # 수동 고정\n\n"
        '[plugins."chatgpt-codex-self-improving-skills@samton-plugins"]\n'
        "enabled = false\n")
    migrate_local.run(apply=True)
    text = cfg.read_text(encoding="utf-8")
    assert "# 설치 메모" in text
    assert "# 사용자 메모: 이 플러그인은 수동 설치함" in text
    assert "# 수동 고정" in text
    assert tomllib.loads(text)["plugins"] == {
        "chatgpt-codex-self-improving-skills@samton-plugins": {"enabled": False}}


def test_codex_state_move_withheld_when_config_withheld(home, monkeypatch):
    """config 마이그레이션이 보류되면 상태 디렉토리 이동도 함께 보류 — 구 설정이
    구 상태 경로를 계속 쓰므로 어긋나게 두면 안 된다."""
    monkeypatch.setattr(migrate_local, "tomllib", None)
    _write_codex_config(
        home,
        '[plugins."codex-self-improvement@samton-claude"]\n'
        "enabled = true\n")
    (home / ".codex-self-improvement").mkdir()
    report = migrate_local.run(apply=True)
    assert (home / ".codex-self-improvement").exists()
    assert not (home / ".self-improving-skills").exists()
    assert any("함께 보류" in w for w in report.warnings)


def test_codex_config_without_tomllib_skipped(home, monkeypatch):
    """tomllib 없는 환경(Python 3.10 이하)에서는 검증 없이 삭제·치환하지 않는다 —
    건너뛰고 경고만."""
    monkeypatch.setattr(migrate_local, "tomllib", None)
    content = ('[plugins."codex-self-improvement@samton-claude"]\n'
               "enabled = true\n")
    cfg = _write_codex_config(home, content)
    report = migrate_local.run(apply=True)
    assert cfg.read_text(encoding="utf-8") == content
    assert not list(cfg.parent.glob("*.bak-migration-*"))
    assert any("Python 3.11" in w for w in report.warnings)


@needs_tomllib
def test_codex_config_and_state_idempotent(home):
    _write_codex_config(
        home,
        '[marketplaces.samton-claude]\n'
        'source = "https://github.com/washcarnewcar/samton-claude.git"\n')
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


# --- team-share sunset (v0.12.0 기능 제거) -----------------------------------

def _write_usage(home, data):
    path = home / ".claude" / "self-improve" / "skill_usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


USAGE_WITH_TEAM = {
    "_meta": {"offsets": {"s1": {"o": 3, "t": "2026-07-01T00:00:00+00:00"}}},
    "team-born": {"use_count": 2, "created_by": "team", "state": "active"},
    "my-own": {"use_count": 5, "created_by": "agent", "state": "active"},
    "hand-made": {"use_count": 1, "created_by": "user", "state": "active"},
}


def test_team_sunset_rewrites_created_by(home):
    path = _write_usage(home, USAGE_WITH_TEAM)
    migrate_local.run(apply=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["team-born"]["created_by"] == "agent"
    assert data["team-born"]["use_count"] == 2  # 나머지 필드는 불변
    assert data["my-own"]["created_by"] == "agent"
    assert data["hand-made"]["created_by"] == "user"
    assert data["_meta"] == USAGE_WITH_TEAM["_meta"]
    assert list(path.parent.glob("skill_usage.json.bak-migration-*"))


def test_team_sunset_dry_run_no_write(home):
    path = _write_usage(home, USAGE_WITH_TEAM)
    before = path.read_text(encoding="utf-8")
    report = migrate_local.run(apply=False)
    assert any("created_by:team" in desc for _p, desc in report.changes)
    assert path.read_text(encoding="utf-8") == before
    assert not list(path.parent.glob("*.bak-migration-*"))


def test_team_sunset_idempotent(home):
    _write_usage(home, USAGE_WITH_TEAM)
    assert migrate_local.run(apply=True).changes
    assert migrate_local.run(apply=True).changes == []


def test_team_leftovers_warned_not_deleted(home):
    state = home / ".claude" / "self-improve"
    state.mkdir(parents=True)
    (state / "team_sync.json").write_text("{}", encoding="utf-8")
    (state / "team_config.json").write_text("{}", encoding="utf-8")
    (state / "team_quarantine").mkdir()
    report = migrate_local.run(apply=True)
    assert report.changes == []  # 안내만, 재작성 대상 아님
    warned = "\n".join(report.warnings)
    for name in ("team_sync.json", "team_config.json", "team_quarantine"):
        assert name in warned
        assert (state / name).exists()  # 절대 삭제하지 않는다
