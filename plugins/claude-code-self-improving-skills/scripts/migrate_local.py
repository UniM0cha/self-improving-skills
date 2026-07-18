#!/usr/bin/env python3
"""migrate_local.py — 플러그인/마켓플레이스 리네임으로 orphan된 로컬 상태를 최신 이름으로 올린다.

2026-07 samton-plugins 개편에서 다음이 리네임되었다:

  marketplace  samton-claude                 -> samton-plugins
  plugin       self-improving-skills         -> claude-code-self-improving-skills
  plugin       self-improving-skills-cowork  -> claude-cowork-self-improving-skills
  plugin       codex-self-improvement        -> chatgpt-codex-self-improving-skills
  plugin       chatgpt-work-self-improvement -> chatgpt-work-self-improving-skills
  (codex 내부 식별자 — MCP 서버명·provenance·상태 디렉토리 — 는 "self-improving-skills"로 통일)

이 스크립트는 마켓플레이스 재등록(remove/add)이 고쳐 주지 **못하는** 로컬 상태를 재작성한다:

  ~/.claude/settings.json        permissions.allow 네임스페이스, enabledPlugins 키,
                                 extraKnownMarketplaces (stale URL -> canonical)
  ~/.claude/skills/**/SKILL.md   학습 스킬 본문의 구 네임스페이스/플러그인 경로 참조
  ~/.codex/config.toml           plugin@marketplace 키, marketplace 섹션/URL
  ~/.codex-self-improvement/     -> ~/.self-improving-skills/ (디렉토리 이동)
  codex 증류 스킬 provenance     codex-self-improvement -> self-improving-skills

기본은 dry-run(보고만). --apply 로 실제 적용하며, 수정 대상 파일은 먼저
<파일>.bak-migration-<UTC타임스탬프> 로 백업한다. 적용 후 재실행은 no-op(멱등).

주의: 학습 스킬 frontmatter 의 `provenance: self-improving-skills` 값은 리네임과
무관하게 **의도적으로 유지되는** 마커다 — 이 스크립트는 절대 건드리지 않는다
(네임스페이스 치환은 `이름:` 꼴, 경로 치환은 `plugins/이름/` 꼴로 앵커링).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Rename map — 향후 리네임이 또 생기면 여기에만 추가한다.
# ---------------------------------------------------------------------------

MARKETPLACE_RENAMES = {
    "samton-claude": "samton-plugins",
}

PLUGIN_RENAMES = {
    "self-improving-skills": "claude-code-self-improving-skills",
    "self-improving-skills-cowork": "claude-cowork-self-improving-skills",
    "codex-self-improvement": "chatgpt-codex-self-improving-skills",
    "chatgpt-work-self-improvement": "chatgpt-work-self-improving-skills",
}

# GitHub slug 교체 (https/ssh 두 표기 모두) — 구 리포·이관 전 개인 계정 stale URL을
# canonical 로 직결한다. 값에 .git 접미사가 붙어 있어도 그대로 보존된다.
URL_RENAMES = {
    "github.com/washcarnewcar/samton-claude": "github.com/samton-inc/samton-plugins",
    "github.com:washcarnewcar/samton-claude": "github.com:samton-inc/samton-plugins",
    "github.com/samton-inc/samton-claude": "github.com/samton-inc/samton-plugins",
    "github.com:samton-inc/samton-claude": "github.com:samton-inc/samton-plugins",
}

# codex 내부 식별자 통일 (codex 환경 한정 — Claude 플러그인 이름과 충돌하지 않음)
CODEX_STATE_DIR_OLD = ".codex-self-improvement"
CODEX_STATE_DIR_NEW = ".self-improving-skills"
CODEX_PROVENANCE_PATTERNS = [
    ("provenance: codex-self-improvement", "provenance: self-improving-skills"),
    ('"provenance": "codex-self-improvement"', '"provenance": "self-improving-skills"'),
]

# 학습 스킬(~/.claude/skills) 본문 치환 규칙. 앵커링이 핵심:
#   - `이름:` (바로 뒤 콜론) — 서브에이전트/스킬 네임스페이스 참조만 잡고,
#     `provenance: self-improving-skills` (콜론이 이름 *앞*) 는 절대 매칭 안 됨.
#   - lookbehind 로 새 이름(claude-code-self-improving-skills:)의 부분 문자열 재매칭 차단.
#   - `plugins/이름/` — 뒤 슬래시 덕에 -cowork 등 파생 이름과 충돌 없음.
def _skill_rules():
    rules = []
    for old, new in sorted(PLUGIN_RENAMES.items(), key=lambda t: -len(t[0])):
        rules.append((re.compile(r"(?<![A-Za-z0-9-])" + re.escape(old) + r":"), new + ":"))
        rules.append((re.compile(re.escape("plugins/" + old + "/")), "plugins/" + new + "/"))
    rules.append((re.compile(re.escape("Repositories/samton-claude")),
                  "Repositories/samton-plugins"))
    return rules


STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Report:
    def __init__(self, apply):
        self.apply = apply
        self.changes = []   # (path, description)
        self.warnings = []

    def change(self, path, desc):
        self.changes.append((str(path), desc))

    def warn(self, msg):
        self.warnings.append(msg)


def _backup_path(path: Path) -> Path:
    base = path.with_name(path.name + ".bak-migration-" + STAMP)
    cand, i = base, 2
    while cand.exists():
        cand = path.with_name(base.name + "-" + str(i))
        i += 1
    return cand


def _backup_and_write(path: Path, new_text: str, report: Report):
    if report.apply:
        shutil.copy2(path, _backup_path(path))
        path.write_text(new_text, encoding="utf-8")


def _rename_plugin_id(plugin_id: str) -> str:
    """'plugin@marketplace' 키를 새 이름으로. '@' 가 없으면 그대로."""
    if "@" not in plugin_id:
        return plugin_id
    plugin, _, market = plugin_id.rpartition("@")
    return (PLUGIN_RENAMES.get(plugin, plugin) + "@"
            + MARKETPLACE_RENAMES.get(market, market))


def _rewrite_url(url: str) -> str:
    # 경계 앵커: samton-claude-archive 같은 접두 일치 리포를 오폭하지 않도록
    for old, new in URL_RENAMES.items():
        url = re.sub(re.escape(old) + r"(?![A-Za-z0-9-])", new, url)
    return url


# ---------------------------------------------------------------------------
# 대상별 마이그레이션
# ---------------------------------------------------------------------------

def migrate_settings(report: Report):
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        report.warn("settings.json 을 읽지 못해 건너뜀: {0}".format(e))
        return
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    ns_rules = [(rx, repl) for rx, repl in _skill_rules()]

    allow = data.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        # 리네임과 무관한 기존 항목(중복 포함)은 그대로 보존한다 — dry-run 이 보고하지
        # 않은 변경이 apply 에서 생기면 안 됨. 리네임이 만든 중복만 제거·보고.
        new_allow = []
        for rule in allow:
            new_rule = rule
            if isinstance(rule, str):
                for rx, repl in ns_rules:
                    new_rule = rx.sub(repl, new_rule)
            if new_rule != rule:
                if new_rule in allow or new_rule in new_allow:
                    report.change(path, "permissions.allow: {0} 제거 (동일 규칙 {1} 이미 존재)"
                                  .format(rule, new_rule))
                    continue
                report.change(path, "permissions.allow: {0} -> {1}".format(rule, new_rule))
            new_allow.append(new_rule)
        data["permissions"]["allow"] = new_allow

    enabled = data.get("enabledPlugins")
    if isinstance(enabled, dict):
        new_enabled = {}
        for key, val in enabled.items():
            new_key = _rename_plugin_id(key)
            if new_key != key:
                if new_key in enabled or new_key in new_enabled:
                    # 신 키가 이미 있으면 그 값(사용자의 명시적 on/off)이 권위값 — 구 키는 버림
                    report.change(path, "enabledPlugins: {0} 제거 (신 키 {1} 이미 존재, 그 값 유지)"
                                  .format(key, new_key))
                    continue
                report.change(path, "enabledPlugins: {0} -> {1}".format(key, new_key))
            if new_key not in new_enabled:
                new_enabled[new_key] = val
        data["enabledPlugins"] = new_enabled

    markets = data.get("extraKnownMarketplaces")
    if isinstance(markets, dict):
        for old_m, new_m in MARKETPLACE_RENAMES.items():
            if old_m in markets:
                entry = markets.pop(old_m)
                if new_m not in markets:
                    markets[new_m] = entry
                report.change(path, "extraKnownMarketplaces: {0} -> {1}".format(old_m, new_m))
        for name, entry in markets.items():
            source = entry.get("source") if isinstance(entry, dict) else None
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if isinstance(url, str):
                new_url = _rewrite_url(url)
                if new_url != url:
                    source["url"] = new_url
                    report.change(path, "marketplace '{0}' URL: {1} -> {2}".format(name, url, new_url))

    after = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if after != before:
        _backup_and_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", report)


def migrate_learned_skills(report: Report):
    root = Path.home() / ".claude" / "skills"
    if not root.is_dir():
        return
    rules = _skill_rules()
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        new_text = text
        for rx, repl in rules:
            new_text = rx.sub(repl, new_text)
        if new_text != text:
            report.change(skill_md, "구 네임스페이스/경로 참조를 새 이름으로 치환")
            _backup_and_write(skill_md, new_text, report)


def migrate_codex_config(report: Report):
    path = Path.home() / ".codex" / "config.toml"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    new_text = text
    for old_p, new_p in sorted(PLUGIN_RENAMES.items(), key=lambda t: -len(t[0])):
        for old_m, new_m in MARKETPLACE_RENAMES.items():
            new_text = new_text.replace(old_p + "@" + old_m, new_p + "@" + new_m)
    for old_m, new_m in MARKETPLACE_RENAMES.items():
        new_text = new_text.replace("[marketplaces." + old_m + "]",
                                    "[marketplaces." + new_m + "]")
        # 리네임 안 된 플러그인의 '@samton-claude' 잔여 키까지 새 마켓플레이스로.
        # codex 키는 항상 '"...@마켓"' 또는 '"...@마켓:hooks/..."' 형태 — 뒤 경계를
        # ["':] 로 앵커링해 주석·이메일 등 키가 아닌 문맥의 오폭을 막는다.
        new_text = re.sub("@" + re.escape(old_m) + r'(?=["\':])',
                          "@" + new_m, new_text)
    new_text = _rewrite_url(new_text)
    if new_text != text:
        report.change(path, "plugin/marketplace 키·URL 을 새 이름으로 치환")
        report.warn("codex 훅 신뢰 해시가 새 키로 이월되지만, 다음 codex 시작 시 "
                    "재신뢰 프롬프트가 뜰 수 있습니다(정상 동작 — 승인하면 됩니다).")
        _backup_and_write(path, new_text, report)


def migrate_codex_state(report: Report):
    old = Path.home() / CODEX_STATE_DIR_OLD
    new = Path.home() / CODEX_STATE_DIR_NEW
    if old.is_dir() and not new.exists():
        report.change(old, "상태 디렉토리 이동 -> {0}".format(new))
        if report.apply:
            shutil.move(str(old), str(new))
    elif old.is_dir() and new.exists():
        report.warn("{0} 와 {1} 가 둘 다 존재합니다 — 수동 병합이 필요해 건너뜀".format(old, new))

    roots = [Path.home() / ".codex" / "skills",
             Path.home() / ".agents" / "skills",
             new / "backups",
             old / "backups"]
    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(list(root.rglob("SKILL.md")) + list(root.rglob("*.json"))):
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            new_text = text
            for old_pat, new_pat in CODEX_PROVENANCE_PATTERNS:
                new_text = new_text.replace(old_pat, new_pat)
            if new_text != text:
                report.change(f, "codex provenance 마커를 self-improving-skills 로 통일")
                _backup_and_write(f, new_text, report)


def check_marketplace_registration(report: Report):
    """재등록이 필요한지 점검만 한다 — known/installed 레지스트리는 CLI 가 관리하므로
    직접 수정하지 않고, remove/add 재등록을 안내한다."""
    plugins_dir = Path.home() / ".claude" / "plugins"
    for fname in ("known_marketplaces.json", "installed_plugins.json"):
        path = plugins_dir / fname
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for old_m in MARKETPLACE_RENAMES:
            if old_m in text:
                report.warn(
                    "{0} 에 구 마켓플레이스 '{1}' 항목이 남아 있습니다 — "
                    "`claude plugin marketplace remove {1}` 후 "
                    "`claude plugin marketplace add samton-inc/samton-plugins` 로 "
                    "재등록하고 플러그인을 새 이름으로 재설치하세요.".format(path, old_m))
                break


# ---------------------------------------------------------------------------

def run(apply: bool) -> Report:
    report = Report(apply)
    migrate_settings(report)
    migrate_learned_skills(report)
    migrate_codex_config(report)
    migrate_codex_state(report)
    check_marketplace_registration(report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="리네임으로 orphan된 로컬 상태를 최신 플러그인/마켓플레이스 이름으로 마이그레이션")
    parser.add_argument("--apply", action="store_true",
                        help="변경을 실제로 적용 (기본: dry-run 보고만)")
    args = parser.parse_args(argv)

    report = run(apply=args.apply)
    verb = "적용" if args.apply else "적용 예정(dry-run)"
    if not report.changes:
        print("[migrate] 마이그레이션할 구-이름 참조가 없습니다 (이미 최신 상태).")
    for path, desc in report.changes:
        print("[migrate:{0}] {1}: {2}".format(verb, path, desc))
    for msg in report.warnings:
        print("[migrate:주의] " + msg)
    if report.changes and not args.apply:
        print("[migrate] 위 {0}건을 적용하려면 --apply 로 다시 실행하세요. "
              "적용 시 각 파일은 .bak-migration-{1} 으로 백업됩니다.".format(
                  len(report.changes), STAMP))
    if report.changes and args.apply:
        print("[migrate] {0}건 적용 완료. settings.json 반영을 위해 Claude Code 를 "
              "재시작하세요.".format(len(report.changes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
