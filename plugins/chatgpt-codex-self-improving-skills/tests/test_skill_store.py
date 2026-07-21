import datetime
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS_DIR)


def _skill(root, name, extra_frontmatter=""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: {0}\ndescription: test skill\n{1}---\nbody\n".format(
            name, extra_frontmatter
        ),
        encoding="utf-8",
    )
    return d


def _iso_days_ago(days):
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - datetime.timedelta(days=days)).isoformat()


def _store(tmp_path, monkeypatch, *roots):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_SELF_IMPROVE_SKILL_ROOTS", os.pathsep.join(str(r) for r in roots))
    monkeypatch.setenv("CODEX_SELF_IMPROVE_CREATE_ROOT", str(roots[0]))
    monkeypatch.delenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", raising=False)
    import skill_store

    return importlib.reload(skill_store)


def test_explicit_plugin_data_wins_over_installed_cache(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit-data"
    installed_root = (
        tmp_path / ".codex" / "plugins" / "cache" / "market" /
        "chatgpt-codex-self-improving-skills" / "0.4.0"
    )
    monkeypatch.setenv("PLUGIN_DATA", str(explicit))
    monkeypatch.setenv("PLUGIN_ROOT", str(installed_root))
    import skill_store

    store = importlib.reload(skill_store)
    path, source = store.resolve_data_dir(create=False)
    assert path == explicit.resolve()
    assert source == "plugin_data_env"
    assert not explicit.exists()  # side-effect-free lookup for dry runs


def test_installed_cache_derives_official_plugin_data_path(tmp_path, monkeypatch):
    codex_home = tmp_path / "custom-codex-home"
    installed_root = (
        codex_home / "plugins" / "cache" / "samton-plugins" /
        "chatgpt-codex-self-improving-skills" / "0.4.0"
    )
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.setenv("PLUGIN_ROOT", str(installed_root))
    import skill_store

    store = importlib.reload(skill_store)
    path, source = store.resolve_data_dir(create=False)
    assert path == (
        codex_home / "plugins" / "data" /
        "chatgpt-codex-self-improving-skills-samton-plugins"
    ).resolve()
    assert source == "codex_plugin_cache"
    assert not path.exists()


def test_source_checkout_keeps_legacy_home_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.setenv("PLUGIN_ROOT", str(tmp_path / "src" / "plugin"))
    import skill_store

    store = importlib.reload(skill_store)
    path, source = store.resolve_data_dir(create=False)
    assert path == (tmp_path / ".self-improving-skills").resolve()
    assert source == "legacy_home"
    assert not path.exists()


def test_user_home_skips_relative_home_for_absolute_userprofile(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "relative-home")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import skill_store

    store = importlib.reload(skill_store)
    assert store.user_home() == tmp_path.resolve()


def test_user_home_never_resolves_relative_path_against_cwd(monkeypatch):
    monkeypatch.setenv("HOME", "relative-home")
    monkeypatch.setenv("USERPROFILE", "relative-profile")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("relative-path-home")))
    import skill_store

    store = importlib.reload(skill_store)
    try:
        resolved = store.user_home()
    except store.SkillStoreError:
        return
    assert resolved.is_absolute()
    assert resolved != (Path.cwd() / "relative-path-home").resolve()


def test_review_mode_defaults_background_and_mode_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CODEX_SELF_IMPROVE_MODE", raising=False)
    monkeypatch.delenv("CODEX_SELF_IMPROVE_AUTO", raising=False)
    import skill_store

    store = importlib.reload(skill_store)
    assert store.resolve_review_mode() == ("background", False)
    assert store.auto_continue_enabled() is False

    monkeypatch.setenv("CODEX_SELF_IMPROVE_MODE", "foreground")
    monkeypatch.setenv("CODEX_SELF_IMPROVE_AUTO", "0")
    assert store.resolve_review_mode() == ("foreground", False)
    assert store.auto_continue_enabled() is True
    status = store.status()
    assert status["review_mode"] == "foreground"
    assert status["automatic_review"] is True
    assert status["auto_continue"] is True
    assert status["mode_invalid"] is False

    monkeypatch.setenv("CODEX_SELF_IMPROVE_MODE", "unexpected")
    monkeypatch.setenv("CODEX_SELF_IMPROVE_AUTO", "1")
    status = store.status()
    assert status["review_mode"] == "off"
    assert status["automatic_review"] is False
    assert status["auto_continue"] is False
    assert status["mode_invalid"] is True


