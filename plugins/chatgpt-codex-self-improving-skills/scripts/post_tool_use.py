#!/usr/bin/env python3
"""Codex PostToolUse hook: tool telemetry + review counter + bypass-edit watch.

Three responsibilities per tool event:
  1. record lightweight tool telemetry (as before),
  2. bump the review-trigger counter (Hermes codex_runtime counts tool
     iterations; the Stop hook fires the review at a threshold — replacing the
     old Stop-turn modulo trigger that ignored how much work a turn contained),
  3. detect skill edits that BYPASSED the skill manager (direct shell writes)
     via a snapshot diff of SKILL.md files — command-string parsing is
     deliberately not used (payload argument shape is not a stable contract;
     the filesystem is). A bypass edit gets validated (broken frontmatter is
     surfaced immediately instead of at next skill load), its patch telemetry
     repaired (so the curator's idle clock stays honest), and a post-hoc
     checkpoint backup (protects against the NEXT edit — the pre-edit content
     is already gone and cannot be restored).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import skill_store
from skill_store import record_tool_use

# Cost gate: only tool families whose payloads can carry arbitrary file writes
# trigger the snapshot diff. Plugin-mediated mutations also match (patch/
# write/create/archive/restore/rollback/curate) so the baseline refreshes
# right after them, and the edit family (Edit/MultiEdit/edit_file) is a
# direct-write vector too.
MUTATING_TOOL_TOKENS = ("shell", "exec", "bash", "terminal", "patch", "write",
                        "create", "apply", "edit", "archive", "restore",
                        "rollback", "curate")

# A change whose skill record shows a manager-mediated mutation this recent
# is assumed manager-mediated. Keyed on last_managed_at, which ONLY the skill
# manager writes — created_at is unsuitable (a mere view of an untracked
# skill seeds it) and a shell edit leaves no stamp at all.
RECENT_PLUGIN_WRITE_SECONDS = 120


def _snapshot_path() -> Path:
    return skill_store.data_dir() / "skill_snapshot.json"


MAX_BASELINE_ROOTS = 16


def _load_baselines() -> dict:
    """Baselines keyed PER ROOT PATH (not per root SET): the root list is
    existence- and cwd-dependent, so a set-shaped key changes whenever a new
    root appears mid-session (losing the old baseline exactly when a bypass
    edit created it), and the shared home root would be duplicated into every
    repo's key. Per-root entries are stable and shared correctly."""
    snap = skill_store.load_json(_snapshot_path(), None)
    if isinstance(snap, dict) and isinstance(snap.get("baselines"), dict):
        # legacy root-SET keys (JSON arrays) never match a root path and age
        # out through the cap — no special migration needed
        return snap["baselines"]
    return {}


def _save_baselines(baselines: dict) -> None:
    if len(baselines) > MAX_BASELINE_ROOTS:
        oldest = sorted(baselines,
                        key=lambda k: str((baselines[k] or {}).get("t") or ""))
        for key in oldest[: len(baselines) - MAX_BASELINE_ROOTS]:
            baselines.pop(key, None)
    skill_store.atomic_write_json(_snapshot_path(), {"baselines": baselines})


def _merge_save_baselines(updates: dict) -> None:
    """Re-load + merge + save OUR roots' baselines under the shared lock:
    concurrent hooks from other repos write their own keys, and a bare
    read-then-write here would last-writer-win their entries away."""
    with skill_store.usage_lock():
        baselines = _load_baselines()
        for key, files in updates.items():
            baselines[key] = {"files": files, "t": skill_store.now_iso()}
        _save_baselines(baselines)


def _watch_roots() -> list:
    """default_skill_roots PLUS the default create root: on a fresh install
    ~/.codex/skills doesn't exist yet and would be absent from the root list,
    so the very first shell op that creates root+SKILL.md together would seed
    its own bypass edit as baseline. default_create_root() mkdirs it, making
    the list stable from SessionStart on."""
    roots = list(skill_store.default_skill_roots())
    try:
        create_root = skill_store.default_create_root()
        if create_root not in roots:
            roots.append(create_root)
    except Exception:
        pass
    return roots


def _current_states_by_root() -> dict:
    """{root_path: {skill_md_path: [mtime, size]}} for every ACTIVE root."""
    states: dict = {}
    for root in _watch_roots():
        files: dict = {}
        if root.exists():
            for skill_md in root.rglob("SKILL.md"):
                try:
                    rel_parts = skill_md.relative_to(root).parts[:-1]
                except ValueError:
                    continue
                if any(part.startswith(".") for part in rel_parts):
                    continue
                try:
                    st = skill_md.stat()
                except OSError:
                    continue
                files[str(skill_md)] = [st.st_mtime, st.st_size]
        states[str(root)] = files
    return states


def _recently_managed(rec: dict, now: datetime, sig: list | None = None) -> bool:
    """True when the manager itself made this change. When the manager
    recorded a file signature and we HAVE the current signature, they must
    match — a bare time window would also mask a direct edit made right
    after a manager write. Moves/deletes (no comparable signature) fall back
    to the time window."""
    stamp = skill_store._parse_time(rec.get("last_managed_at"))
    if not stamp or (now - stamp).total_seconds() >= RECENT_PLUGIN_WRITE_SECONDS:
        return False
    managed_sig = rec.get("managed_sig")
    if sig is not None and managed_sig is not None:
        return managed_sig == sig
    return True


