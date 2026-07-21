import importlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)

import data_migration
import skill_manager_cli
import skill_store


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _usage(*, skills=None, tools=None, counters=None, **extra):
    return {
        "version": 1,
        "skills": skills or {},
        "tools": tools or {},
        "counters": counters or {},
        **extra,
    }


def _backup(store: Path, backup_id: str, body: str, **manifest) -> Path:
    root = store / "backups" / backup_id
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: test\n---\n{body}\n", encoding="utf-8"
    )
    (root / "references" / "nested.txt").write_text("same nested data\n", encoding="utf-8")
    _write_json(root / "manifest.json", {
        "backup_id": backup_id,
        "skill": "demo",
        "created_at": manifest.pop("created_at", "2026-01-01T00:00:00+00:00"),
        **manifest,
    })
    return root


def _reload():
    importlib.reload(skill_store)
    importlib.reload(data_migration)
    return data_migration


def test_dry_run_writes_nothing_even_when_target_does_not_exist(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    source.mkdir()
    _write_json(source / "usage.json", _usage(skills={
        "demo": {"use_count": 3, "pinned": False},
    }))
    (source / "events.jsonl").write_text('{"type":"tool"}\n', encoding="utf-8")
    source_hash = migration.tree_content_hash(source)
    target = tmp_path / "canonical"

    result = migration.migrate_data(source, target=target)

    assert result["applied"] is False
    assert result["usage"]["added"] == 1
    assert result["snapshot"]["path"] is None
    assert not target.exists()
    assert not (tmp_path / "canonical-migration-backups").exists()
    assert migration.tree_content_hash(source) == source_hash


def test_apply_can_initialize_missing_target_with_operational_locks(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "canonical"
    source.mkdir()
    _write_json(source / "usage.json", _usage(skills={"demo": {"use_count": 3}}))

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["applied"] is True
    assert (target / "usage-lock.sqlite3").is_file()
    assert (target / "backups-lock.sqlite3").is_file()
    merged = json.loads((target / "usage.json").read_text(encoding="utf-8"))
    assert merged["skills"]["demo"]["use_count"] == 3


def test_apply_merges_only_skills_and_archives_history(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy-store"
    target = tmp_path / "canonical-store"
    source.mkdir()
    target.mkdir()
    source_usage = _usage(
        skills={
            "shared": {
                "use_count": 8,
                "view_count": 1,
                "patch_count": 7,
                "created_at": "2023-01-01T00:00:00+00:00",
                "last_used_at": "2026-01-01T00:00:00+00:00",
                "last_viewed_at": "2024-01-01T00:00:00+00:00",
                "last_managed_at": "2026-02-01T00:00:00+00:00",
                "pinned": True,
                "state": "stale",
                "created_by": "agent",
                "create_reason": "legacy reason",
                "managed_sig": [1, 2],
            },
            "source-only": {
                "use_count": 4,
                "created_at": "2022-01-01T00:00:00+00:00",
                "state": "active",
                "pinned": "false",
                "last_managed_at": "2024-04-01T00:00:00+00:00",
                "managed_sig": [4, 4],
            },
        },
        tools={"legacy_tool": {"count": 999}},
        counters={"iters_since_review_by_session": {"legacy": {"v": 50}}},
    )
    target_usage = _usage(
        skills={
            "shared": {
                "use_count": 3,
                "view_count": 9,
                "patch_count": 2,
                "created_at": "2024-01-01T00:00:00+00:00",
                "last_used_at": "2025-01-01T00:00:00+00:00",
                "last_viewed_at": "2025-02-01T00:00:00+00:00",
                "last_managed_at": "2025-03-01T00:00:00+00:00",
                "pinned": False,
                "state": "active",
                "created_by": "unknown",
                "managed_sig": [9, 9],
            }
        },
        tools={"live_tool": {"count": 12}},
        counters={"iters_since_review_by_session": {"live": {"v": 7}}},
        target_extension={"keep": True},
    )
    _write_json(source / "usage.json", source_usage)
    _write_json(target / "usage.json", target_usage)
    (source / "usage.lock").touch()
    (target / "usage.lock").touch()
    (source / "events.jsonl").write_text("event\n", encoding="utf-8")
    (source / "review-signals.jsonl").write_text("signal\n", encoding="utf-8")
    _write_json(source / "state.json", {"legacy": True})
    (source / "snapshots").mkdir()
    (source / "snapshots" / "one.json").write_text("{}\n", encoding="utf-8")
    (source / "logs" / "curator").mkdir(parents=True)
    (source / "logs" / "curator" / "report.md").write_text("old\n", encoding="utf-8")
    (source / "skill_snapshot.json").write_text("{}\n", encoding="utf-8")
    source_hash = migration.tree_content_hash(source)

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["applied"] is True
    assert migration.tree_content_hash(source) == source_hash
    merged = json.loads((target / "usage.json").read_text(encoding="utf-8"))
    shared = merged["skills"]["shared"]
    assert (shared["use_count"], shared["view_count"], shared["patch_count"]) == (8, 9, 7)
    assert shared["created_at"] == "2023-01-01T00:00:00+00:00"
    assert shared["last_used_at"] == "2026-01-01T00:00:00+00:00"
    assert shared["last_viewed_at"] == "2025-02-01T00:00:00+00:00"
    assert shared["last_managed_at"] == "2025-03-01T00:00:00+00:00"
    assert shared["pinned"] is True
    assert shared["state"] == "active"
    assert shared["created_by"] == "unknown"
    assert shared["create_reason"] == "legacy reason"
    assert shared["managed_sig"] == [9, 9]
    assert merged["skills"]["source-only"]["use_count"] == 4
    assert merged["skills"]["source-only"]["pinned"] is False
    assert merged["skills"]["source-only"]["last_managed_at"] == (
        "2024-04-01T00:00:00+00:00"
    )
    assert merged["skills"]["source-only"]["managed_sig"] == [4, 4]
    assert merged["tools"] == target_usage["tools"]
    assert merged["counters"] == target_usage["counters"]
    assert merged["target_extension"] == {"keep": True}

    history = Path(result["history"]["path"])
    payload = Path(result["history"]["payload_path"])
    assert (payload / "usage.json").read_text(encoding="utf-8") == (
        source / "usage.json"
    ).read_text(encoding="utf-8")
    for relative in (
        "events.jsonl",
        "review-signals.jsonl",
        "state.json",
        "snapshots/one.json",
        "logs/curator/report.md",
        "skill_snapshot.json",
    ):
        assert (payload / relative).exists()
    assert (history / "import.json").exists()
    assert not (payload / "usage.lock").exists()

    snapshot = Path(result["snapshot"]["path"])
    assert json.loads((snapshot / "target" / "usage.json").read_text(encoding="utf-8")) == target_usage
    assert migration.tree_content_hash(snapshot / "source") == source_hash


def test_apply_is_idempotent_for_active_data_backups_and_history(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={
        "demo": {"use_count": 5, "last_used_at": "2026-01-01T00:00:00+00:00"}
    }))
    _write_json(target / "usage.json", _usage(skills={"demo": {"use_count": 1}}))
    _backup(source, "legacy-backup", "legacy body")

    first = migration.migrate_data(source, apply=True, target=target)
    first_usage = (target / "usage.json").read_bytes()
    first_backup_names = sorted(path.name for path in (target / "backups").iterdir())
    first_import_names = sorted(path.name for path in (target / "imports").iterdir())
    second = migration.migrate_data(source, apply=True, target=target)

    assert not (source / "usage.lock").exists()
    assert (target / "usage.json").read_bytes() == first_usage
    assert sorted(path.name for path in (target / "backups").iterdir()) == first_backup_names
    assert sorted(path.name for path in (target / "imports").iterdir()) == first_import_names
    assert first["backups"]["imported"] == 1
    assert second["backups"]["imported"] == 0
    assert second["backups"]["deduplicated"] == 1
    assert second["history"]["already_imported"] is True
    assert second["history"]["created"] is False
    snapshots = list((tmp_path / "target-migration-backups").iterdir())
    assert len(snapshots) == 2  # every explicit apply remains recoverable


def test_corrupted_import_payload_is_not_treated_as_already_imported(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={"demo": {"use_count": 2}}))

    first = migration.migrate_data(source, apply=True, target=target)
    payload = Path(first["history"]["payload_path"])
    (payload / "usage.json").unlink()

    dry_run = migration.migrate_data(source, target=target)
    assert dry_run["history"]["already_imported"] is False
    assert any(
        item["type"] == "import_archive_collision"
        for item in dry_run["conflicts"]
    )
    with pytest.raises(skill_store.SkillStoreError, match="archive collision"):
        migration.migrate_data(source, apply=True, target=target)


def test_backup_content_dedup_and_same_id_conflict_rename(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    duplicate_source = _backup(
        source, "source-id", "identical", created_at="2026-02-01T00:00:00+00:00"
    )
    duplicate_target = _backup(
        target, "target-id", "identical", created_at="2025-01-01T00:00:00+00:00"
    )
    conflict_source = _backup(source, "same-id", "source version")
    _backup(target, "same-id", "target version")

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["backups"] == {
        "source": 2,
        "target": 2,
        "imported": 1,
        "deduplicated": 1,
        "renamed": 1,
        "skipped": 0,
    }
    assert migration.tree_content_hash(
        duplicate_source, exclude_root_manifest=True
    ) == migration.tree_content_hash(duplicate_target, exclude_root_manifest=True)
    imported = [
        path for path in (target / "backups").iterdir()
        if path.name.startswith("same-id--imported-legacy-")
    ]
    assert len(imported) == 1
    assert migration.tree_content_hash(
        imported[0], exclude_root_manifest=True
    ) == migration.tree_content_hash(conflict_source, exclude_root_manifest=True)
    manifest = json.loads((imported[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["backup_id"] == imported[0].name
    assert manifest["original_backup_id"] == "same-id"
    assert manifest["imported_from"] == str(source.resolve())
    assert any(item["type"] == "backup_id_collision" for item in result["conflicts"])


def test_valid_source_backup_does_not_deduplicate_to_malformed_target(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    source_backup = _backup(source, "same-id", "recoverable body")
    target_backup = _backup(target, "same-id", "recoverable body")
    (target_backup / "manifest.json").write_text("not json\n", encoding="utf-8")

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["backups"]["deduplicated"] == 0
    assert result["backups"]["imported"] == 1
    imported = [
        path for path in (target / "backups").iterdir()
        if path.name != target_backup.name
    ]
    assert len(imported) == 1
    assert migration.tree_content_hash(
        imported[0], exclude_root_manifest=True
    ) == migration.tree_content_hash(source_backup, exclude_root_manifest=True)
    manifest = json.loads((imported[0] / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["skill"] == "demo"
    assert any(
        item["type"] == "malformed_target_backup" for item in result["conflicts"]
    )


def test_malformed_source_usage_is_preserved_without_corrupting_target(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    malformed = "{ definitely not json\n"
    (source / "usage.json").write_text(malformed, encoding="utf-8")
    target_usage = _usage(
        skills={"live": {"use_count": 2}},
        tools={"live": {"count": 10}},
        counters={"live": 3},
    )
    _write_json(target / "usage.json", target_usage)

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["usage"]["source_status"] == "malformed"
    assert any(item["type"] == "malformed_usage" for item in result["conflicts"])
    assert json.loads((target / "usage.json").read_text(encoding="utf-8")) == target_usage
    payload = Path(result["history"]["payload_path"])
    assert (payload / "usage.json").read_text(encoding="utf-8") == malformed


def test_malformed_target_usage_is_snapshotted_before_recovery(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={"recovered": {"use_count": 6}}))
    malformed = "[not an object]"
    (target / "usage.json").write_text(malformed, encoding="utf-8")

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["usage"]["target_status"] == "malformed"
    assert json.loads((target / "usage.json").read_text(encoding="utf-8"))[
        "skills"
    ]["recovered"]["use_count"] == 6
    snapshot = Path(result["snapshot"]["target"])
    assert (snapshot / "usage.json").read_text(encoding="utf-8") == malformed


def test_missing_usage_is_safe(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    target_usage = _usage(skills={"live": {"use_count": 1}})
    _write_json(target / "usage.json", target_usage)

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["usage"]["source_status"] == "missing"
    assert json.loads((target / "usage.json").read_text(encoding="utf-8")) == target_usage


def test_source_must_not_equal_target(tmp_path):
    migration = _reload()
    store = tmp_path / "same"
    store.mkdir()
    with pytest.raises(skill_store.SkillStoreError, match="must be different"):
        migration.migrate_data(store, target=store)


def test_source_must_not_overlap_snapshot_destination(tmp_path):
    migration = _reload()
    target = tmp_path / "target"
    backup_root = tmp_path / "target-migration-backups"
    backup_root.mkdir()
    with pytest.raises(skill_store.SkillStoreError, match="migration-backup"):
        migration.migrate_data(backup_root, target=target)


@pytest.mark.parametrize("apply_flag", [False, True])
def test_cli_migrate_data_is_dry_run_by_default(monkeypatch, capsys, tmp_path, apply_flag):
    calls = []

    def fake_migrate(source, *, apply=False, target=None):
        calls.append((source, apply, target))
        return {"applied": apply}

    monkeypatch.setattr(skill_manager_cli, "migrate_data", fake_migrate)
    argv = ["skill-manager", "migrate-data", "--source", str(tmp_path / "legacy")]
    if apply_flag:
        argv.append("--apply")
    monkeypatch.setattr(sys, "argv", argv)

    assert skill_manager_cli.main() == 0
    assert calls == [(str(tmp_path / "legacy"), apply_flag, None)]
    assert json.loads(capsys.readouterr().out) == {"applied": apply_flag}


def test_cli_migrate_data_accepts_an_explicit_target(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_migrate(source, *, apply=False, target=None):
        calls.append((source, apply, target))
        return {"applied": apply}

    source = tmp_path / "legacy"
    target = tmp_path / "canonical"
    monkeypatch.setattr(skill_manager_cli, "migrate_data", fake_migrate)
    monkeypatch.setattr(sys, "argv", [
        "skill-manager",
        "migrate-data",
        "--source",
        str(source),
        "--target",
        str(target),
        "--apply",
    ])

    assert skill_manager_cli.main() == 0
    assert calls == [(str(source), True, str(target))]
    assert json.loads(capsys.readouterr().out) == {"applied": True}


def test_tree_hash_uses_unambiguous_file_boundaries(tmp_path):
    migration = _reload()
    one_file = tmp_path / "one-file"
    two_files = tmp_path / "two-files"
    one_file.mkdir()
    two_files.mkdir()

    # A delimiter-only hash can confuse the first payload's suffix with a
    # second file record. The v2 format hashes typed, length-prefixed records.
    (one_file / "a").write_bytes(b"Xb\0F\0Y")
    (two_files / "a").write_bytes(b"X")
    (two_files / "b").write_bytes(b"Y")

    assert migration.tree_content_hash(one_file) != migration.tree_content_hash(
        two_files
    )


def test_source_change_during_snapshot_aborts_before_active_import(
    monkeypatch, tmp_path
):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={"old": {"use_count": 4}}))
    target_usage = _usage(
        skills={"live": {"use_count": 2}},
        tools={"live-tool": {"count": 9}},
        counters={"live": 3},
    )
    _write_json(target / "usage.json", target_usage)
    original_copy = migration._copy_complete_tree

    def drift_after_source_copy(source_arg, destination, excluded):
        original_copy(source_arg, destination, excluded)
        if Path(source_arg) == source and Path(destination).name == "source":
            _write_json(
                source / "usage.json",
                _usage(skills={"changed": {"use_count": 99}}),
            )

    monkeypatch.setattr(migration, "_copy_complete_tree", drift_after_source_copy)

    with pytest.raises(skill_store.SkillStoreError, match="source changed"):
        migration.migrate_data(source, apply=True, target=target)

    assert json.loads((target / "usage.json").read_text(encoding="utf-8")) == target_usage
    assert not (target / "imports").exists()
    assert not (target / "backups").exists()
    snapshots = list((tmp_path / "target-migration-backups").iterdir())
    assert len(snapshots) == 1


def test_target_change_during_snapshot_aborts_before_migration_writes(
    monkeypatch, tmp_path
):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={"old": {"use_count": 4}}))
    target_usage = _usage(skills={"live": {"use_count": 2}})
    _write_json(target / "usage.json", target_usage)
    original_copy = migration._copy_complete_tree

    def drift_after_target_copy(source_arg, destination, excluded):
        original_copy(source_arg, destination, excluded)
        if Path(source_arg) == target and Path(destination).name == "target":
            (target / "events.jsonl").write_text("concurrent\n", encoding="utf-8")

    monkeypatch.setattr(migration, "_copy_complete_tree", drift_after_target_copy)

    with pytest.raises(skill_store.SkillStoreError, match="target changed"):
        migration.migrate_data(source, apply=True, target=target)

    assert json.loads((target / "usage.json").read_text(encoding="utf-8")) == target_usage
    assert (target / "events.jsonl").read_text(encoding="utf-8") == "concurrent\n"
    assert not (target / "imports").exists()
    assert not (target / "backups").exists()


def test_missing_target_initialization_after_snapshot_aborts_without_overwrite(
    monkeypatch, tmp_path
):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    _write_json(source / "usage.json", _usage(skills={"old": {"use_count": 4}}))
    initialized_usage = _usage(
        skills={"live": {"use_count": 8}},
        tools={"live-tool": {"count": 17}},
        counters={"session": {"v": 6}},
    )
    original_lock = migration._file_lock
    injected = False

    @contextmanager
    def initialize_before_target_lock(path):
        nonlocal injected
        path = Path(path)
        if path == target / "usage.lock" and not injected:
            _write_json(target / "usage.json", initialized_usage)
            injected = True
        with original_lock(path):
            yield

    monkeypatch.setattr(migration, "_file_lock", initialize_before_target_lock)

    with pytest.raises(skill_store.SkillStoreError, match="was initialized"):
        migration.migrate_data(source, apply=True, target=target)

    assert json.loads((target / "usage.json").read_text(encoding="utf-8")) == (
        initialized_usage
    )
    assert not (target / "imports").exists()
    assert not (target / "backups").exists()
    snapshots = list((tmp_path / "target-migration-backups").iterdir())
    assert len(snapshots) == 1
    metadata = json.loads((snapshots[0] / "snapshot.json").read_text(encoding="utf-8"))
    assert metadata["target_existed"] is False
    assert not (snapshots[0] / "target").exists()


def test_migration_lock_order_matches_usage_then_backups_hierarchy(
    monkeypatch, tmp_path
):
    migration = _reload()
    source = tmp_path / "a-source"
    target = tmp_path / "z-target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    original_file_lock = migration._file_lock
    original_existing_lock = migration._existing_file_lock
    entered = []

    @contextmanager
    def track_file_lock(path):
        path = Path(path)
        if path.name in {"usage.lock", "backups.lock"}:
            entered.append(path)
        with original_file_lock(path):
            yield

    @contextmanager
    def track_existing_lock(path):
        path = Path(path)
        if path.name in {"usage.lock", "backups.lock"}:
            entered.append(path)
        with original_existing_lock(path):
            yield

    monkeypatch.setattr(migration, "_file_lock", track_file_lock)
    monkeypatch.setattr(migration, "_existing_file_lock", track_existing_lock)

    migration.migrate_data(source, apply=True, target=target)

    assert entered == [
        source / "usage.lock",
        source / "backups.lock",
        target / "usage.lock",
        target / "backups.lock",
    ]


def test_legacy_import_manifest_is_preserved_inside_payload(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    legacy_manifest = b'{"legacy": true, "note": "keep me"}\n'
    (source / "import.json").write_bytes(legacy_manifest)

    result = migration.migrate_data(source, apply=True, target=target)

    history = Path(result["history"]["path"])
    payload = Path(result["history"]["payload_path"])
    assert (payload / "import.json").read_bytes() == legacy_manifest
    generated = json.loads((history / "import.json").read_text(encoding="utf-8"))
    assert generated["source_content_hash"] == result["source_content_hash"]
    assert generated["source"] == str(source.resolve())


def test_operational_lock_databases_and_journals_are_not_archived(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    for name in migration.OPERATIONAL_LOCK_NAMES:
        (source / name).touch(mode=0o600)

    result = migration.migrate_data(source, apply=True, target=target)

    payload = Path(result["history"]["payload_path"])
    snapshot = Path(result["snapshot"]["source"])
    for name in migration.OPERATIONAL_LOCK_NAMES:
        assert not (payload / name).exists()
        assert not (snapshot / name).exists()


def test_backup_manifest_paths_are_not_trusted_and_unsafe_backups_are_skipped(
    tmp_path,
):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    outside = tmp_path / "outside.txt"
    source.mkdir()
    target.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    _write_json(source / "usage.json", _usage())
    valid = _backup(
        source,
        "valid",
        "valid body",
        source="/tmp/attacker-controlled",
        reason="legacy reason",
    )
    mismatched = _backup(source, "mismatched", "wrong skill")
    mismatched_manifest = json.loads(
        (mismatched / "manifest.json").read_text(encoding="utf-8")
    )
    mismatched_manifest["skill"] = "another-skill"
    _write_json(mismatched / "manifest.json", mismatched_manifest)
    linked = _backup(source, "linked", "linked body")
    (linked / "references" / "outside.txt").symlink_to(outside)

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["backups"]["source"] == 3
    assert result["backups"]["imported"] == 1
    assert result["backups"]["skipped"] == 2
    imported_manifest = json.loads(
        (target / "backups" / valid.name / "manifest.json").read_text(encoding="utf-8")
    )
    assert imported_manifest["skill"] == "demo"
    assert "source" not in imported_manifest
    assert imported_manifest["imported_from"] == str(source.resolve())
    assert imported_manifest["reason"] == "legacy reason"
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert sum(
        item["type"] == "malformed_backup" for item in result["conflicts"]
    ) == 2


def test_backup_without_skill_file_is_preserved_only_in_snapshot(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    broken = _backup(source, "broken", "temporary body")
    (broken / "SKILL.md").unlink()

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["backups"]["source"] == 1
    assert result["backups"]["imported"] == 0
    assert result["backups"]["skipped"] == 1
    assert not (target / "backups").exists()
    assert (Path(result["snapshot"]["source"]) / "backups" / "broken").is_dir()


def test_non_directory_target_backup_id_is_reserved_and_source_is_renamed(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    source_backup = _backup(source, "same-id", "source body")
    (target / "backups").mkdir()
    occupied = target / "backups" / "same-id"
    occupied.write_text("reserved\n", encoding="utf-8")

    result = migration.migrate_data(source, apply=True, target=target)

    assert occupied.read_text(encoding="utf-8") == "reserved\n"
    assert result["backups"]["renamed"] == 1
    imported = [
        entry for entry in (target / "backups").iterdir()
        if entry.name != "same-id"
    ]
    assert len(imported) == 1
    assert migration.tree_content_hash(
        imported[0], exclude_root_manifest=True
    ) == migration.tree_content_hash(source_backup, exclude_root_manifest=True)


def test_case_alias_backup_id_is_treated_as_occupied(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    source_backup = _backup(source, "Same-ID", "source body")
    target_backup = _backup(target, "same-id", "target body")
    alias = target_backup.parent / "Same-ID"
    if not alias.exists() or not os.path.samefile(alias, target_backup):
        pytest.skip("filesystem is case-sensitive")

    result = migration.migrate_data(source, apply=True, target=target)

    assert result["backups"]["renamed"] == 1
    assert target_backup.is_dir()
    imported = [
        entry for entry in (target / "backups").iterdir()
        if not os.path.samefile(entry, target_backup)
    ]
    assert len(imported) == 1
    assert migration.tree_content_hash(
        imported[0], exclude_root_manifest=True
    ) == migration.tree_content_hash(source_backup, exclude_root_manifest=True)


def test_planned_backup_ids_are_reserved_case_insensitively(monkeypatch, tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    _backup(source, "upper", "first body")
    _backup(source, "lower", "second body")
    original_inventory = migration._backup_inventory

    def case_alias_inventory(root):
        rows = original_inventory(root)
        if Path(root) == source / "backups":
            return [
                dict(rows[0], id="Same-ID"),
                dict(rows[1], id="same-id"),
            ]
        return rows

    monkeypatch.setattr(migration, "_backup_inventory", case_alias_inventory)
    conflicts = []

    actions, stats = migration._plan_backups(
        source,
        target,
        "legacy",
        conflicts,
    )

    assert stats["imported"] == 2
    assert stats["renamed"] == 1
    destination_ids = [action["destination_id"] for action in actions]
    assert len({backup_id.casefold() for backup_id in destination_ids}) == 2


def test_generated_collision_id_never_reuses_malformed_occupied_path(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage())
    _write_json(target / "usage.json", _usage())
    source_backup = _backup(source, "same-id", "source body")
    _backup(target, "same-id", "different target body")
    source_hash = migration.tree_content_hash(
        source_backup, exclude_root_manifest=True
    )
    occupied_id = f"same-id--imported-legacy-{source_hash[:12]}"
    malformed = _backup(target, occupied_id, "source body")
    (malformed / "manifest.json").write_text("malformed\n", encoding="utf-8")

    result = migration.migrate_data(source, apply=True, target=target)

    imported_id = f"{occupied_id}-2"
    assert result["backups"]["renamed"] == 1
    assert malformed.is_dir()
    assert (target / "backups" / imported_id).is_dir()


def test_target_tool_and_counter_shapes_are_preserved_verbatim(tmp_path):
    migration = _reload()
    source = tmp_path / "legacy"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    _write_json(source / "usage.json", _usage(skills={"old": {"use_count": 3}}))
    target_usage = {
        "version": 7,
        "skills": {"live": {"use_count": 5}},
        "tools": ["future", "schema"],
        "counters": "opaque-counter-state",
    }
    _write_json(target / "usage.json", target_usage)

    migration.migrate_data(source, apply=True, target=target)

    merged = json.loads((target / "usage.json").read_text(encoding="utf-8"))
    assert merged["tools"] == target_usage["tools"]
    assert merged["counters"] == target_usage["counters"]
    assert merged["skills"]["live"]["use_count"] == 5
    assert merged["skills"]["old"]["use_count"] == 3


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
@pytest.mark.parametrize("owner", ["source", "target"])
def test_symlinked_active_usage_file_is_refused(tmp_path, owner):
    migration = _reload()
    source = tmp_path / "source"
    target = tmp_path / "target"
    external = tmp_path / "external-usage.json"
    source.mkdir()
    target.mkdir()
    _write_json(external, _usage(skills={"outside": {"use_count": 999}}))
    selected = source if owner == "source" else target
    (selected / "usage.json").symlink_to(external)

    with pytest.raises(skill_store.SkillStoreError, match=f"symlinked {owner}"):
        migration.migrate_data(source, target=target)


def test_case_alias_of_same_existing_directory_is_refused(tmp_path):
    migration = _reload()
    source = tmp_path / "Case-Sensitive-Probe"
    source.mkdir()
    alias = Path(str(source).upper())
    if not alias.exists() or not os.path.samefile(source, alias):
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(skill_store.SkillStoreError, match="must be different"):
        migration.migrate_data(source, target=alias)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_special_files_are_rejected_without_being_opened(tmp_path):
    migration = _reload()
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    os.mkfifo(source / "events.fifo")

    with pytest.raises(skill_store.SkillStoreError, match="special filesystem"):
        migration.migrate_data(source, target=target)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_symlinked_source_and_target_roots_are_refused(tmp_path):
    migration = _reload()
    real_source = tmp_path / "real-source"
    real_target = tmp_path / "real-target"
    source_link = tmp_path / "source-link"
    target_link = tmp_path / "target-link"
    real_source.mkdir()
    real_target.mkdir()
    source_link.symlink_to(real_source, target_is_directory=True)
    target_link.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(skill_store.SkillStoreError, match="symlinked migration source"):
        migration.migrate_data(source_link, target=tmp_path / "target")
    with pytest.raises(skill_store.SkillStoreError, match="symlinked migration target"):
        migration.migrate_data(real_source, target=target_link)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
@pytest.mark.parametrize("managed_name", ["backups", "imports"])
def test_symlinked_target_managed_roots_are_refused(tmp_path, managed_name):
    migration = _reload()
    source = tmp_path / "source"
    target = tmp_path / "target"
    external = tmp_path / "external"
    source.mkdir()
    target.mkdir()
    external.mkdir()
    (target / managed_name).symlink_to(external, target_is_directory=True)

    with pytest.raises(skill_store.SkillStoreError, match="symlinked"):
        migration.migrate_data(source, target=target)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
def test_symlinked_source_backups_and_snapshot_root_are_refused(tmp_path):
    migration = _reload()
    source = tmp_path / "source"
    target = tmp_path / "target"
    external = tmp_path / "external"
    source.mkdir()
    target.mkdir()
    external.mkdir()
    (source / "backups").symlink_to(external, target_is_directory=True)

    with pytest.raises(skill_store.SkillStoreError, match="symlinked source backups"):
        migration.migrate_data(source, target=target)

    (source / "backups").unlink()
    snapshot_root = tmp_path / "target-migration-backups"
    snapshot_root.symlink_to(external, target_is_directory=True)
    with pytest.raises(skill_store.SkillStoreError, match="symlinked migration backups"):
        migration.migrate_data(source, target=target)
