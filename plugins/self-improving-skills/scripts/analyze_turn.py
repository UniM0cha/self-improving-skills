#!/usr/bin/env python3
"""Stop-hook analyzer for the self-improving-skills plugin.

Reads the Claude Code Stop-hook payload on stdin, measures how many tool calls
have accumulated since the last skill-distillation "anchor", and emits a
Stop-hook decision on stdout. If the work since the last distillation looks
substantial enough and nothing has been distilled, it BLOCKs and instructs the
agent to delegate to the skill-distiller subagent. Otherwise it APPROVEs.

Design notes — every one of these avoids a confirmed failure mode of the
sibling dev-log hook (which never fired across 396 real transcripts):

  * Tool calls are detected via the REAL transcript shape — an `assistant` row
    whose `message.content[]` contains `{"type":"tool_use","name":...}`.
    (dev-log grepped for `"tool":"Edit"`, which matches 0 transcripts; the real
    key is `"name":"Edit"`.)
  * "Already distilled?" is decided by an ACTUAL action — a Task delegation to
    skill-distiller, or a Write/Edit of a SKILL.md under ~/.claude/skills — not
    by a substring match on the word "distill"/the plugin name, which would
    self-trip because the plugin's own name/paths are injected into every
    transcript.
  * The block decision is emitted as JSON on STDOUT with exit 0 (the contract
    Claude Code actually parses), NOT on stderr with exit 2.
  * `stop_hook_active` is honored as a loop guard so we never re-block our own
    block.
  * Any error fails safe to APPROVE — the hook must never wedge a session shut.

Config:
  SIS_DISTILL_THRESHOLD  tool calls since last distill required to nudge (default 12)
  SIS_MIN_FILE_EDITS     min real file edits (Edit/Write/MultiEdit/NotebookEdit)
                         since last distill, so pure read/search turns don't nudge (default 2)
"""

import json
import os
import sys
from typing import NoReturn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import usage_store
except Exception:
    usage_store = None  # telemetry is best-effort; nudge logic works without it

SKILL_MARKER = "skill-distiller"
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SKILLS_DIR = os.path.expanduser("~/.claude/skills")


