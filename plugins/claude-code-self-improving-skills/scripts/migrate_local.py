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

리네임 외에, v0.12.0 의 팀 스킬 공유(TEAM SHARE) 기능 제거로 orphan 된 상태도
전환한다: skill_usage.json 의 created_by:"team" 레코드를 "agent" 로 재작성하고
(일반 스킬 라이프사이클 편입), team_sync.json 등 inert 산출물은 삭제하지 않고
안내만 한다.

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

try:
    import tomllib  # Python 3.11+ — codex config 충돌 판정·치환 결과 검증용
except ModuleNotFoundError:
    tomllib = None  # 3.10 이하 — codex config 마이그레이션은 경고 후 건너뜀

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


def _codex_rename_key(key: str) -> str:
    """키 문자열(따옴표 제거 상태) 하나를 리네임 — 키 선두의 plugin@marketplace
    구성 요소만 settings.json 과 동일한 _rename_plugin_id 로 치환한다. dict 정확
    일치라서 hooks.state 키('plugin@market:경로...')의 경로 부분, 유사 이름
    (-archive 접미 마켓, myfork-·_ 접두 플러그인), 키 내부의 따옴표/이메일 조각은
    절대 건드리지 않는다."""
    head, colon, rest = key.partition(":")
    return _rename_plugin_id(head) + colon + rest


def _rename_after_prefix(fragment: str, prefix: str) -> str:
    """prefix 정규식 바로 뒤에 오는 따옴표 세그먼트 하나만 리네임한다 — 섹션 경로
    직결 위치의 키만 대상이고, 그 뒤 하위 세그먼트(옵션 키 등)나 bare 세그먼트
    다음의 중첩 키는 기대 모델과 동일하게 보존된다."""
    rx = re.compile("(" + prefix + r")(\"[^\"\n]*\"|'[^'\n]*')")

    def repl(m):
        token = m.group(2)
        return (m.group(1) + token[0]
                + _codex_rename_key(token[1:-1]) + token[-1])

    return rx.sub(repl, fragment, count=1)


def _seg_alt(token: str) -> str:
    """헤더/키 세그먼트의 bare·따옴표 표기 변형을 모두 허용하는 정규식 조각."""
    esc = re.escape(token)
    return '(?:{0}|"{0}"|\'{0}\')'.format(esc)


# 식별자(plugin@marketplace) 키가 사는 섹션 경로 — 본문 스캐너·기대 모델·충돌
# 정리가 모두 이 상수 하나에서 파생된다 (한쪽만 고치는 사고 방지).
_CODEX_ID_SECTION_PATHS = (("plugins",), ("hooks", "state"))
_CODEX_ID_SECTIONS_ALT = "|".join(
    r"[ \t]*\.[ \t]*".join(_seg_alt(seg) for seg in parts)
    for parts in _CODEX_ID_SECTION_PATHS)
_CODEX_ID_HEADER_RE = re.compile(
    r"^[ \t]*\[\[?[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT + r")[ \t]*[.\]]")
_CODEX_ID_LHS_PREFIX_RE = re.compile(
    r"^[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT + r")[ \t]*\.")
# 루트에서 인라인 테이블로 대입되는 형태: plugins / hooks.state / hooks(중첩 state)
_CODEX_ID_INLINE_LHS_RE = re.compile(
    r"^[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT + "|" + _seg_alt("hooks") + r")[ \t]*$")
# [hooks] 테이블 문맥의 상대 표기: state."키"... / state = { "키" = ... }
_HOOKS_TABLE_HEADER_RE = re.compile(
    r"^[ \t]*\[[ \t]*" + _seg_alt("hooks") + r"[ \t]*\]")
_STATE_LHS_PREFIX_RE = re.compile(r"^[ \t]*" + _seg_alt("state") + r"[ \t]*\.")
_STATE_INLINE_LHS_RE = re.compile(r"^[ \t]*" + _seg_alt("state") + r"[ \t]*$")
# 섹션 테이블 자체([plugins] / [hooks.state])의 헤더 — 이 안의 대입 좌변 키만 id 다.
# 키가 붙은 자식 테이블([plugins."x"]) 안의 좌변은 옵션 키라 치환 대상이 아니다.
_CODEX_ID_SECTION_TABLE_RE = re.compile(
    r"^[ \t]*\[[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT + r")[ \t]*\]")
