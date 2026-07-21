#!/usr/bin/env python3
"""Local Codex skill store utilities for the self-improvement plugin."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import fcntl
except Exception:  # pragma: no cover - Windows fallback
    fcntl = None

VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ALLOWED_SUPPORT_DIRS = {"references", "templates", "scripts", "assets"}
MAX_SKILL_CHARS = 100_000
MAX_SUPPORT_BYTES = 1_048_576
MAX_DESCRIPTION_CHARS = 1024  # hard cap (mirrors Hermes skill_manager validation)
DESCRIPTION_ADVISORY_CHARS = 200  # soft: a description should be ONE short sentence
PROVENANCE_VALUE = "self-improving-skills"
# Exact shape of archive_skill's collision suffix: -YYYYMMDDHHMMSS (UTC).
ARCHIVE_SUFFIX_RE = re.compile(r"^(.*)-(\d{14})$")


class SkillStoreError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def plugin_root() -> Path:
    env = os.environ.get("PLUGIN_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _installed_plugin_data_dir(root: Optional[Path] = None) -> Optional[Path]:
    """Derive Codex's writable plugin-data directory from an installed cache.

    Plugin hooks receive ``PLUGIN_DATA`` directly, but plugin-provided MCP
    servers currently only have the installed ``PLUGIN_ROOT``.  Codex installs
    marketplace plugins as::

        <codex-home>/plugins/cache/<marketplace>/<plugin>/<version>

    and stores hook data as::

        <codex-home>/plugins/data/<plugin>-<marketplace>

    Keeping this derivation here makes hooks, the MCP server, and the CLI use
    one store without depending on an undocumented MCP environment variable.
    """
    resolved = (root or plugin_root()).expanduser().resolve()
    plugin_dir = resolved.parent
    marketplace_dir = plugin_dir.parent
    cache_dir = marketplace_dir.parent
    plugins_dir = cache_dir.parent
    codex_home = plugins_dir.parent
    if cache_dir.name != "cache" or plugins_dir.name != "plugins":
        return None
    if not plugin_dir.name or not marketplace_dir.name:
        return None
    return codex_home / "plugins" / "data" / f"{plugin_dir.name}-{marketplace_dir.name}"


def resolve_data_dir(*, create: bool = True) -> Tuple[Path, str]:
    """Return the active data directory and the rule that selected it."""
    env = os.environ.get("PLUGIN_DATA")
    if env:
        path = Path(env).expanduser().resolve()
        source = "plugin_data_env"
    else:
        installed = _installed_plugin_data_dir()
        if installed is not None:
            path = installed.resolve()
            source = "codex_plugin_cache"
        else:
            path = (Path.home() / ".self-improving-skills").resolve()
            source = "legacy_home"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path, source


def data_dir() -> Path:
    path, _ = resolve_data_dir()
    return path


def auto_continue_enabled(value: Optional[str] = None) -> bool:
    """Automatic reviews default on and support an explicit opt-out.

    ``None`` means the environment variable is absent.  Any explicitly set
    value outside the documented truthy set, including an empty value, turns
    automatic continuation off.
    """
    raw = os.environ.get("CODEX_SELF_IMPROVE_AUTO") if value is None else value
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def usage_path() -> Path:
    return data_dir() / "usage.json"


def usage_lock_path() -> Path:
    return data_dir() / "usage.lock"


def backups_lock_path() -> Path:
    return data_dir() / "backups.lock"


def events_path() -> Path:
    return data_dir() / "events.jsonl"


def review_signals_path() -> Path:
    return data_dir() / "review-signals.jsonl"


def state_path() -> Path:
    return data_dir() / "state.json"


def normalize_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    name = re.sub(r"-+", "-", name).strip("-._")
    return name[:64]


def validate_name(name: str) -> str:
    name = normalize_name(name)
    if not name or not VALID_NAME_RE.match(name):
        raise SkillStoreError(
            "Invalid skill name. Use lowercase letters, numbers, dots, underscores, or hyphens."
        )
    return name


def default_skill_roots(cwd: Optional[Path] = None) -> List[Path]:
    roots: List[Path] = []
    env = os.environ.get("CODEX_SELF_IMPROVE_SKILL_ROOTS")
    if env:
        for item in env.split(os.pathsep):
            if item.strip():
                roots.append(Path(item).expanduser())
    else:
        cwd = cwd or Path.cwd()
        repo_root = _git_root(cwd) or cwd
        for repo_skills in (
            repo_root / ".agents" / "skills",
            repo_root / ".codex" / "skills",
        ):
            if repo_skills.exists():
                roots.append(repo_skills)
        roots.append(Path.home() / ".agents" / "skills")
        codex_skills = Path.home() / ".codex" / "skills"
        if codex_skills.exists():
            roots.append(codex_skills)

    unique: List[Path] = []
    seen = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if str(resolved) not in seen:
            unique.append(resolved)
            seen.add(str(resolved))
    return unique


def default_create_root() -> Path:
    env = os.environ.get("CODEX_SELF_IMPROVE_CREATE_ROOT")
    root = Path(env).expanduser() if env else Path.home() / ".codex" / "skills"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _git_root(cwd: Path) -> Optional[Path]:
    current = cwd.resolve()
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return parent
    return None


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


@contextmanager
def usage_lock() -> Iterable[None]:
    if fcntl is None:
        yield
        return
    lock = usage_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def backups_lock() -> Iterable[None]:
    """Serialize backup creation, restore reads, pruning, and migration."""
    if fcntl is None:
        yield
        return
    lock = backups_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def mutate_usage(mutator: Any) -> Any:
    with usage_lock():
        data = load_usage()
        result = mutator(data)
        save_usage(data)
        return result


def parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillStoreError("SKILL.md must start with YAML frontmatter.")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillStoreError("SKILL.md frontmatter is not closed.")
    raw = text[4:end]
    body = text[text.find("\n", end + 4) + 1 :]
    meta: Dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def validate_skill_content(text: str, expected_name: Optional[str] = None) -> None:
    if len(text) > MAX_SKILL_CHARS:
        raise SkillStoreError(f"SKILL.md exceeds {MAX_SKILL_CHARS} characters.")
    meta, body = parse_frontmatter(text)
    name = meta.get("name")
    description = meta.get("description")
    if not name:
        raise SkillStoreError("Frontmatter must include name.")
    if not description:
        raise SkillStoreError("Frontmatter must include description.")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillStoreError(
            f"Frontmatter description exceeds {MAX_DESCRIPTION_CHARS} characters."
        )
    if expected_name and normalize_name(name) != expected_name:
        raise SkillStoreError(
            f"Frontmatter name '{name}' does not match target skill '{expected_name}'."
        )
    if not body.strip():
        raise SkillStoreError("SKILL.md must include instructions after frontmatter.")


def _description_advisory(text: str) -> Optional[str]:
    """Non-fatal advisory for an over-long (but valid) description."""
    try:
        meta, _ = parse_frontmatter(text)
    except SkillStoreError:
        return None
    description = meta.get("description") or ""
    if len(description) > DESCRIPTION_ADVISORY_CHARS:
        return (
            f"description is {len(description)} chars — long descriptions bloat the "
            "skill-discovery context and degrade routing quality. Trim it to one "
            "short sentence stating the capability."
        )
    return None


def _stamp_provenance(content: str) -> str:
    """Inject a metadata.provenance marker into new-skill frontmatter so
    agent-created skills stay identifiable even if the usage.json sidecar is
    lost or the skill moves machines. Never touches an existing metadata
    block (the author manages it) and never double-stamps — decided from the
    FRONTMATTER, not a raw substring (a body/description merely mentioning
    the plugin name must not suppress the stamp)."""
    if not content.startswith("---\n"):
        return content
    end = content.find("\n---", 4)
    if end == -1:
        return content
    fm = content[4:end]
    if re.search(r"^metadata\s*:", fm, re.MULTILINE):
        return content
    if re.search(r"^\s*provenance\s*:", fm, re.MULTILINE):
        return content  # already stamped
    new_fm = (
        fm.rstrip("\n")
        + "\nmetadata:\n"
        + f"  provenance: {PROVENANCE_VALUE}\n"
        + f"  created_at: {now_iso()}\n"
    )
    return "---\n" + new_fm + content[end:]


def _frontmatter_provenance(skill_dir: Path) -> bool:
    """True when SKILL.md's FRONTMATTER carries this plugin's provenance stamp.

    Scoped to the closed frontmatter block, not a raw head-substring check — a
    user-authored skill whose body merely MENTIONS the marker string must never
    become curation-eligible through it."""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return False
        end = text.find("\n---", 4)
        if end == -1:
            return False
        fm = text[4:end]
    except Exception:
        return False
    return bool(re.search(
        r"^\s*provenance\s*:\s*" + re.escape(PROVENANCE_VALUE) + r"\s*$",
        fm, re.MULTILINE))


def scan_skill_dir(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """Advisory security scan of a skill directory (secrets / injection /
    invisible unicode / local paths). NEVER raises and never blocks a write —
    Hermes keeps agent-created scanning off by default because the agent can
    already run the same code through the shell; the value here is surfacing,
    not gating. Warn-level detail is folded to a count (machine-local paths
    are routine in locally-authored skills)."""
    try:
        from scan_skill import scan_dir

        findings = scan_dir(str(skill_dir))
    except Exception:
        return None
    blocking = [f for f in findings if f.get("severity") == "block"]
    return {
        "blocking": len(blocking),
        "warnings": len(findings) - len(blocking),
        "findings": blocking,
    }


def iter_skill_files(roots: Optional[List[Path]] = None) -> Iterable[Path]:
    roots = roots or default_skill_roots()
    for root in roots:
        if not root.exists():
            continue
        for skill_md in root.rglob("SKILL.md"):
            try:
                rel_parts = skill_md.relative_to(root).parts[:-1]
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_parts):
                continue
            yield skill_md


def read_skill_name(skill_md: Path) -> str:
    try:
        meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        return normalize_name(meta.get("name") or skill_md.parent.name)
    except Exception:
        return normalize_name(skill_md.parent.name)


def find_skill(name: str, include_archived: bool = False) -> Optional[Path]:
    name = validate_name(name)
    for skill_md in iter_skill_files():
        if read_skill_name(skill_md) == name:
            return skill_md.parent
    if include_archived:
        for root in default_skill_roots():
            archived = root / ".archive" / name / "SKILL.md"
            if archived.exists():
                return archived.parent
    return None


def list_skills() -> Dict[str, Any]:
    usage = load_usage()
    skills = []
    for skill_md in iter_skill_files():
        name = read_skill_name(skill_md)
        try:
            meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        rec = usage.get("skills", {}).get(name, {})
        skills.append(
            {
                "name": name,
                "description": meta.get("description", ""),
                "path": str(skill_md),
                "root": str(_containing_root(skill_md.parent)),
                "usage": rec,
            }
        )
    skills.sort(key=lambda row: row["name"])
    return {"skills": skills, "roots": [str(p) for p in default_skill_roots()]}


def view_skill(name: str, file_path: Optional[str] = None) -> Dict[str, Any]:
    skill_dir = find_skill(name)
    if not skill_dir:
        raise SkillStoreError(f"Skill '{name}' was not found.")
    name = read_skill_name(skill_dir / "SKILL.md")
    rel = _safe_relative_path(file_path) if file_path else Path("SKILL.md")
    target = _resolve_inside(skill_dir, rel)
    if not target.is_file():
        raise SkillStoreError(f"File '{rel}' does not exist in skill '{name}'.")
    # Loading a skill is behavioural intent, not idle browsing — count it as a
    # use too (Hermes skills_tool bumps view AND use on skill_view). Without
    # this, nothing in the plugin ever records use and the curator's
    # "actually unused" signal is permanently empty.
    record_usage(name, view=True, use=True)
    files = []
    for child in sorted(skill_dir.rglob("*")):
        if child.is_file():
            files.append(str(child.relative_to(skill_dir)))
    return {
        "name": name,
        "path": str(skill_dir),
        "file": str(rel),
        "content": target.read_text(encoding="utf-8"),
        "files": files,
    }


def _containing_root(path: Path) -> Path:
    resolved = path.resolve()
    for root in default_skill_roots():
        try:
            resolved.relative_to(root)
            return root
        except ValueError:
            continue
    return default_create_root()


def _safe_relative_path(file_path: str) -> Path:
    rel = Path(file_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise SkillStoreError("file_path must be a safe relative path.")
    if str(rel) == "SKILL.md":
        return rel
    if not rel.parts or rel.parts[0] not in ALLOWED_SUPPORT_DIRS:
        allowed = ", ".join(sorted(ALLOWED_SUPPORT_DIRS))
        raise SkillStoreError(f"Supporting files must live under one of: {allowed}.")
    return rel


def _resolve_inside(skill_dir: Path, rel: Path) -> Path:
    """`skill_dir / rel`, refused when the RESOLVED path escapes the resolved
    skill dir — a lexically-safe relative path can still be a symlink pointing
    anywhere on disk, which would turn view into an arbitrary local-file read
    and patch/write into an arbitrary write."""
    target = skill_dir / rel
    resolved = target.resolve()
    root = skill_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise SkillStoreError(
            f"'{rel}' escapes the skill directory (symlink?) — refused.")
    return target


def _frontmatter_pinned(skill_dir: Path) -> bool:
    try:
        meta, _ = parse_frontmatter((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(meta.get("pinned") or "").strip().lower() in {"1", "true", "yes", "on"}


def _backup_skill_unlocked(skill_dir: Path, reason: str = "manual") -> Dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = read_skill_name(skill_dir / "SKILL.md")
    base_id = f"{ts}-{name}"
    backup_id = base_id
    dest = data_dir() / "backups" / backup_id
    suffix = 2
    while dest.exists():
        backup_id = f"{base_id}-{suffix}"
        dest = data_dir() / "backups" / backup_id
        suffix += 1
    shutil.copytree(skill_dir, dest)
    manifest = {
        "backup_id": backup_id,
        "skill": name,
        "source": str(skill_dir),
        "created_at": now_iso(),
        "reason": reason,
    }
    atomic_write_json(dest / "manifest.json", manifest)
    return manifest


def backup_skill(skill_dir: Path, reason: str = "manual") -> Dict[str, Any]:
    with backups_lock():
        return _backup_skill_unlocked(skill_dir, reason=reason)


def load_usage() -> Dict[str, Any]:
    data = load_json(usage_path(), {"version": 1, "skills": {}, "tools": {}, "counters": {}})
    if not isinstance(data, dict):
        return {"version": 1, "skills": {}, "tools": {}, "counters": {}}
    data.setdefault("version", 1)
    data.setdefault("skills", {})
    data.setdefault("tools", {})
    data.setdefault("counters", {})
    return data


def save_usage(data: Dict[str, Any]) -> None:
    atomic_write_json(usage_path(), data)


def record_usage(
    name: str,
    *,
    view: bool = False,
    use: bool = False,
    patch: bool = False,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    name = validate_name(name)

    def _mutate(data: Dict[str, Any]) -> Dict[str, Any]:
        rec = data.setdefault("skills", {}).setdefault(
            name,
            {
                "created_at": now_iso(),
                "created_by": created_by or "unknown",
                "state": "active",
                "pinned": False,
                "use_count": 0,
                "view_count": 0,
                "patch_count": 0,
            },
        )
        if created_by and rec.get("created_by") in (None, "unknown"):
            rec["created_by"] = created_by
        if view:
            rec["view_count"] = int(rec.get("view_count") or 0) + 1
            rec["last_viewed_at"] = now_iso()
        if use:
            rec["use_count"] = int(rec.get("use_count") or 0) + 1
            rec["last_used_at"] = now_iso()
        if patch:
            rec["patch_count"] = int(rec.get("patch_count") or 0) + 1
            rec["last_patched_at"] = now_iso()
        if (view or use or patch) and rec.get("state") == "stale":
            rec["state"] = "active"
        return rec

    return mutate_usage(_mutate)


def record_tool_use(tool_name: str, payload: Dict[str, Any]) -> None:
    def _mutate(data: Dict[str, Any]) -> None:
        rec = data.setdefault("tools", {}).setdefault(tool_name, {"count": 0})
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last_used_at"] = now_iso()
        rec["last_payload_keys"] = sorted(payload.keys())

    mutate_usage(_mutate)
    append_jsonl(events_path(), {"at": now_iso(), "type": "tool", "tool": tool_name})


# --- review-trigger counter (Hermes codex_runtime._iters_since_skill port) ---
# Stored inside usage.json under the SAME usage_lock as everything else, so the
# PostToolUse increment and the Stop-hook read/reset never race on a bare
# state.json load-modify-write. Keyed PER SESSION (when the hook payload
# carries a session/thread id — "global" fallback otherwise): concurrent
# sessions sharing one PLUGIN_DATA must not pool their iterations, or one
# session's Stop would consume the other's accumulated work signal.

MAX_COUNTER_SESSIONS = 20


def _counter_map(data: Dict[str, Any]) -> Dict[str, Any]:
    counters = data.setdefault("counters", {})
    m = counters.setdefault("iters_since_review_by_session", {})
    # migrate the pre-v0.2.0 single-int shape once
    legacy = counters.pop("iters_since_review", None)
    if legacy is not None and "global" not in m:
        try:
            m["global"] = {"v": int(legacy), "t": now_iso()}
        except (TypeError, ValueError):
            pass
    return m


def _prune_counter_map(m: Dict[str, Any]) -> None:
    if len(m) <= MAX_COUNTER_SESSIONS:
        return
    oldest = sorted(m, key=lambda k: str((m[k] or {}).get("t") or ""))
    for key in oldest[: len(m) - MAX_COUNTER_SESSIONS]:
        m.pop(key, None)


def bump_review_counter(session: str = "global") -> int:
    def _mutate(data: Dict[str, Any]) -> int:
        m = _counter_map(data)
        entry = m.setdefault(session, {"v": 0})
        entry["v"] = int(entry.get("v") or 0) + 1
        entry["t"] = now_iso()
        _prune_counter_map(m)
        return entry["v"]

    return mutate_usage(_mutate)


def get_review_counter(session: str = "global") -> int:
    def _mutate(data: Dict[str, Any]) -> int:
        try:
            return int((_counter_map(data).get(session) or {}).get("v") or 0)
        except (TypeError, ValueError):
            return 0

    return mutate_usage(_mutate)


def reset_review_counter() -> None:
    """Zero ALL sessions' counters — called on real skill work (create/patch/
    write), which is a store-level event with no session identity; resetting
    broadly only makes reviews rarer, never spurious."""
    def _mutate(data: Dict[str, Any]) -> None:
        m = _counter_map(data)
        for entry in m.values():
            if isinstance(entry, dict):
                entry["v"] = 0

    mutate_usage(_mutate)


def consume_review_counter(session: str = "global") -> int:
    """Atomically read AND zero ONE session's counter in one locked mutation —
    a separate get-then-reset would erase increments landing in between
    (parallel PostToolUse), delaying the next review."""
    def _mutate(data: Dict[str, Any]) -> int:
        m = _counter_map(data)
        entry = m.setdefault(session, {"v": 0})
        try:
            value = int(entry.get("v") or 0)
        except (TypeError, ValueError):
            value = 0
        entry["v"] = 0
        entry["t"] = now_iso()
        return value

    return mutate_usage(_mutate)


def hook_session_key(payload: Dict[str, Any]) -> str:
    """The session/thread identity a hook payload carries, or 'global'."""
    for key in ("session_id", "sessionId", "thread_id", "threadId",
                "conversation_id", "conversationId"):
        value = payload.get(key)
        if value:
            return str(value)
    return "global"


def _touch_managed(name: str, skill_dir: Optional[Path] = None) -> None:
    """Stamp the record with the time AND file signature of a MANAGER-mediated
    mutation (create/patch/write/archive/restore/rollback). The bypass-edit
    watcher keys on these — created_at is unsuitable (a mere view_skill of an
    untracked skill seeds created_at=now), and the signature lets the watcher
    distinguish "the manager's own write, not yet baselined" from "a direct
    edit made right after a manager write" (a bare time window can't)."""
    sig = None
    try:
        target_dir = skill_dir or find_skill(name)
        if target_dir:
            st = (target_dir / "SKILL.md").stat()
            sig = [st.st_mtime, st.st_size]
    except Exception:
        sig = None

    def _mutate(data: Dict[str, Any]) -> None:
        rec = data.setdefault("skills", {}).setdefault(name, {"created_at": now_iso()})
        rec["last_managed_at"] = now_iso()
        rec["managed_sig"] = sig

    mutate_usage(_mutate)