def emit(obj) -> NoReturn:
    """Write a Stop-hook decision to stdout and exit 0."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def approve() -> NoReturn:
    emit({"decision": "approve"})


def _int_env(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _tool_uses(row):
    """Yield tool_use blocks from an assistant row (real transcript shape)."""
    if not isinstance(row, dict) or row.get("type") != "assistant":
        return
    msg = row.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _is_skill_path(file_path):
    """True if a path is a SKILL.md inside a ~/.claude/skills tree."""
    norm = str(file_path or "").replace("\\", "/")
    return "/.claude/skills/" in norm and norm.endswith("SKILL.md")


def _is_plugin_source_path(file_path):
    """True if a path is inside the self-improving-skills plugin's OWN source
    tree (dev checkout, marketplace clone, or plugin cache) — NOT a distilled
    skill under ~/.claude/skills. Used to surface "you touched the plugin core"
    so the improvement can be routed upstream instead of (only) into skills.

    Matching on the "/self-improving-skills/" path segment covers every install
    location. The state dir "~/.claude/self-improve/" has a different name and
    does not match. A distilled skill that happens to be named
    "self-improving-skills" would live under "/.claude/skills/", so we exclude
    that first.
    """
    norm = str(file_path or "").replace("\\", "/")
    if "/.claude/skills/" in norm:
        return False
    if "/sis-pr-" in norm:
        # our own temp PR clone (propose_plugin_pr.py mkdtemp prefix) — editing
        # files there is the L2 flow itself, not a fresh edit of the user's tree
        return False
    return "/self-improving-skills/" in norm


def _is_core_pr_action(command):
    """True if a Bash command handled a core change via a PR — submitting through
    the L2 helper, or creating a PR directly. Seeing this AFTER a core edit clears
    the core-touch advisory so the L1 notice doesn't keep re-firing on the same
    edit. We match the `submit` subcommand specifically (not `prepare`, which only
    clones) so a prepared-but-not-yet-submitted change still surfaces.
    """
    c = str(command or "")
    return "propose_plugin_pr.py submit" in c or "gh pr create" in c


def _learned_skill_names():
    """Names of learned skills = immediate dirs under ~/.claude/skills with a SKILL.md."""
    names = set()
    try:
        for entry in os.listdir(SKILLS_DIR):
            if entry.startswith("."):
                continue
            if os.path.isfile(os.path.join(SKILLS_DIR, entry, "SKILL.md")):
                names.add(entry)
    except Exception:
        pass
    return names


def _skill_name_from_path(file_path):
    """The skill name for a ~/.claude/skills/<name>/SKILL.md path (dir basename)."""
    if not _is_skill_path(file_path):
        return None
    norm = str(file_path).replace("\\", "/")
    return os.path.basename(os.path.dirname(norm)) or None


def _capture_telemetry(rows, session_id):
    """Best-effort: bump use/view/patch counters for learned skills from new
    transcript rows (since this session's last processed offset). Signals
    (verified against real transcripts):
      - Skill tool, input.skill (namespace-stripped) matches a learned skill -> use
      - Read of a ~/.claude/skills/**/SKILL.md                                -> view
      - Write/Edit/MultiEdit of the same                                      -> patch
    """
    if usage_store is None:
        return
    learned = _learned_skill_names()
    try:
        usage_store.forget_missing(learned)
    except Exception:
        pass

    offset = 0
    try:
        offset = usage_store.get_offset(session_id)
    except Exception:
        offset = 0
    if offset < 0 or offset > len(rows):
        offset = 0

    events = []
    if learned:
        for row in rows[offset:]:
            for tu in _tool_uses(row):
                name = tu.get("name")
                raw_inp = tu.get("input")
                inp = raw_inp if isinstance(raw_inp, dict) else {}
                if name == "Skill":
                    sk = str(inp.get("skill", "")).split(":")[-1]
                    if sk in learned:
                        events.append((sk, "use", "agent"))
                elif name == "Read":
                    sn = _skill_name_from_path(inp.get("file_path"))
                    if sn in learned:
                        events.append((sn, "view", "agent"))
                elif name in EDIT_TOOLS:
                    sn = _skill_name_from_path(inp.get("file_path"))
                    if sn in learned:
                        events.append((sn, "patch", "agent"))
    try:
        usage_store.apply_events(events, session_id, len(rows))
    except Exception:
        pass


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        approve()

    # Loop guard: never re-block inside the same Stop cycle.
    if payload.get("stop_hook_active"):
        approve()

    path = payload.get("transcript_path") or ""
    if not path or not os.path.isfile(path):
        approve()

    threshold = _int_env("SIS_DISTILL_THRESHOLD", 12)
    min_edits = _int_env("SIS_MIN_FILE_EDITS", 2)

    rows = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        approve()

    # Telemetry capture (best-effort, isolated): record skill use/view/patch from
    # new transcript rows. Never let this affect the nudge decision below.
    try:
        session_id = str(payload.get("session_id") or os.path.basename(path))
        _capture_telemetry(rows, session_id)
    except Exception:
        pass

    # Anchor = the last index at which a distillation ALREADY happened, i.e.
    #   (a) a subagent delegation to skill-distiller, or
    #   (b) a Write/Edit/MultiEdit whose file_path is a ~/.claude/skills SKILL.md.
    # Everything after the anchor is "work not yet distilled".
    #
    # NOTE: the subagent-spawning tool is named differently across Claude Code
    # surfaces ("Task" in the docs, "Agent" in some runtimes), and the
    # subagent_type may carry a plugin namespace prefix
    # ("self-improving-skills:skill-distiller"). So we key on the *presence of a
    # subagent_type input* containing the distiller marker — environment- and
    # name-agnostic — rather than hardcoding the tool name. (Getting this wrong
    # is exactly the class of silent-mismatch bug dev-log hit; verified against a
    # real transcript where the tool name was "Agent", not "Task".)
    anchor = -1
    for i, row in enumerate(rows):
        for tu in _tool_uses(row):
            name = tu.get("name")
            raw_inp = tu.get("input")
            inp = raw_inp if isinstance(raw_inp, dict) else {}
            subagent_type = str(inp.get("subagent_type", ""))
            if SKILL_MARKER in subagent_type:
                anchor = i
            elif name in EDIT_TOOLS and _is_skill_path(inp.get("file_path")):
                anchor = i

    # Count tool calls and real file edits since the anchor.
    total_calls = 0
    file_edits = 0
    core_touched = False  # did this segment edit the plugin's OWN source?
    for row in rows[anchor + 1:]:
        for tu in _tool_uses(row):
            total_calls += 1
            name = tu.get("name")
            raw_inp = tu.get("input")
            inp = raw_inp if isinstance(raw_inp, dict) else {}
            if name in EDIT_TOOLS:
                file_edits += 1
                if _is_plugin_source_path(inp.get("file_path")):
                    core_touched = True
            elif name == "Bash" and _is_core_pr_action(inp.get("command")):
                # core change was handled via a PR -> clear the advisory so it
                # doesn't keep re-firing on the same old edit in later Stops
                core_touched = False

    # Two independent triggers, merged into one block message:
    #   (1) nudge_fires — the undistilled segment is BOTH substantial (enough
    #       tool calls) AND has real artifacts (enough file edits). Keeps pure
    #       exploration/Q&A turns from triggering while staying broad across all
    #       kinds of work (unlike dev-log's compiled-build-only trigger).
    #   (2) core_touched — this segment edited the plugin's OWN source. Worth
    #       surfacing even for a tiny edit so the improvement can flow upstream
    #       (the "L1" advisory). PURELY INFORMATIONAL — it never auto-acts; the
    #       actual PR is opt-in and human-gated via /propose-plugin-improvement.
    nudge_fires = total_calls >= threshold and file_edits >= min_edits
    if nudge_fires or core_touched:
        parts = []
        if nudge_fires:
            parts.append((
                "이번 작업 구간에서 도구 호출이 {calls}회(파일 편집 {edits}회) 누적됐고 "
                "아직 스킬로 증류되지 않았습니다. 종료하기 전에 /distill-skill 을 실행하거나 "
                'self-improving-skills:skill-distiller 서브에이전트'
                '(subagent_type="self-improving-skills:skill-distiller" — 플러그인 네임스페이스 '
                '접두사를 빼면 호출이 실패함)를 호출해, '
                "이 세션에서 얻은 재사용 가능한 기법·패턴·해결책을 ~/.claude/skills 의 "
                "SKILL.md 로 캡처하세요.\n\n"
                "원칙:\n"
                "- 이미 관련된 기존 스킬이 있으면 새로 만들지 말고 그 SKILL.md 를 patch 하세요.\n"
                "- 한 번 쓰고 버릴 일회성 작업(특정 PR·특정 버그·환경 의존적 우회)이라면 "
                "캡처하지 말고 그대로 종료하세요.\n"
                "- 증류가 불필요하다고 판단되면, 그 이유를 사용자에게 한 줄로 알린 뒤 종료하세요."
            ).format(calls=total_calls, edits=file_edits))
        if core_touched:
            parts.append(
                "이번 구간이 self-improving-skills 코어 소스를 직접 수정했습니다. "
                "이건 ~/.claude/skills 증류와는 별개입니다 — 플러그인 코어 개선은 사람이 "
                "유지보수하는 영역이므로, 다음 중 하나로 처리하세요:\n"
                "- 이 개선을 upstream(UniM0cha/claude-self-improving-skills)에 기여하려면 "
                "/propose-plugin-improvement 를 실행하세요. fresh clone 에서 변경을 재현해 "
                "PR 로 제안합니다(write 권한이 없으면 fork 경유). "
                "opt-in: 환경변수 SIS_PLUGIN_PR=1 이 설정돼 있어야 실제 PR 을 만듭니다.\n"
                "- 또는 사람이 직접 브랜치/PR 로 처리하세요.\n"
                "자동 push·머지는 하지 않습니다. PR 제안까지만이며, 머지는 사람이 결정합니다."
            )
        emit({"decision": "block", "reason": "\n\n———\n\n".join(parts)})

    approve()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # approve()/emit() exit normally — let them through
    except Exception:
        # Last-resort fail-safe: any unexpected error -> clean approve JSON,
        # never a traceback that could be read as a malformed Stop decision.
        approve()