# hooks = { state = {...} } 중첩 인라인 판별용 — 좌변이 bare hooks 뿐인 형태
_HOOKS_ONLY_LHS_RE = re.compile(r"^[ \t]*" + _seg_alt("hooks") + r"[ \t]*$")
# [marketplaces] 섹션 테이블 자체 — 이 안의 대입 좌변 선두 키가 마켓 이름이다
_MARKETPLACES_TABLE_RE = re.compile(
    r"^[ \t]*\[[ \t]*" + _seg_alt("marketplaces") + r"[ \t]*\]")
# 루트 인라인 marketplaces = { 구이름 = ... } 판별용
_MARKETS_INLINE_LHS_RE = re.compile(
    r"^[ \t]*" + _seg_alt("marketplaces") + r"[ \t]*$")

# _rename_after_prefix 용 접두 패턴들 — 섹션 경로 직결 위치의 키 세그먼트만 겨냥.
# ^ 앵커 필수: 헤더 뒤 꼬리 주석 속 헤더 모양 텍스트를 치환하면 안 된다.
_ID_HEADER_PREFIX = (r"^[ \t]*\[\[?[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT
                     + r")[ \t]*\.[ \t]*")
_ID_ROOT_LHS_PREFIX = (r"^[ \t]*(?:" + _CODEX_ID_SECTIONS_ALT
                       + r")[ \t]*\.[ \t]*")
_ID_STATE_LHS_PREFIX = r"^[ \t]*" + _seg_alt("state") + r"[ \t]*\.[ \t]*"
_ID_LEADING_PREFIX = r"^[ \t]*"
def _scan_line(fragment: str, state=None):
    """TOML 한 줄을 스캔해 (주석 제외 본문, 주석, 줄 끝의 여러 줄 문자열 상태,
    따옴표 밖 대괄호 [ / ] 개수 차)를 돌려주는 미니 lexer. state 는 앞 줄에서
    열려 있던 여러 줄 문자열 구분자. 단일/삼중 따옴표, basic string 이스케이프,
    여러 줄 문자열이 4~5연속 따옴표로 끝나는(마지막 1~2개는 내용) TOML 종결
    규칙까지 추적한다. 대괄호 개수 차는 여러 줄 배열 문맥 추적용이다."""
    quote = state  # 열려 있는 따옴표 구분자: " ' 또는 삼중 따옴표
    depth = 0      # 따옴표 밖 '[' - ']' 개수 차
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if quote is not None:
            qch = quote[0]
            if qch == '"' and ch == "\\":
                i += 2
                continue
            if len(quote) == 1:
                if ch == qch:
                    quote = None
                i += 1
                continue
            if ch == qch:
                # 삼중 따옴표 안의 연속 따옴표 run — 3개 이상이면 전부(내용에
                # 속하는 최대 2개 포함) 소비하며 문자열 종료, 미만이면 내용
                run = 1
                while i + run < n and fragment[i + run] == qch:
                    run += 1
                i += run
                if run >= 3:
                    quote = None
                continue
            i += 1
            continue
        if ch in "\"'":
            quote = ch * 3 if fragment.startswith(ch * 3, i) else ch
            i += len(quote)
            continue
        if ch == "#":
            return fragment[:i], fragment[i:], None, depth
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        i += 1
    # 한 줄 따옴표는 TOML 에서 줄을 넘지 못한다 — 여러 줄 구분자만 다음 줄로 이월
    return (fragment, "",
            (quote if quote is not None and len(quote) == 3 else None), depth)


def _find_unquoted(fragment: str, target: str) -> int:
    """따옴표 밖에서 처음 나오는 target 문자의 인덱스 (없으면 -1) — 따옴표 키
    내부의 '=' 등이 구분자로 오인되지 않게 한다."""
    quote = None
    i = 0
    n = len(fragment)
    while i < n:
        ch = fragment[i]
        if quote is not None:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == target:
            return i
        i += 1
    return -1