def create_skill(
    name: str,
    content: str,
    root: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    name = validate_name(name)
    validate_skill_content(content, expected_name=name)
    content = _stamp_provenance(content)
    # the stamp ADDS characters — near-limit input must fail validation here,
    # not produce a file that every later validation pass rejects
    validate_skill_content(content, expected_name=name)
    target_root = Path(root).expanduser().resolve() if root else default_create_root()
    skill_dir = target_root / name
    if skill_dir.exists():
        raise SkillStoreError(f"Skill '{name}' already exists at {skill_dir}.")
    skill_dir.mkdir(parents=True, exist_ok=False)
    atomic_write_text(skill_dir / "SKILL.md", content)
    record_usage(name, created_by="agent")
    _touch_managed(name, skill_dir=skill_dir)
    if reason:
        def _reason(data: Dict[str, Any]) -> None:
            rec = data.setdefault("skills", {}).setdefault(name, {"created_at": now_iso()})
            rec["create_reason"] = str(reason)[:500]

        mutate_usage(_reason)
    reset_review_counter()  # real skill work just happened — restart the clock
    result = {"action": "create", "name": name, "path": str(skill_dir), "backup": None}
    advisory = _description_advisory(content)
    if advisory:
        result["advisory"] = advisory
    scan = scan_skill_dir(skill_dir)
    if scan:
        result["scan"] = scan
    return result


def patch_skill(name: str, old_text: str, new_text: str, file_path: str = "SKILL.md") -> Dict[str, Any]:
    name = validate_name(name)
    skill_dir = find_skill(name)
    if not skill_dir:
        raise SkillStoreError(f"Skill '{name}' was not found.")
    rel = _safe_relative_path(file_path)
    target = _resolve_inside(skill_dir, rel)
    if not target.exists():
        raise SkillStoreError(f"File '{file_path}' does not exist in skill '{name}'.")
    text = target.read_text(encoding="utf-8")
    if old_text not in text:
        # Self-correction affordance (Hermes _patch_skill): show the head of
        # the file so the caller can retry with real content instead of
        # guessing blind.
        preview = text[:500] + ("..." if len(text) > 500 else "")
        raise SkillStoreError(
            "old_text was not found. File starts with:\n" + preview
        )
    updated = text.replace(old_text, new_text, 1)
    if rel == Path("SKILL.md"):
        validate_skill_content(updated, expected_name=name)
    backup = backup_skill(skill_dir, reason=f"patch:{file_path}")
    atomic_write_text(target, updated)
    record_usage(name, patch=True)
    _touch_managed(name, skill_dir=skill_dir)
    reset_review_counter()
    result = {"action": "patch", "name": name, "file": file_path, "backup": backup["backup_id"]}
    if rel == Path("SKILL.md"):
        advisory = _description_advisory(updated)
        if advisory:
            result["advisory"] = advisory
    scan = scan_skill_dir(skill_dir)
    if scan:
        result["scan"] = scan
    return result


def write_support_file(name: str, file_path: str, content: str) -> Dict[str, Any]:
    name = validate_name(name)
    skill_dir = find_skill(name)
    if not skill_dir:
        raise SkillStoreError(f"Skill '{name}' was not found.")
    rel = _safe_relative_path(file_path)
    target = _resolve_inside(skill_dir, rel)
    if rel == Path("SKILL.md"):
        validate_skill_content(content, expected_name=name)
    if len(content.encode("utf-8")) > MAX_SUPPORT_BYTES:
        raise SkillStoreError(f"File exceeds {MAX_SUPPORT_BYTES} bytes.")
    backup = backup_skill(skill_dir, reason=f"write:{file_path}")
    atomic_write_text(target, content)
    record_usage(name, patch=True)
    _touch_managed(name, skill_dir=skill_dir)
    reset_review_counter()
    result = {"action": "write_file", "name": name, "file": file_path, "backup": backup["backup_id"]}
    scan = scan_skill_dir(skill_dir)
    if scan:
        result["scan"] = scan
    return result


def pin_skill(name: str, pinned: bool = True) -> Dict[str, Any]:
    name = validate_name(name)
    if not find_skill(name, include_archived=True):
        raise SkillStoreError(f"Skill '{name}' was not found.")

    def _mutate(data: Dict[str, Any]) -> None:
        rec = data.setdefault("skills", {}).setdefault(name, {"created_at": now_iso(), "state": "active"})
        rec["pinned"] = bool(pinned)

    mutate_usage(_mutate)
    return {"action": "pin" if pinned else "unpin", "name": name, "pinned": bool(pinned)}


def archive_skill(name: str) -> Dict[str, Any]:
    name = validate_name(name)
    skill_dir = find_skill(name)
    if not skill_dir:
        raise SkillStoreError(f"Skill '{name}' was not found.")
    with usage_lock():
        data = load_usage()
        rec = data.setdefault("skills", {}).setdefault(name, {"created_at": now_iso(), "state": "active"})
        if rec.get("pinned") or _frontmatter_pinned(skill_dir):
            raise SkillStoreError(f"Skill '{name}' is pinned and cannot be archived.")
        root = _containing_root(skill_dir)
        archive_dir = root / ".archive" / name
        if archive_dir.exists():
            # never refuse (that strands the live dir) and never overwrite an
            # older archive — park under a timestamp suffix (Hermes
            # skill_usage). Bump the numeric stamp until free: shutil.move
            # into an EXISTING dir would nest instead of replace, and the
            # suffix must keep its exact 14-digit shape for restore matching.
            stamp = int(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
            archive_dir = root / ".archive" / f"{name}-{stamp}"
            while archive_dir.exists():
                stamp += 1
                archive_dir = root / ".archive" / f"{name}-{stamp}"
        backup = backup_skill(skill_dir, reason="archive")
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill_dir), str(archive_dir))
        rec["state"] = "archived"
        rec["archived_at"] = now_iso()
        rec["archived_as"] = archive_dir.name
        rec["last_managed_at"] = now_iso()
        save_usage(data)
    return {"action": "archive", "name": name, "path": str(archive_dir), "backup": backup["backup_id"]}


