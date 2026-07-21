#!/usr/bin/env python3
"""Conservative migration of legacy self-improvement plugin data stores.

Only durable per-skill usage and recoverable skill backups become active data.
Everything else is retained as immutable import history, while the target's
live tool telemetry, review counters, and state remain authoritative.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import skill_store
from skill_store import SkillStoreError


COUNT_FIELDS = ("use_count", "view_count", "patch_count")
TARGET_MANAGED_FIELDS = {"last_managed_at", "managed_sig"}
OPERATIONAL_LOCK_NAMES = {
    "usage.lock",
    "backups.lock",
    "usage-lock.sqlite3",
    "usage-lock.sqlite3-journal",
    "usage-lock.sqlite3-wal",
    "usage-lock.sqlite3-shm",
    "backups-lock.sqlite3",
    "backups-lock.sqlite3-journal",
    "backups-lock.sqlite3-wal",
    "backups-lock.sqlite3-shm",
}
HISTORICAL_EXCLUDES = {"backups", *OPERATIONAL_LOCK_NAMES}
IMPORT_MANIFEST = "import.json"
HASH_CHUNK_SIZE = 1024 * 1024
SAFE_BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _source_label(source: Path) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-._")
    return (label or "legacy-store")[:48]


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_existing_path(left: Path, right: Path) -> bool:
    if left.exists() and right.exists():
        try:
            return os.path.samefile(left, right)
        except OSError:
            pass
    return left == right


def _path_is_within_filesystem(path: Path, parent: Path) -> bool:
    """Containment that also respects case-insensitive filesystem aliases."""
    if _path_is_within(path, parent):
        return True
    if not parent.exists():
        return False
    for candidate in (path, *path.parents):
        if candidate.exists() and _same_existing_path(candidate, parent):
            return True
    return False


def _validate_paths(source: Path, target: Path) -> Tuple[Path, Path]:
    source = source.expanduser()
    target = target.expanduser()
    if source.is_symlink():
        raise SkillStoreError(f"Refusing symlinked migration source: {source}")
    if target.is_symlink():
        raise SkillStoreError(f"Refusing symlinked migration target: {target}")
    source = source.resolve()
    target = target.resolve()
    if not source.is_dir():
        raise SkillStoreError(f"Migration source is not an existing directory: {source}")
    if _same_existing_path(source, target):
        raise SkillStoreError("Migration source and target must be different directories.")
    # An ancestor relationship would make either the snapshot or historical
    # import recursively include the destination while it is being created.
    if (
        _path_is_within_filesystem(source, target)
        or _path_is_within_filesystem(target, source)
    ):
        raise SkillStoreError(
            "Migration source and target must not contain one another."
        )
    backup_root = target.parent / f"{target.name}-migration-backups"
    if (
        _same_existing_path(source, backup_root)
        or _path_is_within_filesystem(source, backup_root)
        or _path_is_within_filesystem(backup_root, source)
    ):
        raise SkillStoreError(
            "Migration source must not overlap the target's migration-backup directory."
        )
    return source, target


def _ensure_managed_dir(
    path: Path,
    parent: Path,
    *,
    label: str,
    create: bool = False,
) -> Path:
    """Reject symlink redirection for migration-owned read/write roots."""
    parent = parent.resolve()
    if path.parent.resolve() != parent:
        raise SkillStoreError(f"Unsafe {label} path outside its expected parent: {path}")
    if path.is_symlink():
        raise SkillStoreError(f"Unsafe symlinked {label} directory: {path}")
    if path.exists() and not path.is_dir():
        raise SkillStoreError(f"{label} path is not a directory: {path}")
    if create:
        path.mkdir(parents=False, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise SkillStoreError(f"Unsafe {label} directory after creation: {path}")
    if path.exists() and not _path_is_within(path.resolve(), parent):
        raise SkillStoreError(f"Unsafe {label} directory outside its parent: {path}")
    return path


def _validate_managed_roots(source: Path, target: Path) -> None:
    _ensure_managed_dir(
        source / "backups", source, label="source backups", create=False
    )
    _ensure_managed_dir(
        target / "backups", target, label="target backups", create=False
    )
    _ensure_managed_dir(
        target / "imports", target, label="target imports", create=False
    )
    _ensure_managed_dir(
        target.parent / f"{target.name}-migration-backups",
        target.parent,
        label="migration backups",
        create=False,
    )


def _default_target() -> Path:
    path, _source = skill_store.resolve_data_dir(create=False)
    return Path(path)


def _file_digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_SIZE)
            if not chunk:
                return digest.digest()
            digest.update(chunk)


def _hash_record(digest: "hashlib._Hash", *parts: bytes | str) -> None:
    """Fold an unambiguous length-prefixed record into a tree digest."""
    digest.update(len(parts).to_bytes(4, "big"))
    for part in parts:
        encoded = part.encode("utf-8") if isinstance(part, str) else part
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def tree_content_hash(
    root: Path,
    *,
    exclude_root_manifest: bool = False,
    excluded_paths: Iterable[Path] = (),
) -> str:
    """Hash names, entry types, symlink targets, and file bytes deterministically."""
    root = root.resolve()
    excluded = {path.expanduser().resolve() for path in excluded_paths}
    digest = hashlib.sha256()
    digest.update(b"self-improving-skills-tree-v2\0")
    if not root.exists():
        _hash_record(digest, "missing")
        return digest.hexdigest()
    if not root.is_dir():
        raise SkillStoreError(f"Tree hash root is not a directory: {root}")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        kept_dirs: List[str] = []
        for name in sorted(dirnames):
            path = current / name
            resolved = path.resolve()
            if any(resolved == item or _path_is_within(resolved, item) for item in excluded):
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                _hash_record(digest, "symlink", rel, os.readlink(path))
            else:
                mode = path.lstat().st_mode
                if not stat.S_ISDIR(mode):
                    raise SkillStoreError(
                        f"Unsupported special filesystem entry: {path}"
                    )
                _hash_record(digest, "directory", rel)
                kept_dirs.append(name)
        dirnames[:] = kept_dirs

        for name in sorted(filenames):
            path = current / name
            if current == root and name in OPERATIONAL_LOCK_NAMES:
                continue
            resolved = path.resolve()
            if any(resolved == item or _path_is_within(resolved, item) for item in excluded):
                continue
            rel = path.relative_to(root).as_posix()
            if exclude_root_manifest and rel == "manifest.json":
                continue
            if path.is_symlink():
                _hash_record(digest, "symlink", rel, os.readlink(path))
                continue
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SkillStoreError(
                    f"Unsupported special filesystem entry: {path}"
                )
            _hash_record(digest, "file", rel, _file_digest(path))
    return digest.hexdigest()


def _read_usage(path: Path) -> Tuple[Dict[str, Any], str, Optional[str]]:
    if not path.exists():
        return {"version": 1, "skills": {}, "tools": {}, "counters": {}}, "missing", None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return (
            {"version": 1, "skills": {}, "tools": {}, "counters": {}},
            "malformed",
            f"{path}: unreadable usage.json ({exc.__class__.__name__})",
        )
    if not isinstance(value, dict):
        return (
            {"version": 1, "skills": {}, "tools": {}, "counters": {}},
            "malformed",
            f"{path}: usage.json root is not an object",
        )
    value = copy.deepcopy(value)
    if not isinstance(value.get("skills"), dict):
        value["skills"] = {}
    value.setdefault("tools", {})
    value.setdefault("counters", {})
    value.setdefault("version", 1)
    return value, "ok", None


def _validate_active_usage_file(path: Path, owner: str) -> None:
    if path.is_symlink():
        raise SkillStoreError(f"Refusing symlinked {owner} usage.json: {path}")
    if path.exists() and not path.is_file():
        raise SkillStoreError(f"{owner} usage.json is not a regular file: {path}")


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _parsed_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _choose_time(target: Any, source: Any, *, earliest: bool) -> Any:
    target_time = _parsed_time(target)
    source_time = _parsed_time(source)
    if target_time is None:
        return source if source_time is not None else target
    if source_time is None:
        return target
    if earliest:
        return source if source_time < target_time else target
    return source if source_time > target_time else target


def _absent(record: Dict[str, Any], key: str) -> bool:
    return key not in record or record[key] is None or record[key] == ""


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True or value == 1


def merge_skill_record(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """Merge one skill while keeping target-owned operational metadata."""
    merged = copy.deepcopy(target)
    for key, value in source.items():
        if _absent(merged, key):
            merged[key] = copy.deepcopy(value)

    for field in COUNT_FIELDS:
        merged[field] = max(_count(target.get(field)), _count(source.get(field)))

    if "created_at" in target or "created_at" in source:
        merged["created_at"] = _choose_time(
            target.get("created_at"), source.get("created_at"), earliest=True
        )
    for key in set(target) | set(source):
        if key.startswith("last_") and key not in TARGET_MANAGED_FIELDS:
            merged[key] = _choose_time(target.get(key), source.get(key), earliest=False)

    merged["pinned"] = _flag(target.get("pinned")) or _flag(source.get("pinned"))
    return merged


def _merge_usage(
    target_usage: Dict[str, Any], source_usage: Dict[str, Any], conflicts: List[Dict[str, Any]]
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    merged = copy.deepcopy(target_usage)
    merged.setdefault("version", 1)
    target_skills = target_usage.get("skills")
    if not isinstance(target_skills, dict):
        target_skills = {}
    source_skills = source_usage.get("skills")
    if not isinstance(source_skills, dict):
        source_skills = {}
    merged_skills = copy.deepcopy(target_skills)
    stats = {"source_skills": 0, "target_skills": len(target_skills), "added": 0,
             "merged": 0, "changed": 0, "skipped": 0}

    for raw_name, source_record in sorted(source_skills.items(), key=lambda item: str(item[0])):
        name = str(raw_name)
        if not isinstance(source_record, dict):
            stats["skipped"] += 1
            conflicts.append({
                "type": "malformed_skill_record",
                "skill": name,
                "resolution": "skipped",
            })
            continue
        stats["source_skills"] += 1
        target_record = merged_skills.get(name)
        if not isinstance(target_record, dict):
            if name in merged_skills:
                conflicts.append({
                    "type": "malformed_target_skill_record",
                    "skill": name,
                    "resolution": "replaced_with_source",
                })
            merged_skills[name] = merge_skill_record({}, source_record)
            stats["added"] += 1
            stats["changed"] += 1
            continue
        updated = merge_skill_record(target_record, source_record)
        stats["merged"] += 1
        if updated != target_record:
            stats["changed"] += 1
        merged_skills[name] = updated

    merged["skills"] = merged_skills
    # These are live target-owned structures. Never seed them from the source.
    merged["tools"] = copy.deepcopy(target_usage.get("tools", {}))
    merged["counters"] = copy.deepcopy(target_usage.get("counters", {}))
    return merged, stats


def _load_manifest(path: Path) -> Dict[str, Any]:
    if path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _backup_inventory(root: Path) -> List[Dict[str, Any]]:
    if root.is_symlink():
        raise SkillStoreError(f"Unsafe symlinked backup directory: {root}")
    if not root.is_dir():
        return []
    rows = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.is_symlink():
            continue
        rows.append({
            "id": entry.name,
            "path": entry,
            "hash": tree_content_hash(entry, exclude_root_manifest=True),
            "manifest": _load_manifest(entry / "manifest.json"),
        })
    return rows


def _validated_backup_skill(row: Dict[str, Any]) -> str:
    for entry in row["path"].rglob("*"):
        if entry.is_symlink():
            raise SkillStoreError(
                f"backup contains a symlink and cannot be imported safely: {entry}"
            )
    manifest_name = skill_store.validate_name(str(row["manifest"].get("skill") or ""))
    skill_md = row["path"] / "SKILL.md"
    if not skill_md.is_file() or skill_md.is_symlink():
        raise SkillStoreError("backup has no regular SKILL.md")
    content = skill_md.read_text(encoding="utf-8")
    skill_store.validate_skill_content(content, expected_name=manifest_name)
    metadata, _body = skill_store.parse_frontmatter(content)
    return skill_store.validate_name(str(metadata["name"]))


def _collision_backup_id(
    original_id: str,
    label: str,
    content_hash: str,
    reserved_ids: set[str],
    target_root: Path,
) -> str:
    safe_original = (
        re.sub(r"[^A-Za-z0-9._-]+", "-", original_id).strip("-.") or "backup"
    )
    safe_original = re.sub(r"^[^A-Za-z0-9]+", "", safe_original) or "backup"
    safe_original = safe_original[:96]
    base = f"{safe_original}--imported-{label}-{content_hash[:12]}"
    candidate = base
    suffix = 2
    while candidate.casefold() in reserved_ids or os.path.lexists(target_root / candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _plan_backups(
    source: Path,
    target: Path,
    label: str,
    conflicts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    source_root = source / "backups"
    target_root = target / "backups"
    source_entries = (
        sorted(source_root.iterdir(), key=lambda item: item.name)
        if source_root.is_dir() else []
    )
    target_entries = (
        sorted(target_root.iterdir(), key=lambda item: item.name)
        if target_root.is_dir() else []
    )
    source_rows = _backup_inventory(source_root)
    target_rows = _backup_inventory(target_root)
    reserved_ids = {entry.name.casefold() for entry in target_entries}
    by_hash: Dict[str, str] = {}
    actions: List[Dict[str, Any]] = []
    stats = {"source": len(source_entries), "target": len(target_entries), "imported": 0,
             "deduplicated": 0, "renamed": 0, "skipped": 0}

    source_row_ids = {row["id"] for row in source_rows}
    for entry in source_entries:
        if entry.name in source_row_ids:
            continue
        stats["skipped"] += 1
        conflicts.append({
            "type": "malformed_backup",
            "backup_id": entry.name,
            "resolution": "skipped",
            "detail": "backup entry is not a regular directory",
        })

    target_row_ids = {row["id"] for row in target_rows}
    for entry in target_entries:
        if entry.name in target_row_ids:
            continue
        conflicts.append({
            "type": "malformed_target_backup",
            "backup_id": entry.name,
            "resolution": "retained_and_reserved",
            "detail": "backup entry is not a regular directory",
        })

    for row in target_rows:
        try:
            _validated_backup_skill(row)
        except (OSError, UnicodeError, SkillStoreError) as exc:
            conflicts.append({
                "type": "malformed_target_backup",
                "backup_id": row["id"],
                "resolution": "retained_but_excluded_from_deduplication",
                "detail": str(exc),
            })
            continue
        by_hash.setdefault(row["hash"], row["id"])

    for row in source_rows:
        try:
            skill_name = _validated_backup_skill(row)
        except (OSError, UnicodeError, SkillStoreError) as exc:
            stats["skipped"] += 1
            conflicts.append({
                "type": "malformed_backup",
                "backup_id": row["id"],
                "resolution": "skipped",
                "detail": str(exc),
            })
            continue
        row = dict(row, skill=skill_name)
        duplicate_id = by_hash.get(row["hash"])
        if duplicate_id is not None:
            actions.append(dict(row, action="deduplicate", destination_id=duplicate_id))
            stats["deduplicated"] += 1
            continue

        destination_id = row["id"]
        unsafe_id = SAFE_BACKUP_ID_RE.fullmatch(destination_id) is None
        renamed = (
            unsafe_id
            or destination_id.casefold() in reserved_ids
            or os.path.lexists(target_root / destination_id)
        )
        if renamed:
            destination_id = _collision_backup_id(
                row["id"], label, row["hash"], reserved_ids, target_root
            )
            conflicts.append({
                "type": "unsafe_backup_id" if unsafe_id else "backup_id_collision",
                "backup_id": row["id"],
                "resolution": destination_id,
            })
            stats["renamed"] += 1
        actions.append(dict(row, action="import", destination_id=destination_id))
        stats["imported"] += 1
        reserved_ids.add(destination_id.casefold())
        by_hash[row["hash"]] = destination_id
    return actions, stats


def _historical_entries(source: Path) -> List[Path]:
    return [
        entry for entry in sorted(source.iterdir(), key=lambda item: item.name)
        if entry.name not in HISTORICAL_EXCLUDES
    ]


def _copy_entry(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _ignore_paths(excluded: Iterable[Path]):
    excluded_resolved = {path.resolve() for path in excluded}

    def _ignore(dirpath: str, names: List[str]) -> set:
        ignored = set()
        base = Path(dirpath)
        for name in names:
            candidate = (base / name).resolve()
            if any(candidate == item or _path_is_within(candidate, item)
                   for item in excluded_resolved):
                ignored.add(name)
        return ignored

    return _ignore


def _copy_complete_tree(source: Path, destination: Path, excluded: Iterable[Path]) -> None:
    if source.exists():
        shutil.copytree(
            source,
            destination,
            symlinks=True,
            ignore=_ignore_paths(excluded),
        )


def _unique_snapshot_path(backup_root: Path, label: str) -> Path:
    base = f"{_utc_stamp()}-{label}"
    candidate = backup_root / base
    suffix = 2
    while candidate.exists() or (backup_root / f".{candidate.name}.staging").exists():
        candidate = backup_root / f"{base}-{suffix}"
        suffix += 1
    return candidate


def _snapshot(
    source: Path,
    target: Path,
    label: str,
    *,
    source_hash: str,
    target_hash: str,
    target_existed: bool,
) -> Dict[str, Any]:
    backup_root = target.parent / f"{target.name}-migration-backups"
    _ensure_managed_dir(
        backup_root,
        target.parent,
        label="migration backups",
        create=True,
    )
    destination = _unique_snapshot_path(backup_root, label)
    staging = backup_root / f".{destination.name}.staging"
    try:
        staging.mkdir()
        source_operational = tuple(source / name for name in OPERATIONAL_LOCK_NAMES)
        target_operational = tuple(target / name for name in OPERATIONAL_LOCK_NAMES)
        _copy_complete_tree(
            source,
            staging / "source",
            excluded=(backup_root, *source_operational),
        )
        _copy_complete_tree(
            target,
            staging / "target",
            excluded=(backup_root, *target_operational),
        )
        skill_store.atomic_write_json(staging / "snapshot.json", {
            "version": 1,
            "created_at": skill_store.now_iso(),
            "source": str(source),
            "target": str(target),
            "source_existed": True,
            "target_existed": target_existed,
            "source_content_hash": source_hash,
            "target_content_hash": target_hash,
        })
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "root": str(backup_root),
        "path": str(destination),
        "source": str(destination / "source"),
        "target": str(destination / "target"),
        "metadata": str(destination / "snapshot.json"),
    }


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    with skill_store.legacy_file_lock(path, create=True):
        yield


@contextmanager
def _existing_file_lock(path: Path) -> Iterator[None]:
    """Share a legacy store's lock without creating or writing the source."""
    with skill_store.legacy_file_lock(path, create=False):
        yield