def _rename_inline_table_keys(body: str, mode: str = "direct") -> str:
    """인라인 테이블 조각에서 대상 깊이의 '직접 키'만 리네임한다.
    mode:
      - "direct": 최상위 직접 키를 _codex_rename_key 로 (plugins = {...} /
        hooks.state = {...} / [hooks] 안의 state = {...})
      - "hooks":  hooks = { state = {...} } 표기 — state 하위 직접 키만
      - "markets": marketplaces = {...} — 직접 키(bare 포함)를 마켓 이름 규칙으로
    키 위치 판정(직전 비공백 { , . + 직후 비공백 = .)에 더해 중괄호 깊이와 부모
    키를 스택으로 추적해 무관한 중첩 테이블(옵션·metadata·aliases 등)의 키는
    건드리지 않고, 문자열 값은 이스케이프 인지 스캔으로 통째 소비해 보존한다."""
    out = []
    i = 0
    n = len(body)
    prev = "{"        # 조각 선두는 키 위치로 취급 ('plugins =' 좌변 바로 뒤)
    stack = []        # 중괄호 스택 — 각 원소는 그 테이블을 연 부모 키 이름
    last_key = None   # 직전에 본 키/단어 (다음 '{' 의 부모 후보)
    dot_parent = None  # 직전 '.' 앞의 키 이름 — state."키" 점 표기 판별용
    word = ""         # bare 키 추적 버퍼 (flush 시점에 출력·리네임)

    def rename_here():
        if mode == "hooks":
            if len(stack) == 2 and stack[1] == "state":
                return prev in "{,"
            # hooks = { state."키".trusted = ... } 점 표기 — state 직후 세그먼트만
            return len(stack) == 1 and prev == "." and dot_parent == "state"
        return len(stack) == 1 and prev in "{,"

    def rename_key(key):
        return (_marketplace_rename(key) if mode == "markets"
                else _codex_rename_key(key))

    def flush_word(next_pos):
        """버퍼된 bare 단어를 출력 — markets 모드의 직접 키 위치면 리네임."""
        nonlocal word, last_key
        if not word:
            return
        k = next_pos
        while k < n and body[k] in " \t":
            k += 1
        nxt = body[k] if k < n else ""
        if mode == "markets" and nxt in "=." and rename_here():
            out.append(_marketplace_rename(word))
        else:
            out.append(word)
        last_key = word
        word = ""

    while i < n:
        ch = body[i]
        if ch in "\"'":
            flush_word(i)
            if body.startswith(ch * 3, i):
                # 한 줄 안에서 닫히는 여러 줄 문자열 — 이스케이프 인지 통째 소비
                j = i + 3
                while j < n:
                    if ch == '"' and body[j] == "\\":
                        j += 2
                        continue
                    if body.startswith(ch * 3, j):
                        j += 3
                        while j < n and body[j] == ch:  # 내용 따옴표 종결
                            j += 1
                        break
                    j += 1
                else:
                    j = n
                out.append(body[i:j])
                i = j
                prev = ch
                continue
            j = i + 1
            while j < n:
                if ch == '"' and body[j] == "\\":
                    j += 2
                    continue
                if body[j] == ch:
                    break
                j += 1
            if j >= n:  # 짝 없는 따옴표 — 남은 조각 그대로
                out.append(body[i:])
                break
            token = body[i:j + 1]
            k = j + 1
            while k < n and body[k] in " \t":
                k += 1
            nxt = body[k] if k < n else ""
            if nxt in "=." and rename_here():
                token = token[0] + rename_key(token[1:-1]) + token[-1]
            last_key = token[1:-1]
            out.append(token)
            prev = ch
            i = j + 1
            continue
        if ch == "{":
            flush_word(i)
            stack.append(last_key)
            last_key = None
            dot_parent = None
            prev = ch
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            flush_word(i)
            if stack:
                stack.pop()
            dot_parent = None
            prev = ch
            out.append(ch)
            i += 1
            continue
        if ch.isalnum() or ch in "_-":
            word += ch
            i += 1
            continue
        flush_word(i)
        if ch == ".":
            dot_parent = last_key
        elif ch == ",":
            dot_parent = None
        if ch not in " \t":
            prev = ch
        out.append(ch)
        i += 1
    flush_word(n)
    return "".join(out)