def _strip_archive_suffix(name: str) -> str:
    """`<name>-<14-digit UTC stamp>` → bare name (exact shape only)."""
    match = ARCHIVE_SUFFIX_RE.match(name)
    return match.group(1) if match else name


def _archived_dest_name(archived: Path) -> str:
    """The name an archived dir restores to: the frontmatter name when
    readable (authoritative — resolves the '<name>-<14 digits>' ambiguity
    between a collision suffix and a skill legitimately named that way),
    else the dir name with an exact-shape suffix stripped."""
    skill_md = archived / "SKILL.md"
    if skill_md.is_file():
        try:
            meta, _ = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            if meta.get("name"):
                return validate_name(meta["name"])
        except Exception:
            pass
    return validate_name(_strip_archive_suffix(archived.name))


def restore_skill(name: str, root: Optional[str] = None) -> Dict[str, Any]:
    name = validate_name(name)
    roots = [Path(root).expanduser().resolve()] if root else default_skill_roots()
    explicit_suffix = _strip_archive_suffix(name) != name
    for skill_root in roots:
        archive_root = skill_root / ".archive"
        # Pass 1: a dir EXACTLY matching the input — never strip first, or a
        # skill legitimately named 'report-20260713010203' could be hijacked
        # into restoring as 'report'.
        archived = archive_root / name
        if not archived.is_dir():
            if explicit_suffix:
                # an explicit archive ID that doesn't exist must FAIL in this
                # root — substituting the bare/newest copy would silently
                # restore a different version than the one asked for
                continue
            # Pass 2 (bare-name requests only): exact-shape timestamp-suffix
            # fallback — no loose prefix matching (Hermes 992b9223: restoring
            # 'git' must not swallow an unrelated 'git-helpers'); newest wins.
            bare = _strip_archive_suffix(name)
            candidates = []
            if archive_root.is_dir():
                for entry in archive_root.iterdir():
                    match = ARCHIVE_SUFFIX_RE.match(entry.name)
                    if match and match.group(1) == bare and entry.is_dir():
                        # a skill LEGITIMATELY named '<bare>-<14 digits>' is
                        # not a collision archive of '<bare>' — its
                        # frontmatter says so; never restore it under a name
                        # the caller didn't ask for
                        if _archived_dest_name(entry) == bare:
                            candidates.append(entry)
            if not candidates:
                continue
            archived = sorted(candidates, key=lambda p: p.name)[-1]
        dest_name = _archived_dest_name(archived)
        dest = skill_root / dest_name
        if dest.exists():
            raise SkillStoreError(f"Restore destination already exists: {dest}")
        shutil.move(str(archived), str(dest))

        def _mutate(data: Dict[str, Any]) -> None:
            rec = data.setdefault("skills", {}).setdefault(dest_name, {"created_at": now_iso()})
            rec["state"] = "active"
            rec["restored_at"] = now_iso()
            rec.pop("archived_as", None)

        mutate_usage(_mutate)
        _touch_managed(dest_name, skill_dir=dest)
        return {"action": "restore", "name": dest_name, "path": str(dest)}
    raise SkillStoreError(f"Archived skill '{name}' was not found.")