def test_legacy_auto_maps_to_background_or_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CODEX_SELF_IMPROVE_MODE", raising=False)
    import skill_store

    store = importlib.reload(skill_store)
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("CODEX_SELF_IMPROVE_AUTO", value)
        assert store.resolve_review_mode() == ("background", False)
        assert store.auto_continue_enabled() is False
    for value in ("0", "false", "no", "off", "", "unexpected"):
        monkeypatch.setenv("CODEX_SELF_IMPROVE_AUTO", value)
        assert store.resolve_review_mode() == ("off", False)
        assert store.auto_continue_enabled() is False
    status = store.status()
    assert status["auto_continue"] is False
    assert status["data_dir_source"] == "plugin_data_env"


def test_status_does_not_report_reused_worker_pid_as_active(tmp_path, monkeypatch):
    roots = [tmp_path / "skills"]
    roots[0].mkdir()
    store = _store(tmp_path, monkeypatch, *roots)
    import review_queue

    identities = {12345: "original"}
    monkeypatch.setattr(review_queue, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(review_queue, "_pid_identity", lambda pid: identities.get(int(pid)))
    queue = review_queue.ReviewQueue()
    assert queue.acquire_worker_lease("old-worker", pid=12345) is True
    identities[12345] = "reused-by-another-process"

    worker = store.status()["worker"]

    assert worker["active"] is False
    assert worker["state"] == "stale"


def test_default_create_root_is_codex_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PLUGIN_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("CODEX_SELF_IMPROVE_SKILL_ROOTS", raising=False)
    monkeypatch.delenv("CODEX_SELF_IMPROVE_CREATE_ROOT", raising=False)
    import skill_store

    store = importlib.reload(skill_store)
    content = "---\nname: codex-born\ndescription: created in the Codex skill root\n---\nbody\n"
    result = store.create_skill("codex-born", content)

    expected = tmp_path / ".codex" / "skills" / "codex-born"
    assert result["path"] == str(expected.resolve())
    assert (expected / "SKILL.md").is_file()


def test_write_roots_keep_repo_skills_readable_but_immutable(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo-skills"
    user_root = tmp_path / "user-skills"
    repo_skill = _skill(repo_root, "repo-owned")
    _skill(user_root, "user-owned")
    store = _store(tmp_path, monkeypatch, repo_root, user_root)
    monkeypatch.setenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", str(user_root))

    assert {row["name"] for row in store.list_skills()["skills"]} == {
        "repo-owned", "user-owned"
    }
    assert store.view_skill("repo-owned")["name"] == "repo-owned"

    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.patch_skill("repo-owned", "body", "changed")
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.write_support_file("repo-owned", "references/note.md", "changed")
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.pin_skill("repo-owned")
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.archive_skill("repo-owned")

    assert "body\n" in (repo_skill / "SKILL.md").read_text(encoding="utf-8")
    assert store.patch_skill("user-owned", "body", "changed")["action"] == "patch"


def test_write_roots_limit_create_and_block_symlink_escape(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    store = _store(tmp_path, monkeypatch, allowed)
    monkeypatch.setenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", str(allowed))
    content = "---\nname: safe\ndescription: d\n---\nbody\n"

    assert store.create_skill("safe", content)["action"] == "create"
    blocked_content = "---\nname: blocked\ndescription: d\n---\nbody\n"
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.create_skill("blocked", blocked_content, root=str(outside))

    missing_outside = tmp_path / "missing-outside"
    monkeypatch.setenv("CODEX_SELF_IMPROVE_CREATE_ROOT", str(missing_outside))
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.create_skill("blocked", blocked_content)
    assert not missing_outside.exists()

    escaped_root = allowed / "escaped-root"
    os.symlink(str(outside), str(escaped_root))
    escaped_content = "---\nname: escaped\ndescription: d\n---\nbody\n"
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.create_skill("escaped", escaped_content, root=str(escaped_root))
    assert not (outside / "escaped").exists()


def test_write_roots_limit_restore_and_rollback_targets(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo-skills"
    user_root = tmp_path / "user-skills"
    repo_skill = _skill(repo_root, "repo-backup")
    _skill(repo_root / ".archive", "repo-archived")
    _skill(user_root, "user-restorable")
    store = _store(tmp_path, monkeypatch, repo_root, user_root)
    backup_id = store.backup_skill(repo_skill, reason="test")["backup_id"]
    monkeypatch.setenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", str(user_root))

    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.restore_skill("repo-archived", root=str(repo_root))
    with pytest.raises(store.SkillStoreError, match="outside configured write roots"):
        store.restore_backup(backup_id)

    store.archive_skill("user-restorable")
    assert store.restore_skill("user-restorable")["action"] == "restore"


def test_empty_write_roots_disable_mutations(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "read-only")
    store = _store(tmp_path, monkeypatch, root)
    monkeypatch.setenv("CODEX_SELF_IMPROVE_WRITE_ROOTS", "")

    assert store.view_skill("read-only")["name"] == "read-only"
    with pytest.raises(store.SkillStoreError, match=r"write roots: \(none\)"):
        store.pin_skill("read-only")


def test_curate_protects_untracked_user_skills_and_skips_system_root(tmp_path, monkeypatch):
    agents = tmp_path / ".agents" / "skills"
    codex = tmp_path / ".codex" / "skills"
    _skill(agents, "handmade")
    _skill(codex / ".system", "bundled")
    store = _store(tmp_path, monkeypatch, agents, codex)

    listed = store.list_skills()["skills"]
    assert [row["name"] for row in listed] == ["handmade"]

    result = store.curate(dry_run=True, stale_days=0, archive_days=0)
    row = result["candidates"][0]
    assert row["name"] == "handmade"
    assert row["candidate_action"] == "keep"
    assert row["created_by"] == "user"
    assert "protected user" in row["reason"]


def test_agent_archive_candidates_and_proven_usage_threshold(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "unproven")
    _skill(root, "proven")
    store = _store(tmp_path, monkeypatch, root)

    store.record_usage("unproven", created_by="agent")
    store.record_usage("proven", created_by="agent")

    with store.usage_lock():
        usage = store.load_usage()
        usage["skills"]["unproven"].update(
            created_at=_iso_days_ago(121), last_used_at=_iso_days_ago(120), use_count=2
        )
        usage["skills"]["proven"].update(
            created_at=_iso_days_ago(121), last_used_at=_iso_days_ago(120), use_count=3
        )
        store.save_usage(usage)

    rows = {row["name"]: row for row in store.curate(dry_run=True)["candidates"]}
    assert rows["unproven"]["candidate_action"] == "archive"
    assert rows["proven"]["candidate_action"] == "mark_stale"


def test_record_usage_reactivates_stale_skill(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "reactive")
    store = _store(tmp_path, monkeypatch, root)
    store.record_usage("reactive", created_by="agent")
    with store.usage_lock():
        usage = store.load_usage()
        usage["skills"]["reactive"]["state"] = "stale"
        store.save_usage(usage)

    store.record_usage("reactive", use=True)
    assert store.load_usage()["skills"]["reactive"]["state"] == "active"


def test_frontmatter_pin_blocks_archive_candidate(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "pinned", extra_frontmatter="pinned: true\n")
    store = _store(tmp_path, monkeypatch, root)
    store.record_usage("pinned", created_by="agent")
    with store.usage_lock():
        usage = store.load_usage()
        usage["skills"]["pinned"]["created_at"] = _iso_days_ago(201)
        usage["skills"]["pinned"]["last_used_at"] = _iso_days_ago(200)
        store.save_usage(usage)

    row = store.curate(dry_run=True)["candidates"][0]
    assert row["candidate_action"] == "keep"
    assert row["pinned"] is True


def test_view_counts_use_and_view(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "loaded")
    store = _store(tmp_path, monkeypatch, root)
    store.view_skill("loaded")
    rec = store.load_usage()["skills"]["loaded"]
    assert rec["view_count"] == 1
    assert rec["use_count"] == 1  # loading is behavioural intent (Hermes rule)


def test_create_stamps_provenance_and_reason(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    content = "---\nname: fresh\ndescription: d\n---\nbody\n"
    result = store.create_skill("fresh", content, reason="captured retry ladder")
    text = (root / "fresh" / "SKILL.md").read_text(encoding="utf-8")
    assert "provenance: self-improving-skills" in text
    assert store.load_usage()["skills"]["fresh"]["create_reason"] == "captured retry ladder"
    assert result["action"] == "create"
    # an author-managed metadata block is never touched
    content2 = "---\nname: meta-owner\ndescription: d\nmetadata:\n  foo: bar\n---\nbody\n"
    store.create_skill("meta-owner", content2)
    text2 = (root / "meta-owner" / "SKILL.md").read_text(encoding="utf-8")
    assert "provenance" not in text2


def test_provenance_stamp_keeps_curation_eligibility(tmp_path, monkeypatch):
    """usage.json lost → the frontmatter stamp alone must keep the skill on
    the curator's agent-created track."""
    root = tmp_path / "skills"
    _skill(root, "orphan",
           extra_frontmatter="metadata:\n  provenance: self-improving-skills\n")
    store = _store(tmp_path, monkeypatch, root)
    # no usage record at all (created_by defaults to "user")
    rows = {r["name"]: r for r in store.curate(dry_run=True, stale_days=0, archive_days=0)["candidates"]}
    assert rows["orphan"]["candidate_action"] == "archive"


def test_provenance_mention_in_body_is_not_a_stamp(tmp_path, monkeypatch):
    """A user skill whose BODY mentions the marker string must stay protected
    (codex review R1: raw head-substring check false-positive)."""
    root = tmp_path / "skills"
    d = root / "essay"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: essay\ndescription: d\n---\n"
        "This skill discusses provenance: self-improving-skills markers.\n",
        encoding="utf-8")
    store = _store(tmp_path, monkeypatch, root)
    rows = {r["name"]: r for r in store.curate(dry_run=True, stale_days=0, archive_days=0)["candidates"]}
    assert rows["essay"]["candidate_action"] == "keep"
    assert "protected" in rows["essay"]["reason"]


def test_rollback_preserves_nested_manifest_json(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "manifesty")
    store = _store(tmp_path, monkeypatch, root)
    store.view_skill("manifesty")
    store.write_support_file("manifesty", "references/manifest.json", "{\"keep\": true}")
    backups = store.list_backups(skill="manifesty")["backups"]
    # patch SKILL.md (new backup contains the nested manifest.json)
    store.patch_skill("manifesty", "body", "body v2")
    latest = store.list_backups(skill="manifesty")["backups"][-1]["backup_id"]
    store.restore_backup(latest)
    assert (root / "manifesty" / "references" / "manifest.json").is_file()
    assert not (root / "manifesty" / "manifest.json").exists()  # root metadata excluded
    assert backups is not None


def test_archive_collision_suffix_and_restore(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "dupe")
    store = _store(tmp_path, monkeypatch, root)
    store.archive_skill("dupe")
    _skill(root, "dupe")  # recreate and archive again → collision
    second = store.archive_skill("dupe")
    assert (root / ".archive" / "dupe").is_dir()
    suffixed = second["path"]
    assert suffixed != str(root / ".archive" / "dupe")
    rec = store.load_usage()["skills"]["dupe"]
    assert rec["archived_as"].startswith("dupe-")
    # restore prefers the exact bare name first
    store.restore_skill("dupe")
    assert (root / "dupe").is_dir()
    store.archive_skill("dupe")  # bare slot free again? no — suffixed remains
    # now only timestamp-suffixed archives handled: restore normalizes input
    restored = store.restore_skill(rec["archived_as"])
    assert restored["name"] == "dupe"
    assert (root / "dupe").is_dir()


def test_restore_never_swallows_sibling_prefix(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    (root / ".archive" / "git-helpers").mkdir(parents=True)
    try:
        store.restore_skill("git")
    except store.SkillStoreError:
        pass
    else:
        raise AssertionError("restore('git') must not match 'git-helpers'")
    assert (root / ".archive" / "git-helpers").is_dir()


def test_description_hard_cap_and_advisory(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    over = "---\nname: chatty\ndescription: {0}\n---\nbody\n".format("x" * 1100)
    try:
        store.create_skill("chatty", over)
    except store.SkillStoreError as exc:
        assert "description exceeds" in str(exc)
    else:
        raise AssertionError("hard cap not enforced")
    longish = "---\nname: chatty\ndescription: {0}\n---\nbody\n".format("x" * 300)
    result = store.create_skill("chatty", longish)
    assert "advisory" in result and "routing" in result["advisory"]


def test_patch_mismatch_includes_preview(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "target")
    store = _store(tmp_path, monkeypatch, root)
    try:
        store.patch_skill("target", "NOT IN FILE", "x")
    except store.SkillStoreError as exc:
        assert "File starts with" in str(exc)
        assert "name: target" in str(exc)  # actual head shown for self-correction
    else:
        raise AssertionError("mismatch must raise")


def test_backup_list_restore_prune_roundtrip(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "precious")
    store = _store(tmp_path, monkeypatch, root)
    original = (root / "precious" / "SKILL.md").read_text(encoding="utf-8")
    store.patch_skill("precious", "body", "body v2")  # takes a pre-patch backup
    backups = store.list_backups(skill="precious")["backups"]
    assert len(backups) == 1
    result = store.restore_backup(backups[0]["backup_id"])
    assert (root / "precious" / "SKILL.md").read_text(encoding="utf-8") == original
    assert result["undo_backup"]  # the rollback is itself undoable
    # the backup dir's manifest.json must not leak into the restored skill
    assert not (root / "precious" / "manifest.json").exists()
    # prune keeps the newest N per skill and never removes a protected id
    all_ids = [b["backup_id"] for b in store.list_backups()["backups"]]
    assert len(all_ids) == 2  # pre-patch backup + pre-restore undo backup
    pruned = store.prune_backups(keep_per_skill=1, protect=[all_ids[0]])
    assert all_ids[0] not in pruned["removed"]
    remaining = [b["backup_id"] for b in store.list_backups()["backups"]]
    assert all_ids[0] in remaining


def test_review_counter_bump_and_reset_on_skill_work(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    for _ in range(3):
        store.bump_review_counter()
    assert store.get_review_counter() == 3
    store.create_skill("resetting", "---\nname: resetting\ndescription: d\n---\nbody\n")
    assert store.get_review_counter() == 0  # real skill work restarts the clock


def test_curate_persists_report_and_state(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    _skill(root, "any")
    store = _store(tmp_path, monkeypatch, root)
    result = store.curate(dry_run=True)
    assert result["report_path"] and os.path.isfile(result["report_path"])
    state = store.load_state()
    assert state["last_curate_at"]
    assert state["last_report_path"] == result["report_path"]


def test_create_result_carries_scan(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    secret = "ghp_" + "a" * 36
    leaky = "---\nname: leaky\ndescription: d\n---\ntoken " + secret + "\n"
    result = store.create_skill("leaky", leaky)
    assert result["scan"]["blocking"] >= 1  # surfaced, but the write succeeded
    assert (root / "leaky" / "SKILL.md").is_file()
    # the finding replayed into MCP/CLI results must not carry the credential
    assert secret not in str(result["scan"])


def test_view_refuses_symlink_escape(tmp_path, monkeypatch):
    """codex review R2: a lexically-safe relative path that is a symlink out
    of the skill dir must not become an arbitrary local-file read."""
    root = tmp_path / "skills"
    d = _skill(root, "sneaky")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    refs = d / "references"
    refs.mkdir()
    os.symlink(str(outside), str(refs / "escape.md"))
    store = _store(tmp_path, monkeypatch, root)
    try:
        store.view_skill("sneaky", file_path="references/escape.md")
    except store.SkillStoreError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("symlink escape must be refused")


def test_restore_preserves_legit_timestamp_suffixed_name(tmp_path, monkeypatch):
    """codex review R2: a skill LEGITIMATELY named '<x>-<14 digits>' must
    restore under its own name, not get stripped to '<x>'."""
    root = tmp_path / "skills"
    legit = "report-20260713010203"
    _skill(root, legit)
    store = _store(tmp_path, monkeypatch, root)
    store.record_usage(legit, created_by="agent")
    store.archive_skill(legit)
    res = store.restore_skill(legit)
    assert res["name"] == legit  # frontmatter name wins — no stripping
    assert (root / legit).is_dir()
    assert not (root / "report").exists()


def test_consume_review_counter_is_atomic_read_and_zero(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    for _ in range(4):
        store.bump_review_counter()
    assert store.consume_review_counter() == 4
    assert store.get_review_counter() == 0


def _process_env(plugin_data):
    env = os.environ.copy()
    env["PLUGIN_DATA"] = str(plugin_data)
    env["PYTHONPATH"] = SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_transaction_locks_are_private_nonsymlink_and_nestable(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    with store.usage_lock():
        usage_db = tmp_path / "data" / "usage-lock.sqlite3"
        journal = tmp_path / "data" / "usage-lock.sqlite3-journal"
        assert usage_db.is_file()
        assert journal.is_file()
        with store.backups_lock():
            assert (tmp_path / "data" / "backups-lock.sqlite3").is_file()
        if os.name != "nt":
            assert stat.S_IMODE(usage_db.stat().st_mode) == 0o600
            assert stat.S_IMODE(journal.stat().st_mode) == 0o600

    bad_data = tmp_path / "bad-data"
    bad_data.mkdir()
    target = tmp_path / "outside-lock"
    target.touch()
    try:
        (bad_data / "usage-lock.sqlite3").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    monkeypatch.setenv("PLUGIN_DATA", str(bad_data))
    with pytest.raises(store.SkillStoreError, match="symlinked SQLite lock"):
        with store.usage_lock():
            pass


def test_transaction_lock_does_not_relabel_sqlite_error_from_caller(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    store = _store(tmp_path, monkeypatch, root)

    with pytest.raises(sqlite3.OperationalError, match="caller body failure"):
        with store.usage_lock():
            raise sqlite3.OperationalError("caller body failure")


def test_noncreating_lock_probe_does_not_create_parent(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    store = _store(tmp_path, monkeypatch, root)
    missing_root = tmp_path / "missing-store"

    with store.legacy_file_lock(missing_root / "usage.lock", create=False):
        pass
    with store.sqlite_transaction_lock(missing_root, "usage", create=False):
        pass

    assert not missing_root.exists()


def test_multiprocess_usage_counter_has_no_lost_updates(tmp_path):
    plugin_data = tmp_path / "data"
    env = _process_env(plugin_data)
    code = "import skill_store\nfor _ in range(40): skill_store.bump_review_counter('shared')"
    children = [
        subprocess.Popen([sys.executable, "-c", code], env=env)
        for _ in range(4)
    ]
    for child in children:
        assert child.wait(timeout=20) == 0

    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import skill_store; print(skill_store.get_review_counter('shared'))",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert check.stdout.strip() == "160"


def test_multiprocess_backup_ids_and_manifests_are_not_lost(tmp_path):
    plugin_data = tmp_path / "data"
    skill_root = tmp_path / "skills"
    skill = _skill(skill_root, "shared")
    env = _process_env(plugin_data)
    code = (
        "import pathlib, skill_store, sys; "
        "skill_store.backup_skill(pathlib.Path(sys.argv[1]), reason='parallel')"
    )
    children = [
        subprocess.Popen([sys.executable, "-c", code, str(skill)], env=env)
        for _ in range(8)
    ]
    for child in children:
        assert child.wait(timeout=20) == 0

    backups = sorted((plugin_data / "backups").iterdir())
    assert len(backups) == 8
    assert len({entry.name for entry in backups}) == 8
    for entry in backups:
        manifest = json.loads(
            (entry / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["backup_id"] == entry.name
        assert manifest["skill"] == "shared"
        assert manifest["reason"] == "parallel"


def test_transaction_lock_is_reacquired_after_holder_is_terminated(tmp_path):
    plugin_data = tmp_path / "data"
    marker = tmp_path / "held"
    acquired = tmp_path / "acquired"
    env = _process_env(plugin_data)
    holder_code = (
        "import pathlib, skill_store, sys, time\n"
        "with skill_store.usage_lock():\n"
        " pathlib.Path(sys.argv[1]).write_text('held', encoding='utf-8')\n"
        " time.sleep(120)\n"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_code, str(marker)], env=env)
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists(), "child did not acquire the transaction lock"
    holder.kill()
    holder.wait(timeout=10)

    recovery_code = (
        "import pathlib, skill_store, sys\n"
        "with skill_store.usage_lock():\n"
        " pathlib.Path(sys.argv[1]).write_text('ok', encoding='utf-8')\n"
    )
    recovered = subprocess.run(
        [sys.executable, "-c", recovery_code, str(acquired)],
        env=env,
        timeout=10,
        check=False,
    )
    assert recovered.returncode == 0
    assert acquired.read_text(encoding="utf-8") == "ok"


def test_restore_backup_targets_original_root_only(tmp_path, monkeypatch):
    """codex review R3: restoring a user-root backup must never overwrite a
    same-named skill in an earlier-searched (repo) root."""
    repo_root = tmp_path / "repo-skills"
    user_root = tmp_path / "user-skills"
    _skill(repo_root, "shared-name")
    d = _skill(user_root, "shared-name")
    (d / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: d\n---\nuser version\n",
        encoding="utf-8")
    store = _store(tmp_path, monkeypatch, repo_root, user_root)
    backup_id = store.backup_skill(d, reason="test")["backup_id"]
    assert (tmp_path / "data" / "backups.lock").is_file()

    def fail_if_restore_reenters(*_args, **_kwargs):
        raise AssertionError("restore must not re-enter backups_lock")

    monkeypatch.setattr(store, "backup_skill", fail_if_restore_reenters)
    (d / "SKILL.md").write_text(
        "---\nname: shared-name\ndescription: d\n---\nuser edited\n",
        encoding="utf-8")
    res = store.restore_backup(backup_id)
    assert res["path"] == str(d)  # manifest source wins, not root order
    assert "user version" in (d / "SKILL.md").read_text(encoding="utf-8")
    repo_text = (repo_root / "shared-name" / "SKILL.md").read_text(encoding="utf-8")
    assert "user version" not in repo_text  # repo skill untouched


def test_stamp_applied_even_when_body_mentions_plugin(tmp_path, monkeypatch):
    """codex review R3: a body/description mentioning the plugin name must
    not suppress the frontmatter provenance stamp."""
    root = tmp_path / "skills"
    store = _store(tmp_path, monkeypatch, root)
    content = ("---\nname: mentions\ndescription: about self-improving-skills\n---\n"
               "This body discusses self-improving-skills.\n")
    store.create_skill("mentions", content)
    text = (root / "mentions" / "SKILL.md").read_text(encoding="utf-8")
    import re
    fm = text[4:text.find("\n---", 4)]
    assert re.search(r"^\s*provenance: self-improving-skills\s*$", fm, re.M)