def _codex_rename_ids(text: str) -> str:
    """본문의 식별자 치환을 plugins/hooks.state 키 문맥에만 적용 — 해당 섹션의
    테이블 헤더 대괄호부, 대입문 좌변(루트 점 키·[hooks] 아래 state.* 상대 표기
    포함), plugins/hooks.state/hooks 에 대입되는 인라인 테이블의 키 위치만
    치환한다. 그 외 섹션(mcp_servers 등)·다른 테이블 아래의 상대 점 키·문자열 값·
    주석(전체/인라인)·여러 줄 문자열 내용(라인 간 상태 이월 추적)은 건드리지
    않는다."""
    out = []
    in_id_section = False   # 현재 테이블이 [plugins]/[hooks.state] 섹션 자체인지
                            # (키 붙은 자식 테이블의 좌변은 옵션 키 — 치환 금지)
    in_hooks_table = False  # 현재 테이블이 [hooks] 라 state.* 상대 표기가 가능한지
    in_markets_table = False  # 현재 테이블이 [marketplaces] 섹션 자체인지
    at_root = True          # 아직 어떤 테이블 헤더도 지나지 않은 루트 문맥인지
    ml_state = None         # 앞 줄에서 열려 있는 여러 줄 문자열 구분자
    arr_depth = 0           # 여러 줄 배열 중첩 깊이 — 요소 라인은 헤더가 아니다
    for raw in text.splitlines(keepends=True):
        _b, _c, new_ml, delta = _scan_line(raw, ml_state)
        if ml_state is not None or arr_depth > 0:
            # 여러 줄 문자열 내용/배열 요소 — 문맥 전환도 치환도 하지 않는다
            out.append(raw)
            ml_state = new_ml
            arr_depth = max(0, arr_depth + delta)
            continue
        line = raw
        stripped = line.lstrip()
        # 헤더 판정은 전체 라인 패턴으로 — 여러 줄 배열 요소의 '[...]' 라인을
        # 테이블 헤더로 오인해 문맥을 잃지 않게 한다(배열 깊이는 위에서 차단).
        if _TABLE_HEADER_LINE_RE.match(line) is not None:
            at_root = False
            in_id_section = _CODEX_ID_SECTION_TABLE_RE.match(line) is not None
            in_hooks_table = _HOOKS_TABLE_HEADER_RE.match(line) is not None
            in_markets_table = _MARKETPLACES_TABLE_RE.match(line) is not None
            if _CODEX_ID_HEADER_RE.match(line) is not None:
                line = _rename_after_prefix(line, _ID_HEADER_PREFIX)
            # 마켓플레이스 헤더 세그먼트 리네임 — 표기 변형·하위 경로 포함.
            # 여러 줄 문자열 안의 헤더 모양 라인은 위 상태 추적이 이미 차단한다.
            for old_m, new_m in MARKETPLACE_RENAMES.items():
                line = re.sub(
                    r"^([ \t]*\[\[?[ \t]*" + _seg_alt("marketplaces")
                    + r"[ \t]*\.[ \t]*)" + _seg_alt(old_m)
                    + r"(?=[ \t]*[\].])",
                    r"\g<1>" + new_m, line, count=1)
        elif not stripped.startswith("#"):
            eq_at = _find_unquoted(line, "=")
            if eq_at >= 0:
                lhs, eq, rhs = line[:eq_at], line[eq_at], line[eq_at + 1:]
                if in_id_section:
                    lhs = _rename_after_prefix(lhs, _ID_LEADING_PREFIX)
                elif (at_root
                        and _CODEX_ID_LHS_PREFIX_RE.match(lhs) is not None):
                    lhs = _rename_after_prefix(lhs, _ID_ROOT_LHS_PREFIX)
                elif (in_hooks_table
                        and _STATE_LHS_PREFIX_RE.match(lhs) is not None):
                    lhs = _rename_after_prefix(lhs, _ID_STATE_LHS_PREFIX)
                elif in_markets_table:
                    # [marketplaces] 테이블의 선두 키(bare/따옴표) — 마켓 이름.
                    # lhs 는 '=' 앞에서 잘린 조각이라 끝($)도 경계로 허용한다.
                    for old_m, new_m in MARKETPLACE_RENAMES.items():
                        lhs = re.sub(r"^([ \t]*)" + _seg_alt(old_m)
                                     + r"(?=[ \t]*(?:\.|$))",
                                     r"\g<1>" + new_m, lhs, count=1)
                elif at_root:
                    # 루트 점 키 marketplaces.구이름.…
                    for old_m, new_m in MARKETPLACE_RENAMES.items():
                        lhs = re.sub(r"^([ \t]*" + _seg_alt("marketplaces")
                                     + r"[ \t]*\.[ \t]*)" + _seg_alt(old_m)
                                     + r"(?=[ \t]*(?:\.|$))",
                                     r"\g<1>" + new_m, lhs, count=1)
                if ((at_root and _CODEX_ID_INLINE_LHS_RE.match(lhs) is not None)
                        or (in_hooks_table
                            and _STATE_INLINE_LHS_RE.match(lhs) is not None)):
                    # 인라인 테이블 — 대상 깊이의 키 위치만, 따옴표 밖 주석 앞까지만
                    body, comment, _st, _d = _scan_line(rhs)
                    nested = (at_root
                              and _HOOKS_ONLY_LHS_RE.match(lhs) is not None)
                    rhs = (_rename_inline_table_keys(
                        body, "hooks" if nested else "direct") + comment)
                elif (at_root
                        and _MARKETS_INLINE_LHS_RE.match(lhs) is not None):
                    # marketplaces = { 구이름 = ... } 인라인 — 직접 키만
                    body, comment, _st, _d = _scan_line(rhs)
                    rhs = _rename_inline_table_keys(body, "markets") + comment
                line = lhs + eq + rhs
        out.append(line)
        ml_state = new_ml
        arr_depth = max(0, arr_depth + delta)
    return "".join(out)