def seed_baseline() -> None:
    """Seed any of OUR roots whose baseline is absent (other roots' baselines
    are preserved). Called from SessionStart so the FIRST mutating tool of a
    session is already comparable — otherwise an immediate first-tool bypass
    edit would be silently accepted as the baseline itself."""
    try:
        current_by_root = _current_states_by_root()  # fs walk OUTSIDE the lock
        with skill_store.usage_lock():
            baselines = _load_baselines()
            missing = {
                key: files for key, files in current_by_root.items()
                if not (isinstance(baselines.get(key), dict)
                        and isinstance(baselines[key].get("files"), dict))
            }
            if not missing:
                return
            for key, files in missing.items():
                baselines[key] = {"files": files, "t": skill_store.now_iso()}
            _save_baselines(baselines)
    except Exception:
        pass


def _check_bypass_edits(tool_name: str) -> list[str]:
    messages: list[str] = []
    if not any(token in tool_name.lower() for token in MUTATING_TOOL_TOKENS):
        return messages
    baselines = _load_baselines()
    current_by_root = _current_states_by_root()
    now = datetime.now(timezone.utc)
    usage_skills = skill_store.load_usage().get("skills", {})
    for root_key, current in current_by_root.items():
        entry = baselines.get(root_key)
        previous = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(previous, dict):
            continue  # first sight of this root — seed below, no detection
        for path_str, sig in current.items():
            if previous.get(path_str) == sig:
                continue
            skill_md = Path(path_str)
            name = skill_store.read_skill_name(skill_md)
            rec = usage_skills.get(name, {})
            if _recently_managed(rec, now, sig=sig):
                continue  # the manager's own write, not yet baselined
            try:
                # expected_name pins the frontmatter to the directory — a
                # direct edit that renames the frontmatter would otherwise
                # silently split dir / lookup key / usage record
                skill_store.validate_skill_content(
                    skill_md.read_text(encoding="utf-8"),
                    expected_name=skill_store.normalize_name(skill_md.parent.name))
            except Exception as exc:
                messages.append(
                    f"Skill '{name}' was edited outside the skill manager and "
                    f"is now invalid: {exc} Fix it (or restore a backup via "
                    "codex_skill_backups / codex_skill_rollback)."
                )
            try:
                skill_store.record_usage(name, patch=True)
                skill_store.append_jsonl(skill_store.events_path(), {
                    "at": skill_store.now_iso(), "type": "bypass_edit",
                    "skill": name, "file": path_str, "tool": tool_name,
                })
                skill_store.backup_skill(skill_md.parent,
                                         reason="bypass-edit-checkpoint")
            except Exception:
                pass
        # Deletions bypassed the reversible archive workflow entirely — the
        # content is gone, so all we can do is surface it and point at backups.
        for path_str in previous:
            if path_str in current:
                continue
            name = skill_store.normalize_name(Path(path_str).parent.name)
            rec = usage_skills.get(name, {})
            if _recently_managed(rec, now):
                continue  # manager-driven move, just now
            if rec.get("state") == "archived":
                continue  # manager archive moved it out of the roots (any age)
            messages.append(
                f"Skill '{name}' ({path_str}) was DELETED outside the skill "
                "manager. Prefer codex_skill_archive (reversible); check "
                "codex_skill_backups for a recoverable copy."
            )
            try:
                skill_store.append_jsonl(skill_store.events_path(), {
                    "at": skill_store.now_iso(), "type": "bypass_delete",
                    "skill": name, "file": path_str, "tool": tool_name,
                })
            except Exception:
                pass
    # Always refresh OUR roots' baselines (first sight just seeds them);
    # other roots' baselines stay intact (merged under the lock).
    try:
        _merge_save_baselines(current_by_root)
    except Exception:
        pass
    return messages


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    tool_name = str(payload.get("tool_name") or payload.get("toolName") or payload.get("name") or "unknown")
    try:
        record_tool_use(tool_name, payload if isinstance(payload, dict) else {})
    except Exception:
        pass
    lowered = tool_name.lower()
    # The manager's own skill-work tools just RESET the counter (create/
    # patch/write = real skill work) — their own PostToolUse must not bump
    # it right back to 1.
    is_skill_work = "codex_skill" in lowered and any(
        k in lowered for k in ("create", "patch", "write"))
    if not is_skill_work:
        try:
            skill_store.bump_review_counter(
                session=skill_store.hook_session_key(payload if isinstance(payload, dict) else {}))
        except Exception:
            pass
    try:
        messages = _check_bypass_edits(tool_name)
    except Exception:
        messages = []
    if messages:
        text = " ".join(messages)
        # Both channels: systemMessage surfaces to the user; additionalContext
        # reaches the MODEL so it can act on the repair guidance immediately.
        print(json.dumps({
            "systemMessage": text,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            },
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
