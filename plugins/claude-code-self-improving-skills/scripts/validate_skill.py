#!/usr/bin/env python3
"""PostToolUse-hook logic for the claude-code-self-improving-skills plugin.

Runs after every Write/Edit/MultiEdit. It only acts when the edited file is a
SKILL.md under ~/.claude/skills (a learned skill); for everything else it stays
silent. For a learned skill it:

  1. Validates the on-disk SKILL.md against the Claude Code skill contract:
       - starts with a `---` frontmatter block that closes with `---`
       - frontmatter has a non-empty `name` (<=64 chars, lowercase/digits/hyphen)
       - frontmatter has a non-empty `description` (<=1024 chars)
       - non-empty body after the frontmatter
       - whole file <= 100000 chars
     and surfaces any problems back to the agent as additionalContext so it can
     fix them immediately.
  2. Stamps provenance: if the frontmatter has no `metadata:` provenance marker,
     it injects one (`metadata: { provenance: self-improving-skills, ... }`) so
     /curate-skills and the SessionStart counter can later tell agent-distilled
     skills apart from user-authored ones. Stamping never overwrites existing
     metadata and is skipped if it can't be done cleanly.

Output contract: print a PostToolUse JSON object with
`hookSpecificOutput.additionalContext` when there's something to say; otherwise
print nothing. Fails safe to silent on any error — validation feedback must
never break the edit that already happened.
"""

import json
import os
import re
import shutil
import sys
from typing import NoReturn

import runtime_env
import sis_io
from skill_paths import backup_path, is_learned_skill, skill_name

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import usage_store
except Exception:
    usage_store = None

# Pin UTF-8 before the (Korean) validation message is written; see sis_io.
sis_io.pin_utf8_stdio()

MAX_NAME = 64
# The Claude Code contract ceiling. Existing skills are held to this so that a
# routine edit to one of the 150+ skills written before 0.18.0 never trips a
# rollback for a description it did not touch.
MAX_DESCRIPTION = 1024
# A NEW learned skill: the skill listing shows about this much per skill before
# the 1%-of-context budget cuts descriptions from the least-used skills, so a
# longer description is context spent on text the model never sees.
MAX_DESCRIPTION_NEW = 300
DESC_WARN_LEN = MAX_DESCRIPTION_NEW  # advisory for existing skills
MAX_CONTENT = 100000
# A NEW skill's body, frontmatter excluded. Compaction re-attaches at most
# ~5,000 tokens of each invoked skill; anything longer belongs in references/.
MAX_BODY_NEW = 20000
PROVENANCE_VALUE = "self-improving-skills"
# Hard charset rule (a violation BLOCKS + rolls back the edit).
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Official quick_validate also forbids trailing/consecutive hyphens — enforced
# as a non-blocking advisory only, so pre-existing learned skills with such
# names don't fall into an edit→rollback loop.
NAME_STRICT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def silent() -> NoReturn:
    sys.exit(0)


def feedback(text) -> NoReturn:
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def _rollback_if_possible(file_path):
    """Restore the pre-edit backup (made by backup_skill.py at PreToolUse).
    Returns True if a rollback happened (existing skill whose edit broke it),
    False if there was nothing to roll back to (a brand-new file)."""
    bp = backup_path(file_path)
    if not os.path.isfile(bp):
        return False
    try:
        shutil.copy2(bp, file_path)
        return True
    except Exception:
        return False


def _split_frontmatter(text):
    """Return (frontmatter_str, body_str) or (None, None) if malformed."""
    if not text.startswith("---"):
        return None, None
    # find the closing '---' on its own line after the first
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _scalar(frontmatter, key):
    """Cheap YAML scalar read for top-level `key: value` (quoted or bare)."""
    for line in frontmatter.splitlines():
        m = re.match(r"^" + re.escape(key) + r"\s*:\s*(.*)$", line)
        if m:
            val = m.group(1).strip()
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            return val
    return None