def _marketplace_rename(key: str) -> str:
    return MARKETPLACE_RENAMES.get(key, key)


def _toml_table(data, *keys):
    """중첩 dict 에서 keys 경로의 테이블을 얻는다 — 없거나 dict 가 아니면 빈 dict."""
    for k in keys:
        data = data.get(k) if isinstance(data, dict) else None
    return data if isinstance(data, dict) else {}


def _codex_expected(value, path=()):
    """치환 적용 후 기대되는 파싱 결과 모델. 재파싱 결과와 문서 전체를 통째로
    비교하는 안전망의 기준값 — 블록 삭제/치환이 일으킨 예상 밖 구조 변화(무관한
    테이블 생성, 자식 테이블 잔존, 값 훼손)는 전부 불일치로 걸려 쓰기가 보류된다.
    수렴 충돌은 최종 키와 같은 키(기존 신 테이블)가 있을 때만 그 값을 채택한다 —
    없는 경우는 migrate_codex_config 가 이 모델을 쓰기 전에 경고 후 중단한다."""
    if isinstance(value, dict):
        # 키 리네임은 대상 섹션에서만 — 그 외 섹션(mcp_servers 등)의 키는 유지
        if path == ("marketplaces",):
            rename = _marketplace_rename
        elif path in _CODEX_ID_SECTION_PATHS:
            rename = _codex_rename_key
        else:
            rename = None
        groups = {}
        for key in value:
            groups.setdefault(rename(key) if rename else key, []).append(key)
        out = {}
        for new_key, members in groups.items():
            src = new_key if new_key in members else min(members)
            out[new_key] = _codex_expected(value[src], path + (src,))
        return out
    if isinstance(value, list):
        return [_codex_expected(v, path) for v in value]
    if isinstance(value, str):
        # 문자열 값은 URL 치환만 — 식별자 치환은 키 문맥 한정 (_codex_rename_ids)
        return _rewrite_url(value)
    return value