def list_backups(skill: Optional[str] = None) -> Dict[str, Any]:
    """All backups (newest last), optionally filtered to one skill."""
    wanted = validate_name(skill) if skill else None
    backups = []
    root = data_dir() / "backups"
    if root.is_dir():
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            manifest = load_json(entry / "manifest.json", {})
            if not isinstance(manifest, dict):
                manifest = {}
            manifest.setdefault("backup_id", entry.name)
            if wanted and manifest.get("skill") != wanted:
                continue
            backups.append(manifest)
    return {"backups": backups}


def restore_backup(backup_id: str) -> Dict[str, Any]:
    """Replace a skill directory with a backup's content — exact backup_id
    only. The current directory (if present) is backed up first, so the
    rollback itself is undoable (Hermes curator_backup pattern)."""
    if not backup_id or "/" in backup_id or "\\" in backup_id or backup_id.startswith("."):
        raise SkillStoreError("backup_id must be an exact backup name.")
    with backups_lock():
        src = data_dir() / "backups" / backup_id
        if not src.is_dir():
            raise SkillStoreError(f"Backup '{backup_id}' was not found.")
        manifest = load_json(src / "manifest.json", {})
        if not isinstance(manifest, dict) or not manifest.get("skill"):
            raise SkillStoreError(f"Backup '{backup_id}' has no readable manifest.")
        name = validate_name(str(manifest["skill"]))
        # Restore to the backup's ORIGINAL path only — find_skill(name) would
        # pick the first root in search order, so a user-root backup could
        # overwrite a same-named repo-root skill while the original remains.
        source = str(manifest.get("source") or "")
        dest = Path(source).expanduser() if source else default_create_root() / name
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _ignore_root_manifest(dirpath: str, names: List[str]) -> set:
            # exclude ONLY the backup-root metadata manifest — a skill's own
            # nested references/manifest.json etc. must survive the restore
            if Path(dirpath).resolve() == src.resolve():
                return {"manifest.json"} & set(names)
            return set()

        # Copy under the shared backup lock. Once staging is complete, prune
        # can safely remove the source without affecting this restore.
        staging = dest.parent / f".{dest.name}.restore-staging"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging, ignore=_ignore_root_manifest)
        undo = None
        aside = None
        try:
            if dest.exists():
                undo = _backup_skill_unlocked(
                    dest,
                    reason=f"pre-restore:{backup_id}",
                )["backup_id"]
                aside = dest.parent / f".{dest.name}.restore-aside"
                shutil.rmtree(aside, ignore_errors=True)
                dest.rename(aside)
            staging.rename(dest)
            if aside is not None:
                shutil.rmtree(aside, ignore_errors=True)
        except BaseException:
            if aside is not None and aside.exists() and not dest.exists():
                aside.rename(dest)  # put the original back
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _mutate(data: Dict[str, Any]) -> None:
        rec = data.setdefault("skills", {}).setdefault(name, {"created_at": now_iso()})
        rec["state"] = "active"
        rec["restored_at"] = now_iso()
        rec["restored_from_backup"] = backup_id

    mutate_usage(_mutate)
    _touch_managed(name, skill_dir=dest)
    return {"action": "restore_backup", "name": name, "path": str(dest),
            "undo_backup": undo}