def _archive_matches(destination: Path, source_hash: str) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    payload = destination / "payload"
    if payload.is_symlink() or not payload.is_dir():
        return False
    manifest = _load_manifest(destination / IMPORT_MANIFEST)
    expected_payload_hash = manifest.get("payload_content_hash")
    if (
        manifest.get("source_content_hash") != source_hash
        or not isinstance(expected_payload_hash, str)
    ):
        return False
    try:
        return tree_content_hash(payload) == expected_payload_hash
    except (OSError, SkillStoreError):
        return False


def _archive_history(
    source: Path,
    destination: Path,
    source_hash: str,
    label: str,
    source_identity: Path,
) -> bool:
    if destination.exists():
        if _archive_matches(destination, source_hash):
            return False
        raise SkillStoreError(
            f"Import archive path exists with different metadata: {destination}"
        )

    _ensure_managed_dir(
        destination.parent,
        destination.parent.parent,
        label="target imports",
        create=True,
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        payload = staging / "payload"
        payload.mkdir()
        for entry in _historical_entries(source):
            _copy_entry(entry, payload / entry.name)
        payload_hash = tree_content_hash(payload)
        skill_store.atomic_write_json(staging / IMPORT_MANIFEST, {
            "version": 1,
            "source": str(source_identity),
            "source_label": label,
            "source_content_hash": source_hash,
            "payload_content_hash": payload_hash,
            "imported_at": skill_store.now_iso(),
        })
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return True


def _copy_backup(
    action: Dict[str, Any],
    target: Path,
    source: Path,
    source_hash: str,
) -> None:
    destination = target / "backups" / action["destination_id"]
    _ensure_managed_dir(
        destination.parent,
        target,
        label="target backups",
        create=True,
    )
    staging = Path(tempfile.mkdtemp(prefix=".backup-import.", dir=destination.parent))
    try:
        for entry in sorted(action["path"].iterdir(), key=lambda item: item.name):
            if entry.name == "manifest.json":
                continue
            _copy_entry(entry, staging / entry.name)
        original_manifest = action["manifest"]
        created_at = original_manifest.get("created_at")
        if _parsed_time(created_at) is None:
            created_at = skill_store.now_iso()
        manifest = {
            "backup_id": action["destination_id"],
            "skill": action["skill"],
            "created_at": created_at,
            "original_backup_id": action["id"],
            "imported_from": str(source),
            "imported_source_hash": source_hash,
            "imported_at": skill_store.now_iso(),
        }
        reason = original_manifest.get("reason")
        if isinstance(reason, str) and reason.strip():
            manifest["reason"] = reason[:500]
        skill_store.atomic_write_json(staging / "manifest.json", manifest)
        staging.rename(destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _build_plan(
    source: Path,
    target: Path,
    *,
    source_label: Optional[str] = None,
    source_hash: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_managed_roots(source, target)
    _validate_active_usage_file(source / "usage.json", "source")
    _validate_active_usage_file(target / "usage.json", "target")
    label = source_label or _source_label(source)
    source_hash = source_hash or tree_content_hash(source)
    conflicts: List[Dict[str, Any]] = []
    source_usage, source_status, source_warning = _read_usage(source / "usage.json")
    target_usage, target_status, target_warning = _read_usage(target / "usage.json")
    for warning, owner in ((source_warning, "source"), (target_warning, "target")):
        if warning:
            conflicts.append({
                "type": "malformed_usage",
                "store": owner,
                "resolution": "preserved_in_snapshot_and_import" if owner == "source"
                              else "preserved_in_snapshot",
                "detail": warning,
            })
    merged_usage, usage_stats = _merge_usage(target_usage, source_usage, conflicts)
    backup_actions, backup_stats = _plan_backups(source, target, label, conflicts)
    import_path = target / "imports" / f"{label}-{source_hash}"
    archive_exists = False
    if import_path.is_symlink():
        conflicts.append({
            "type": "import_archive_collision",
            "path": str(import_path),
            "resolution": "apply_refused",
        })
    elif import_path.exists():
        if _archive_matches(import_path, source_hash):
            archive_exists = True
        else:
            conflicts.append({
                "type": "import_archive_collision",
                "path": str(import_path),
                "resolution": "apply_refused",
            })

    return {
        "label": label,
        "source_hash": source_hash,
        "source_usage_status": source_status,
        "target_usage_status": target_status,
        "merged_usage": merged_usage,
        "usage_stats": usage_stats,
        "backup_actions": backup_actions,
        "backup_stats": backup_stats,
        "historical_entries": [entry.name for entry in _historical_entries(source)],
        "import_path": import_path,
        "archive_exists": archive_exists,
        "conflicts": conflicts,
    }


def _public_result(
    source: Path,
    target: Path,
    plan: Dict[str, Any],
    *,
    applied: bool,
    snapshot: Optional[Dict[str, Any]] = None,
    archive_created: bool = False,
) -> Dict[str, Any]:
    return {
        "action": "migrate_data",
        "applied": applied,
        "source": str(source),
        "target": str(target),
        "source_label": plan["label"],
        "source_content_hash": plan["source_hash"],
        "usage": {
            "source_status": plan["source_usage_status"],
            "target_status": plan["target_usage_status"],
            **plan["usage_stats"],
        },
        "backups": plan["backup_stats"],
        "history": {
            "entries": plan["historical_entries"],
            "count": len(plan["historical_entries"]),
            "path": str(plan["import_path"]),
            "payload_path": str(plan["import_path"] / "payload"),
            "already_imported": plan["archive_exists"],
            "created": archive_created,
        },
        "snapshot": snapshot or {
            "root": str(target.parent / f"{target.name}-migration-backups"),
            "path": None,
            "source": None,
            "target": None,
            "metadata": None,
        },
        "conflicts": plan["conflicts"],
    }


def _raise_on_archive_collision(plan: Dict[str, Any]) -> None:
    if any(item.get("type") == "import_archive_collision"
           for item in plan["conflicts"]):
        raise SkillStoreError(
            f"Import archive collision at {plan['import_path']}; "
            "no active data changed."
        )


def _verify_source_snapshot(
    source: Path,
    snapshot_source: Path,
    expected_hash: str,
) -> None:
    snapshot_hash = tree_content_hash(snapshot_source)
    current_hash = tree_content_hash(source)
    if snapshot_hash != expected_hash or current_hash != expected_hash:
        raise SkillStoreError(
            "Migration source changed while it was being snapshotted. "
            "Stop processes that use the legacy store and retry; the safety "
            "snapshot was retained, but no active data was imported."
        )


def _verify_target_snapshot(
    target: Path,
    snapshot_target: Path,
    expected_hash: str,
) -> None:
    snapshot_hash = tree_content_hash(snapshot_target)
    current_hash = tree_content_hash(target)
    if snapshot_hash != expected_hash or current_hash != expected_hash:
        raise SkillStoreError(
            "Migration target changed while it was being snapshotted. "
            "Stop processes that use the active store and retry; the safety "
            "snapshot was retained, but no migration data was imported."
        )


def migrate_data(
    source: os.PathLike[str] | str,
    *,
    apply: bool = False,
    target: Optional[os.PathLike[str] | str] = None,
) -> Dict[str, Any]:
    """Plan or apply a migration into the current plugin data directory."""
    source_path = Path(source)
    target_path = Path(target) if target is not None else _default_target()
    source_path, target_path = _validate_paths(source_path, target_path)

    plan = _build_plan(source_path, target_path)
    if not apply:
        return _public_result(source_path, target_path, plan, applied=False)

    migration_lock = target_path.parent / f".{target_path.name}.migration.lock"
    with _file_lock(migration_lock):
        target_existed = target_path.exists()
        target_locks = (
            target_path / "usage.lock",
            target_path / "backups.lock",
        )
        target_locks_existed = {
            lock: lock.exists() for lock in target_locks
        }
        _validate_managed_roots(source_path, target_path)
        plan = _build_plan(source_path, target_path)
        _raise_on_archive_collision(plan)
        target_hash = tree_content_hash(target_path)
        snapshot = _snapshot(
            source_path,
            target_path,
            plan["label"],
            source_hash=plan["source_hash"],
            target_hash=target_hash,
            target_existed=target_existed,
        )
        snapshot_source = Path(snapshot["source"])
        _verify_source_snapshot(source_path, snapshot_source, plan["source_hash"])
        _verify_target_snapshot(
            target_path,
            Path(snapshot["target"]),
            target_hash,
        )

        if target_path.is_symlink():
            raise SkillStoreError(f"Refusing symlinked migration target: {target_path}")
        target_path.mkdir(parents=True, exist_ok=True)
        if target_path.is_symlink() or not target_path.is_dir():
            raise SkillStoreError(f"Unsafe migration target: {target_path}")
        _validate_managed_roots(source_path, target_path)

        # Every store uses usage -> backups lock order (archive_skill already
        # does this). Store roots remain globally sorted so two migrations in
        # opposite directions cannot deadlock one another.
        lock_specs = []
        for store, create in sorted(
            ((source_path, False), (target_path, True)),
            key=lambda item: str(item[0]),
        ):
            lock_specs.extend((
                (store / "usage.lock", create),
                (store / "backups.lock", create),
            ))
        with ExitStack() as locks:
            for lock_path, create in lock_specs:
                context = _file_lock(lock_path) if create else _existing_file_lock(lock_path)
                locks.enter_context(context)
                kind = lock_path.name.removesuffix(".lock")
                locks.enter_context(
                    skill_store.sqlite_transaction_lock(
                        lock_path.parent,
                        kind,
                        create=create,
                    )
                )

            # Nothing may drift between the exact pre-apply snapshot and the
            # locked write phase. A lock created by this migration is the only
            # allowed target delta.
            _verify_source_snapshot(source_path, snapshot_source, plan["source_hash"])
            if target_existed:
                excluded = tuple(
                    lock for lock in target_locks
                    if not target_locks_existed[lock]
                )
                current_target_hash = tree_content_hash(
                    target_path,
                    excluded_paths=excluded,
                )
                if current_target_hash != target_hash:
                    raise SkillStoreError(
                        "Migration target changed after the pre-apply snapshot. "
                        "No migration data was imported."
                    )
            else:
                unexpected = [
                    entry for entry in target_path.iterdir()
                    if entry.name not in OPERATIONAL_LOCK_NAMES
                ]
                if unexpected:
                    raise SkillStoreError(
                        "Migration target was initialized after the pre-apply "
                        "snapshot. No migration data was imported; retry to "
                        "snapshot and preserve the new target state."
                    )

            # All imports read from the verified immutable snapshot. The live
            # legacy source is never consulted again during this apply.
            plan = _build_plan(
                snapshot_source,
                target_path,
                source_label=plan["label"],
                source_hash=plan["source_hash"],
            )
            _raise_on_archive_collision(plan)
            archive_created = _archive_history(
                snapshot_source,
                plan["import_path"],
                plan["source_hash"],
                plan["label"],
                source_path,
            )
            for action in plan["backup_actions"]:
                if action["action"] == "import":
                    _copy_backup(
                        action, target_path, source_path, plan["source_hash"]
                    )
            skill_store.atomic_write_json(
                target_path / "usage.json", plan["merged_usage"]
            )

    return _public_result(
        source_path,
        target_path,
        plan,
        applied=True,
        snapshot=snapshot,
        archive_created=archive_created,
    )