def _validate(text, *, is_new=False, previous_description=None):
    """Structural problems that make `text` unacceptable as a SKILL.md.

    `is_new` applies the 0.18.0 caps (MAX_DESCRIPTION_NEW, MAX_BODY_NEW) that
    only a brand-new skill is held to. An existing skill keeps the contract
    ceiling but may not GROW a description that is already over the cap: pass
    its pre-edit `previous_description` and the edit is refused when the
    description got longer while still over the cap. Shrinking it, or editing
    only the body, always passes — so the library written before the caps can
    still be maintained without ever being able to get worse.
    """
    problems = []
    if len(text) > MAX_CONTENT:
        problems.append("파일이 너무 큽니다(>{0}자). references/ 로 본문을 분리하세요.".format(MAX_CONTENT))

    fm, body = _split_frontmatter(text)
    if fm is None:
        problems.append("YAML frontmatter 가 없습니다. 파일은 `---` 로 시작하고 `---` 로 닫혀야 합니다.")
        return problems

    name = _scalar(fm, "name")
    if not name:
        problems.append("frontmatter 에 `name` 이 없습니다.")
    else:
        if len(name) > MAX_NAME:
            problems.append("`name` 이 {0}자를 초과합니다.".format(MAX_NAME))
        if not NAME_RE.match(name):
            problems.append("`name` 은 소문자·숫자·하이픈만 사용해야 합니다(예: my-skill-name).")

    desc = _scalar(fm, "description")
    if not desc:
        problems.append("frontmatter 에 `description` 이 없습니다. 트리거 정확도의 핵심이니 "
                        "'이럴 때 사용한다'는 상황 중심으로 한 문장 작성하세요.")
    elif is_new and len(desc) > MAX_DESCRIPTION_NEW:
        problems.append("새 스킬의 `description` 이 {0}자입니다. {1}자 이하의 한 문장으로 쓰세요 — "
                        "세션 스킬 목록은 그 길이만 보여 주고, 인접 상황 나열은 트리거를 "
                        "넓히는 게 아니라 잘리게 합니다.".format(len(desc), MAX_DESCRIPTION_NEW))
    elif len(desc) > MAX_DESCRIPTION:
        problems.append("`description` 이 {0}자를 초과합니다.".format(MAX_DESCRIPTION))
    elif (not is_new and previous_description is not None
          and len(desc) > MAX_DESCRIPTION_NEW and len(desc) > len(previous_description)):
        problems.append("`description` 이 {0}자에서 {1}자로 늘었습니다. 이미 {2}자를 넘는 설명은 "
                        "더 길어질 수 없습니다 — 줄이거나 그대로 두세요."
                        .format(len(previous_description), len(desc), MAX_DESCRIPTION_NEW))

    if not body or not body.strip():
        problems.append("frontmatter 뒤 본문(스킬 지침)이 비어 있습니다.")
    elif is_new and len(body) > MAX_BODY_NEW:
        problems.append("새 스킬의 본문이 {0}자입니다. {1}자 이하로 줄이고 나머지는 references/ 로 "
                        "옮겨 SKILL.md 에는 한 줄 포인터만 두세요 — compact 뒤 재부착은 "
                        "스킬당 약 5,000 토큰까지라 그 뒤는 잘립니다."
                        .format(len(body), MAX_BODY_NEW))

    return problems