def _prune_backups_unlocked(
    keep_per_skill: int = 5,
    protect: Iterable[str] = (),
) -> Dict[str, Any]:
    """Keep the newest N backups per skill; `protect` names are never removed
    (Hermes fc1119ca: the prune must not delete a backup a restore is using)."""
    keep_per_skill = max(0, int(keep_per_skill))
    protected = {str(p) for p in protect}
    root = data_dir() / "backups"
    removed: List[str] = []
    if root.is_dir():
        by_skill: Dict[str, List[Path]] = {}
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            manifest = load_json(entry / "manifest.json", {})
            skill = str(manifest.get("skill") if isinstance(manifest, dict) else "") or entry.name
            by_skill.setdefault(skill, []).append(entry)
        for dirs in by_skill.values():
            dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in dirs[keep_per_skill:]:
                if old.name in protected:
                    continue
                shutil.rmtree(old, ignore_errors=True)
                removed.append(old.name)
    return {"action": "prune_backups", "keep_per_skill": keep_per_skill,
            "removed": sorted(removed)}


def prune_backups(keep_per_skill: int = 5, protect: Iterable[str] = ()) -> Dict[str, Any]:
    with backups_lock():
        return _prune_backups_unlocked(keep_per_skill, protect)


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_activity(record: Dict[str, Any], skill_dir: Path) -> datetime:
    values = [
        record.get("last_used_at"),
        record.get("last_viewed_at"),
        record.get("last_patched_at"),
        record.get("created_at"),
    ]
    parsed = [dt for dt in (_parse_time(v) for v in values) if dt]
    if parsed:
        return max(parsed)
    return datetime.fromtimestamp((skill_dir / "SKILL.md").stat().st_mtime, tz=timezone.utc)