def _toml_equal(a, b) -> bool:
    """파싱 결과의 의미 동등 비교 — dict/list 는 재귀, float 는 nan==nan 허용,
    스칼라는 타입까지 일치해야 한다(True == 1 같은 파이썬 동등성 오판 방지)."""
    if isinstance(a, dict):
        return (isinstance(b, dict) and a.keys() == b.keys()
                and all(_toml_equal(v, b[k]) for k, v in a.items()))
    if isinstance(a, list):
        return (isinstance(b, list) and len(a) == len(b)
                and all(_toml_equal(x, y) for x, y in zip(a, b)))
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return a == b or (a != a and b != b)
    return a == b


# 테이블 헤더로 볼 수 있는 라인 — 여러 줄 배열/문자열 안의 '[' 시작 라인과 구분하기
# 위해 라인 전체가 [ ... ] (+ 주석) 꼴일 때만 경계로 인정한다. 그래도 남는 오인
# 케이스는 기대 모델 비교가 최종적으로 잡는다.
_TABLE_HEADER_LINE_RE = re.compile(r"^[ \t]*\[[^\n]*\][ \t]*(?:#.*)?$", re.MULTILINE)


def _toml_header_core(parts, key: str, key_quoted: bool,
                      descendant: bool = False) -> str:
    """[plugins."key"] / [hooks.state."key"] / [marketplaces.name] 헤더의 정규식
    조각 — 각 세그먼트의 공백·따옴표 표기 변형까지 허용한다. key_quoted 는 키에
    @ / : 가 있어 반드시 따옴표가 필요한 경우(True), 아니면 bare 표기도 허용.
    descendant=True 면 그 키의 자식 테이블([...key.하위...]) 헤더를 잡는다."""
    def seg(token, allow_bare):
        esc = re.escape(token)
        pat = '(?:{0}|"{0}"|\'{0}\')' if allow_bare else '(?:"{0}"|\'{0}\')'
        return pat.format(esc)
    pieces = [seg(p, True) for p in parts] + [seg(key, not key_quoted)]
    inner = r"[ \t]*\.[ \t]*".join(pieces)
    if descendant:
        return r"\[\[?[ \t]*" + inner + r"[ \t]*\.[^\n]*\]"
    return r"\[[ \t]*" + inner + r"[ \t]*\]"


def _rewrite_urls_outside_comments(text: str) -> str:
    """주석(전체/꼬리)을 제외한 본문과 여러 줄 문자열 내용에만 _rewrite_url 적용 —
    주석 속 URL 은 사용자가 쓴 그대로 보존한다."""
    out = []
    ml_state = None
    for line in text.splitlines(keepends=True):
        body, comment, ml_state, _d = _scan_line(line, ml_state)
        out.append(_rewrite_url(body) + comment)
    return "".join(out)


def _delete_toml_table(text: str, header_core: str) -> str:
    """헤더로 시작하는 테이블 블록(다음 테이블 헤더 직전까지)을 제거. 블록 안의
    사용자 주석은 단독 라인이든 헤더/값 라인의 꼬리 주석이든 삭제하지 않고 남기고,
    여러 줄 문자열 내용의 '#' 모양 라인은 주석으로 오인하지 않는다. 헤더를 못
    찾으면 원문 그대로 — 이후 기대 모델 비교가 중복 선언을 잡아 쓰기를 보류한다."""
    m = re.search(r"^[ \t]*" + header_core + r"[ \t]*(?:#.*)?\n?",
                  text, re.MULTILINE)
    if m is None:
        return text
    nxt = _TABLE_HEADER_LINE_RE.search(text, m.end())
    end = nxt.start() if nxt is not None else len(text)
    span = text[m.start():end].splitlines(keepends=True)
    kept = []
    ml = None
    for line in span:
        _body, comment, new_ml, _d = _scan_line(line, ml)
        if ml is None and line.lstrip().startswith("#"):
            kept.append(line)  # 단독 주석 라인 그대로
        elif comment:
            # 헤더/값 라인의 꼬리 주석은 단독 주석 라인으로 승격해 보존
            kept.append(comment if comment.endswith("\n") else comment + "\n")
        ml = new_ml
    if span and span[-1].strip() == "" and not span[-1].lstrip().startswith("#"):
        kept.append(span[-1])  # 다음 블록과의 구분 공백 라인 유지
    return text[:m.start()] + "".join(kept) + text[end:]