def _advisory(text, file_path=None):
    """Non-blocking quality advisories for a VALID skill (never trips rollback)."""
    fm, _body = _split_frontmatter(text)
    if fm is None:
        return None
    notes = []
    desc = _scalar(fm, "description") or ""
    if len(desc) > DESC_WARN_LEN:
        notes.append("description이 {0}자입니다. 새 스킬이었다면 {1}자 캡에 걸려 거부됐을 길이인데 "
                     "기존 스킬이라 경고만 나갑니다. 세션 스킬 목록은 예산을 넘기면 덜 쓰는 "
                     "스킬부터 설명을 떼어 내므로, 트리거 상황 하나를 지목하는 한 문장으로 "
                     "{1}자 이하 압축을 권장합니다(늘리는 편집은 거부됩니다)."
                     .format(len(desc), DESC_WARN_LEN))
    if _body and len(_body) > MAX_BODY_NEW:
        notes.append("본문이 {0}자입니다. compact 뒤 재부착은 스킬당 약 5,000 토큰까지라 그 뒤는 "
                     "잘립니다. 세부는 references/ 로 옮기고 SKILL.md 는 라우터로 두는 것을 "
                     "권장합니다.".format(len(_body)))
    name = _scalar(fm, "name") or ""
    if name and NAME_RE.match(name) and not NAME_STRICT_RE.match(name):
        notes.append("`name`에 선행·후행·연속 하이픈이 있습니다({0}). 공식 스킬 규약 위반이니 "
                     "디렉토리명과 함께 단어-사이-하이픈 형태로 바꾸는 것을 권장합니다."
                     .format(name))
    # name ≠ dir mismatch: usage telemetry keys on the DIR name, so a mismatch
    # silently splits a skill's records. Advisory only (never blocking, so a
    # pre-existing mismatched skill can't fall into an edit→rollback loop).
    dirname = skill_name(file_path)
    if name and dirname and name != dirname:
        notes.append("frontmatter name('{0}')과 디렉토리명('{1}')이 다릅니다. usage 텔레메트리는 "
                     "디렉토리명으로 집계되므로 어긋납니다 — 디렉토리명 또는 name 을 "
                     "일치시키는 것을 권장합니다.".format(name, dirname))
    if notes:
        return "[claude-code-self-improving-skills] 참고:\n- " + "\n- ".join(notes)
    return None


def _frontmatter_has_pin(text):
    """`pinned: true` inside the CLOSED frontmatter block only — a
    `pinned: true` example in a skill's body must not count. Trailing
    inline comments (`pinned: true # keep`) are valid YAML and count."""
    fm, _ = _split_frontmatter(text or "")
    if fm is None:
        return False
    return bool(re.search(r"^\s*pinned\s*:\s*true\b", fm, re.I | re.M))


def _pinned_guard(file_path, payload, current_text):
    """C5 (Hermes 525e1e77): an AUTONOMOUS distiller edit to a pinned skill is
    rolled back — unattended maintenance has no user present to consent.
    Foreground (human-driven) edits stay allowed: same asymmetry as Hermes.

    Pinned is decided from the usage record, the PRE-edit backup's frontmatter
    (the edit itself could have stripped the marker), or — for a brand-new
    file with no backup/record — the written frontmatter itself (a distiller
    must not CREATE a curator-protected pinned skill unnoticed)."""
    agent_type = str(payload.get("agent_type") or "")
    if "skill-distiller" not in agent_type:
        return None
    name = skill_name(file_path)
    pinned = False
    if usage_store is not None:
        try:
            pinned = bool(usage_store.all_records().get(name, {}).get("pinned"))
        except Exception:
            pinned = False
    if not pinned:
        bp = backup_path(file_path)
        try:
            if os.path.isfile(bp):
                with open(bp, encoding="utf-8", errors="ignore") as fh:
                    pinned = _frontmatter_has_pin(fh.read())
            else:
                pinned = _frontmatter_has_pin(current_text)  # new file
        except Exception:
            pass
    if not pinned:
        return None
    if _rollback_if_possible(file_path):
        return ("[claude-code-self-improving-skills] '{0}' 은 pinned 스킬입니다 — 자율 증류(skill-distiller)는 "
                "pinned 스킬을 수정할 수 없어 편집 직전 버전으로 롤백했습니다. 이 변경이 정말 "
                "필요하면 내용을 사용자에게 보고하고 unpin 여부를 물어보세요.".format(name))
    # Nothing to roll back to (the distiller CREATED a new pinned skill) —
    # deleting a brand-new file is riskier than leaving it; warn only.
    return ("[claude-code-self-improving-skills] '{0}' 은 pinned 스킬입니다 — 자율 증류가 수정할 대상이 "
            "아닙니다. 변경 내용을 사용자에게 보고하고 승인/unpin 을 요청하세요.".format(name))