def _use_count(record: Dict[str, Any]) -> int:
    try:
        return int(record.get("use_count") or 0)
    except (TypeError, ValueError):
        return 0


def _archive_days_for(record: Dict[str, Any], base_days: int) -> int:
    if _use_count(record) >= 3:
        return base_days * 2
    return base_days


def curate(dry_run: bool = True, stale_days: int = 30, archive_days: int = 90) -> Dict[str, Any]:
    usage = load_usage()
    now = datetime.now(timezone.utc)
    rows = []
    for item in list_skills()["skills"]:
        name = item["name"]
        rec = usage.get("skills", {}).get(name, {})
        skill_dir = Path(item["path"]).parent
        latest = _latest_activity(rec, skill_dir)
        age_days = (now - latest).days
        pinned = bool(rec.get("pinned")) or _frontmatter_pinned(skill_dir)
        created_by = str(rec.get("created_by") or "user")
        # curation eligibility is record OR frontmatter stamp — the stamp
        # survives a lost usage.json / a machine move (and this union is the
        # same pattern `pinned` already uses one line up)
        agent_created = created_by == "agent" or _frontmatter_provenance(skill_dir)
        action = "keep"
        reason = "recent"
        if not agent_created:
            reason = f"protected {created_by} skill"
        elif pinned:
            reason = "pinned"
        elif age_days >= _archive_days_for(rec, archive_days):
            action = "archive"
            reason = f"inactive for {age_days} days"
        elif age_days >= stale_days:
            action = "mark_stale"
            reason = f"inactive for {age_days} days"
        elif rec.get("state") == "stale":
            action = "reactivate"
            reason = "recent activity"
        rows.append(
            {
                "name": name,
                "candidate_action": action,
                "reason": reason,
                "age_days": age_days,
                "pinned": pinned,
                "created_by": created_by,
                "use_count": _use_count(rec),
                "path": item["path"],
            }
        )
    applied = []
    if not dry_run:
        for row in rows:
            if row["candidate_action"] == "archive":
                applied.append(archive_skill(row["name"]))
            elif row["candidate_action"] == "mark_stale":
                def _mark_stale(data: Dict[str, Any], skill_name: str = row["name"]) -> None:
                    rec = data.setdefault("skills", {}).setdefault(skill_name, {"created_at": now_iso()})
                    rec["state"] = "stale"

                mutate_usage(_mark_stale)
                applied.append({"action": "mark_stale", "name": row["name"]})
            elif row["candidate_action"] == "reactivate":
                def _reactivate(data: Dict[str, Any], skill_name: str = row["name"]) -> None:
                    rec = data.setdefault("skills", {}).setdefault(skill_name, {"created_at": now_iso()})
                    rec["state"] = "active"

                mutate_usage(_reactivate)
                applied.append({"action": "reactivate", "name": row["name"]})
    result = {"dry_run": dry_run, "stale_days": stale_days, "archive_days": archive_days,
              "candidates": rows, "applied": applied}
    result["report_path"] = _write_curate_report(result)

    def _stamp(state: Dict[str, Any]) -> None:
        # locked mutation — a bare load→save here could last-writer-win a
        # concurrent Stop hook's transcript offsets / auto-review marker
        state["last_curate_at"] = now_iso()
        if result["report_path"]:
            state["last_report_path"] = result["report_path"]

    mutate_state(_stamp)
    return result