def migrate_codex_config(report: Report) -> bool:
    """codex config.toml 마이그레이션. 반환값은 상태가 정합적인지 여부 —
    False(보류·실패)면 호출측이 상태 디렉토리 이동 등 후속 codex 마이그레이션도
    함께 보류해 설정·상태 불일치를 막는다."""
    path = Path.home() / ".codex" / "config.toml"
    if not path.exists():
        return True
    if tomllib is None:
        # 파서 없이는 치환 결과를 검증할 수 없다 — 휴리스틱으로 추측해 갈라지는
        # 대신 codex config 와 상태 마이그레이션을 통째로 보류하고 전제 조건을
        # 안내한다 (안내 문구의 오탐/미탐이 동작에 영향을 주지 않도록).
        report.warn("codex config.toml 이 있으나 tomllib(Python 3.11+) 없이는 "
                    "검증할 수 없어 codex 마이그레이션을 보류합니다 — 최신 "
                    "python3 으로 다시 실행하세요.")
        return False
    text = path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        report.warn("codex config.toml 이 유효한 TOML 이 아니어서 건너뜀 "
                    "(수동 복구 필요): {0}".format(e))
        return False

    # 리네임 결과 이름의 테이블이 이미 선언돼 있으면, 구 섹션을 그대로 개명할 경우
    # 같은 테이블이 2번 선언되어 TOML 파싱이 깨진다("Cannot declare ... twice").
    # 구 섹션은 개명 대신 삭제한다 — 신 섹션 값이 권위값 (settings.json 의
    # enabledPlugins 처리와 같은 원칙). 충돌 판정은 주석·문자열 속 헤더 모양 텍스트에
    # 오폭하지 않도록 파싱 결과의 실제 테이블 키로만 한다.
    sections = ([(parts, _codex_rename_key, True)
                 for parts in _CODEX_ID_SECTION_PATHS]
                + [(("marketplaces",), _marketplace_rename, False)])
    pruned = text
    pending = []  # 검증 통과 후에만 report 로 내보낼 변경 설명
    for parts, rename, key_quoted in sections:
        tbl = _toml_table(parsed, *parts)
        groups = {}
        for key in tbl:
            groups.setdefault(rename(key), []).append(key)
        for new_key, members in sorted(groups.items()):
            if len(members) < 2:
                continue
            if new_key not in members:
                # 어느 쪽도 완전한 최종 키가 아닌 수렴 충돌 — 권위값을 판정할 수
                # 없으므로 임의로 고르지 않고 통째로 보류한다.
                report.warn('codex config.toml 의 {0} 키 {1} 이 같은 최종 키 "{2}" '
                            "로 수렴하지만 권위값을 판정할 수 없어 건너뜀 (수동 정리 "
                            "필요)".format(
                                ".".join(parts),
                                " / ".join('"{0}"'.format(k)
                                           for k in sorted(members)),
                                new_key))
                return False
            # 신 테이블이 이미 있는 충돌 — 구 키들을 삭제하고 신 값을 유지한다.
            for key in sorted(members):
                if key == new_key:
                    continue
                pruned = _delete_toml_table(
                    pruned, _toml_header_core(parts, key, key_quoted))
                # 구 키의 자식 테이블([섹션."구키".하위])도 함께 제거 — 남기면
                # 개명 후 신 키 아래로 되살아나 기대 모델과 어긋난다
                while True:
                    reduced = _delete_toml_table(
                        pruned, _toml_header_core(parts, key, key_quoted,
                                                  descendant=True))
                    if reduced == pruned:
                        break
                    pruned = reduced
                pending.append('{0}."{1}" 제거 (신 테이블 "{2}" 이미 존재, 그 값 유지)'
                               .format(".".join(parts), key, new_key))

    # 마켓플레이스 헤더 리네임 포함 모든 키 문맥 치환은 _codex_rename_ids 안에서
    # 라인 상태(여러 줄 문자열·배열) 추적 하에 수행되고, URL 치환은 주석을 제외한다.
    new_text = _rewrite_urls_outside_comments(_codex_rename_ids(pruned))
    expected = _codex_expected(parsed)
    if new_text == text:
        # 파싱상 리네임/충돌 정리가 필요한데 본문에서 표현하지 못한 경우(이스케이프
        # 표기 키 등) — 조용히 지나치지 않고 보류를 알린다.
        if not _toml_equal(parsed, expected):
            report.warn("codex config.toml 에 리네임이 필요한 키가 있으나 본문에서 "
                        "찾지 못해 변경을 보류함 (수동 확인 필요 — 이스케이프 표기 "
                        "키 등)")
            return False
        return True

    # 안전망: 치환 결과가 TOML 로 파싱되고 문서 전체가 기대 모델과 일치할 때만
    # 쓴다. 어긋나면 쓰지 않고 경고만 — 잘못 쓰면 codex 기동 장애로 이어지는 파일.
    try:
        reparsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        report.warn("codex config.toml 치환 결과가 TOML 파싱에 실패해 변경을 보류함 "
                    "(수동 확인 필요): {0}".format(e))
        return False
    if not _toml_equal(reparsed, expected):
        report.warn("codex config.toml 치환 결과가 기대 구성과 달라 변경을 보류함 "
                    "(수동 확인 필요)")
        return False

    for desc in pending:
        report.change(path, desc)
    if new_text != pruned:
        report.change(path, "plugin/marketplace 키·URL 을 새 이름으로 치환")
        if (set(_toml_table(expected, "hooks", "state"))
                - set(_toml_table(parsed, "hooks", "state"))):
            # 새 이름의 훅 키가 실제로 생기는(이월되는) 경우에만 재신뢰 안내 —
            # 신 키가 이미 있던 충돌 정리는 이월이 아니므로 안내하지 않는다
            report.warn("codex 훅 신뢰 해시가 새 키로 이월되지만, 다음 codex 시작 시 "
                        "재신뢰 프롬프트가 뜰 수 있습니다(정상 동작 — 승인하면 됩니다).")
    _backup_and_write(path, new_text, report)
    return True


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