def _stamp_provenance(path, text):
    """Inject a provenance metadata marker if none exists. Best-effort."""
    if PROVENANCE_VALUE in text:
        return  # already stamped (or mentioned) — don't touch
    fm, body = _split_frontmatter(text)
    if fm is None or body is None:
        return
    if re.search(r"^metadata\s*:", fm, re.MULTILINE):
        return  # author already manages metadata; leave it alone
    new_fm = fm.rstrip("\n") + (
        "\nmetadata:\n"
        "  provenance: {0}\n".format(PROVENANCE_VALUE)
    )
    new_text = "---\n" + new_fm + "\n---\n" + body
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new_text)
    except Exception:
        pass


def _record_patch(file_path, text, payload):
    """Record a patch event for this learned-skill write (seeding the usage
    record on first sight).

    Patch counting lives HERE (PostToolUse) and not in the Stop-hook transcript
    scan, because this hook also fires inside subagents: the background
    skill-distiller's edits land in a separate agent transcript the Stop
    scanner never reads — counting there would let an actively-maintained
    skill look idle and get auto-archived.

    created_by precedence for seeding: the writing agent's type (hook payload
    `agent_type` — present when the hook fires inside a subagent), then an
    explicit `origin: distilled` marker in the written text, else "user"."""
    if usage_store is None:
        return
    name = skill_name(file_path)
    if not name:
        return
    agent_type = str(payload.get("agent_type") or "")
    if "skill-distiller" in agent_type or re.search(r"origin\s*:\s*distilled", text):
        created_by = "agent"
    else:
        created_by = "user"
    try:
        usage_store.apply_events([(name, "patch", created_by)])
    except Exception:
        pass


def main():
    # Inside Cowork the cowork validator runs instead — it adds the checks for
    # names and descriptions claude.ai's "스킬 저장" rejects, which only matter
    # where a skill has to travel through the account to survive.
    if runtime_env.is_cowork_runtime():
        silent()

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        silent()

    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    file_path = tool_input.get("file_path", "")
    if not is_learned_skill(file_path) or not os.path.isfile(file_path):
        silent()

    try:
        with open(file_path, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
    except Exception:
        silent()

    # Autonomous-distiller writes to a pinned skill roll back before anything
    # else — the guard supersedes validation (the edit is not allowed at all).
    try:
        guard_msg = _pinned_guard(file_path, payload, text)
    except Exception:
        guard_msg = None
    if guard_msg:
        feedback(guard_msg)

    # New or existing is decided by the PreToolUse backup: backup_skill.py
    # copies an existing file and drops any stale copy for a new one, so a
    # missing backup means this SKILL.md did not exist before the edit.
    bp = backup_path(file_path)
    is_new = not os.path.isfile(bp)
    previous_description = None
    if not is_new:
        try:
            with open(bp, encoding="utf-8", errors="ignore") as fh:
                prev_fm, _prev_body = _split_frontmatter(fh.read())
            if prev_fm is not None:
                previous_description = _scalar(prev_fm, "description") or ""
        except Exception:
            previous_description = None
    problems = _validate(text, is_new=is_new, previous_description=previous_description)
    if not problems:
        _stamp_provenance(file_path, text)
        _record_patch(file_path, text, payload)
        adv = _advisory(text, file_path)
        if adv:
            feedback(adv)
        silent()

    # 구조가 깨짐 → 편집 직전 백업이 있으면 롤백(트랜잭션 안전), 없으면(신규) 경고만.
    if _rollback_if_possible(file_path):
        msg = (
            "[claude-code-self-improving-skills] {0} 편집이 SKILL.md 구조를 깨뜨려 편집 직전 버전으로 "
            "자동 롤백했습니다. 발견된 문제:\n- ".format(file_path)
            + "\n- ".join(problems)
            + "\n원본이 복원됐으니, 위 문제를 피해 다시 편집하세요."
        )
    else:
        msg = (
            "[claude-code-self-improving-skills] 방금 작성한 학습 스킬 {0} 에 문제가 있습니다:\n- ".format(file_path)
            + "\n- ".join(problems)
            + "\n수정한 뒤 다시 저장하세요."
        )
    feedback(msg)


if __name__ == "__main__":
    main()