def _write_curate_report(result: Dict[str, Any]) -> Optional[str]:
    """Persist a per-run curator report (Hermes writes run.json + REPORT.md
    every pass) — without it there is no after-the-fact audit trail of what
    the curator decided or applied. Best-effort: never fails the curate."""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_dir = data_dir() / "logs" / "curator" / stamp
        n = 1
        while report_dir.exists():  # same-second runs must not overwrite
            report_dir = data_dir() / "logs" / "curator" / f"{stamp}-{n}"
            n += 1
        report_dir.mkdir(parents=True)
        report_json = report_dir / "report.json"
        atomic_write_json(report_json, dict(result, generated_at=now_iso()))
        prefix = "[DRY-RUN] " if result.get("dry_run") else ""
        lines = [
            f"# {prefix}Codex skill curator report",
            "",
            f"- generated_at: {now_iso()}",
            f"- thresholds: stale>={result.get('stale_days')}d, "
            f"archive>={result.get('archive_days')}d",
            f"- candidates: {len(result.get('candidates') or [])} | "
            f"applied: {len(result.get('applied') or [])}",
            "",
        ]
        for row in result.get("candidates") or []:
            if row.get("candidate_action") != "keep":
                lines.append(f"- {row['candidate_action']}: {row['name']} ({row['reason']})")
        atomic_write_text(report_dir / "report.md", "\n".join(lines) + "\n")
        return str(report_json)
    except Exception:
        return None