def sunset_team_share(report: Report):
    """v0.12.0 에서 팀 스킬 공유(TEAM SHARE: /share-skill, /sync-team-skills)가
    제거되었다. 팀 소유권 개념이 사라졌으므로 skill_usage.json 의
    created_by:"team" 레코드를 "agent" 로 재작성해 일반(증류) 스킬
    라이프사이클(큐레이션 대상)로 편입한다. team_sync.json 등 나머지 산출물은
    이제 읽는 코드가 없는 inert 파일 — 사용자 데이터라 삭제하지 않고 안내만."""
    state = Path.home() / ".claude" / "self-improve"
    usage = state / "skill_usage.json"
    if usage.is_file():
        try:
            data = json.loads(usage.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            hit = sorted(n for n, rec in data.items()
                         if isinstance(rec, dict) and rec.get("created_by") == "team")
            if hit:
                for n in hit:
                    data[n]["created_by"] = "agent"
                report.change(usage,
                              "created_by:team 레코드 {0}건({1})을 agent 로 전환 "
                              "(팀 공유 기능 제거)".format(len(hit), ", ".join(hit)))
                _backup_and_write(
                    usage, json.dumps(data, ensure_ascii=False, indent=2) + "\n", report)
    for name in ("team_sync.json", "team_config.json", "team_quarantine"):
        p = state / name
        if p.exists():
            report.warn("{0} 은 팀 공유 기능 제거(v0.12.0)로 더 이상 사용되지 "
                        "않습니다 — 수동으로 삭제해도 안전합니다.".format(p))


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
    if migrate_codex_config(report):
        migrate_codex_state(report)
    else:
        # 설정이 구 이름인 채 상태 디렉토리만 옮기면 구 플러그인이 상태를 잃는다
        # — 설정 마이그레이션이 보류되면 상태 이동·provenance 정리도 함께 보류.
        report.warn("codex config.toml 마이그레이션이 보류되어 상태 디렉토리 이동과 "
                    "provenance 정리도 함께 보류합니다 (설정·상태 불일치 방지).")
    sunset_team_share(report)
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
    if not report.changes and not report.warnings:
        # 경고(보류·수동 확인 필요)가 있으면 '최신 상태' 로 오인시키지 않는다
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