def status() -> Dict[str, Any]:
    usage = load_usage()
    state = load_state()
    active_data_dir, data_dir_source = resolve_data_dir()
    return {
        "plugin_root": str(plugin_root()),
        "data_dir": str(active_data_dir),
        "data_dir_source": data_dir_source,
        "skill_roots": [str(p) for p in default_skill_roots()],
        "skill_count": len(list_skills()["skills"]),
        "tracked_skill_count": len(usage.get("skills", {})),
        "tracked_tool_count": len(usage.get("tools", {})),
        # per-session map — a single "global" read would report 0 while a
        # real session's counter sits at the threshold
        "iters_since_review_by_session": {
            k: int((v or {}).get("v") or 0)
            for k, v in usage.get("counters", {})
                             .get("iters_since_review_by_session", {}).items()
        },
        "last_curate_at": state.get("last_curate_at"),
        "last_report_path": state.get("last_report_path"),
        "auto_continue": auto_continue_enabled(),
    }


def load_state() -> Dict[str, Any]:
    state = load_json(state_path(), {})
    return state if isinstance(state, dict) else {}


def save_state(state: Dict[str, Any]) -> None:
    atomic_write_json(state_path(), state)


def mutate_state(mutator: Any) -> Any:
    """Atomic read-modify-write of state.json under the shared lock —
    concurrent hooks (parallel sessions on one PLUGIN_DATA) doing a bare
    load→save would last-writer-win each other's transcript bookkeeping.
    NEVER call other locked helpers (mutate_usage etc.) from the mutator —
    flock on a second fd of the same file self-deadlocks."""
    with usage_lock():
        state = load_state()
        result = mutator(state)
        save_state(state)
        return result


def record_review_signal(signal: Dict[str, Any]) -> None:
    append_jsonl(review_signals_path(), signal)


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
