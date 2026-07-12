"""Unit tests for the time-based skill lifecycle state machine."""

import datetime
import json


def _iso_days_ago(days):
    d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return d.isoformat()


def _seed(sandbox, name, idle_days, created_by="agent", **fields):
    sandbox.make_skill(name)
    us = sandbox.usage_store
    us.seed_if_missing(name, created_by)
    us.set_fields(name, created_at=_iso_days_ago(idle_days + 1),
                  last_used_at=_iso_days_ago(idle_days), **fields)


def test_stale_archive_and_reactivation(sandbox):
    _seed(sandbox, "fresh-skill", 5)
    _seed(sandbox, "stale-skill", 45)
    _seed(sandbox, "dead-skill", 120)
    summary = sandbox.curator.run()
    assert [x["name"] for x in summary["stale"]] == ["stale-skill"]
    assert [x["name"] for x in summary["archived"]] == ["dead-skill"]
    assert (sandbox.skills / ".archive" / "dead-skill").is_dir()
    assert not (sandbox.skills / "dead-skill").exists()
    # fresh activity lands while the record still says stale (e.g. written by
    # a path that doesn't auto-flip state) -> next run reactivates
    sandbox.usage_store.set_fields("stale-skill", last_used_at=_iso_days_ago(0))
    summary2 = sandbox.curator.run()
    assert "stale-skill" in summary2["reactivated"]


def test_user_and_pinned_skills_untouched(sandbox):
    _seed(sandbox, "user-skill", 200, created_by="user")
    _seed(sandbox, "pinned-skill", 200, pinned=True)
    summary = sandbox.curator.run()
    assert summary["archived"] == []
    assert "user-skill" in summary["skipped_user"]
    assert "pinned-skill" in summary["skipped_pinned"]
    assert (sandbox.skills / "user-skill").is_dir()
    assert (sandbox.skills / "pinned-skill").is_dir()


def test_use_count_extends_archive_threshold(sandbox):
    _seed(sandbox, "proven-skill", 120, use_count=3)
    _seed(sandbox, "unproven-skill", 120, use_count=2)
    summary = sandbox.curator.run()
    archived = [x["name"] for x in summary["archived"]]
    assert "unproven-skill" in archived
    assert "proven-skill" not in archived  # 90d * 2 for use_count >= 3
    _seed(sandbox, "proven-but-ancient", 200, use_count=3)
    summary2 = sandbox.curator.run()
    assert "proven-but-ancient" in [x["name"] for x in summary2["archived"]]


def test_dry_run_mutates_nothing(sandbox):
    _seed(sandbox, "dead-skill", 120)
    summary = sandbox.curator.run(dry_run=True)
    assert [x["name"] for x in summary["archived"]] == ["dead-skill"]
    assert (sandbox.skills / "dead-skill").is_dir()


def test_restore_roundtrip(sandbox):
    _seed(sandbox, "dead-skill", 120)
    sandbox.curator.run()
    assert not (sandbox.skills / "dead-skill").exists()
    assert sandbox.curator.restore("dead-skill") is True
    assert (sandbox.skills / "dead-skill").is_dir()
    assert sandbox.usage_store.all_records()["dead-skill"]["state"] == "active"


def test_archive_one_records_absorbed_into(sandbox):
    _seed(sandbox, "narrow-skill", 1)
    sandbox.make_skill("umbrella-skill")  # C2: the umbrella must exist on disk
    res = sandbox.curator.archive_one("narrow-skill", absorbed_into="umbrella-skill")
    assert res["ok"]
    rec = sandbox.usage_store.all_records()["narrow-skill"]
    assert rec["state"] == "archived"
    assert rec["absorbed_into"] == "umbrella-skill"


def test_archive_one_rejects_missing_or_self_umbrella(sandbox):
    _seed(sandbox, "narrow-skill", 1)
    res = sandbox.curator.archive_one("narrow-skill", absorbed_into="ghost-umbrella")
    assert res["ok"] is False and "umbrella" in res["reason"]
    res2 = sandbox.curator.archive_one("narrow-skill", absorbed_into="narrow-skill")
    assert res2["ok"] is False
    assert (sandbox.skills / "narrow-skill").is_dir()  # nothing moved
    # the hallucinated "absorbed" must NOT be recorded either
    assert sandbox.usage_store.all_records()["narrow-skill"].get("absorbed_into") is None


def test_archive_one_refuses_protected_unless_force(sandbox):
    _seed(sandbox, "pinned-skill", 1, pinned=True)
    _seed(sandbox, "team-skill", 1, created_by="team")
    assert sandbox.curator.archive_one("pinned-skill")["ok"] is False
    assert sandbox.curator.archive_one("team-skill")["ok"] is False
    assert (sandbox.skills / "pinned-skill").is_dir()
    assert (sandbox.skills / "team-skill").is_dir()
    res = sandbox.curator.archive_one("team-skill", force=True)  # human override
    assert res["ok"]
    assert not (sandbox.skills / "team-skill").exists()


def test_archive_one_fails_closed_on_unknown_ownership(sandbox):
    """No telemetry record + no distilled marker = ownership unknown → refuse
    (codex review R1: absence of evidence must not default to agent)."""
    sandbox.make_skill("hand-authored")  # never seeded into usage store
    res = sandbox.curator.archive_one("hand-authored")
    assert res["ok"] is False and "created_by=user" in res["reason"]
    assert (sandbox.skills / "hand-authored").is_dir()
    # but a record-less skill CARRYING the distiller-exclusive marker stays
    # eligible (the plain provenance stamp does NOT count — the PostToolUse
    # hook applies it to user-authored writes too)
    sandbox.make_skill("distilled-orphan",
                       "---\nname: distilled-orphan\ndescription: d\nmetadata:\n"
                       "  provenance: self-improving-skills\n  origin: distilled\n---\nbody\n")
    assert sandbox.curator.archive_one("distilled-orphan")["ok"] is True
    sandbox.make_skill("stamped-user",
                       "---\nname: stamped-user\ndescription: d\nmetadata:\n"
                       "  provenance: self-improving-skills\n---\nbody\n")
    assert sandbox.curator.archive_one("stamped-user")["ok"] is False


def test_restore_timestamp_suffixed_archive(sandbox):
    arch = sandbox.skills / ".archive"
    (arch / "dup-skill.20260101T000000Z").mkdir(parents=True)
    newest = arch / "dup-skill.20260301T000000Z"
    newest.mkdir()
    (newest / "SKILL.md").write_text("---\nname: dup-skill\ndescription: d\n---\nnew\n",
                                     encoding="utf-8")
    (arch / "dup-skill-helpers").mkdir()  # unrelated sibling — must not be swallowed
    assert sandbox.curator.restore("dup-skill") is True
    restored = sandbox.skills / "dup-skill" / "SKILL.md"
    assert restored.read_text(encoding="utf-8").endswith("new\n")  # newest wins
    assert (arch / "dup-skill.20260101T000000Z").is_dir()  # older left in place
    assert (arch / "dup-skill-helpers").is_dir()  # untouched


def test_restore_normalizes_suffixed_input(sandbox):
    arch = sandbox.skills / ".archive"
    (arch / "solo-skill.20260101T000000Z").mkdir(parents=True)
    assert sandbox.curator.restore("solo-skill.20260101T000000Z") is True
    assert (sandbox.skills / "solo-skill").is_dir()  # bare name, suffix stripped
    records = sandbox.usage_store.all_records()
    assert "solo-skill" in records  # usage keyed bare — never the suffixed name
    assert "solo-skill.20260101T000000Z" not in records


def test_restore_ignores_non_timestamp_suffix(sandbox):
    (sandbox.skills / ".archive" / "other-skill.notatimestamp").mkdir(parents=True)
    assert sandbox.curator.restore("other-skill") is False


def test_mark_curated_preserves_state_fields(sandbox):
    state_path = sandbox.home / ".claude" / "self-improve" / "curator_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(
        {"last_run": 1.0, "run_count": 3, "last_summary": {"archived": 1}}),
        encoding="utf-8")
    assert sandbox.curator.mark_curated() is True
    d = json.loads(state_path.read_text(encoding="utf-8"))
    assert d["run_count"] == 4
    assert d["last_run"] > 1.0
    assert d["last_summary"] == {"archived": 1}  # preserved, not overwritten


def test_snapshot_rollback_roundtrip(sandbox):
    import curator_backup
    _seed(sandbox, "keeper-skill", 5)
    assert curator_backup.make_snapshot()
    sandbox.curator.archive_one("keeper-skill", force=True)
    assert not (sandbox.skills / "keeper-skill").exists()
    assert sandbox.usage_store.all_records()["keeper-skill"]["state"] == "archived"
    res = curator_backup.rollback()
    assert res["ok"], res
    assert (sandbox.skills / "keeper-skill").is_dir()
    # usage meta came back with the tree — record no longer stuck at archived
    assert sandbox.usage_store.all_records()["keeper-skill"]["state"] == "active"
    assert res["usage_meta_restored"] is True
    # the rollback itself is undoable
    assert res["undo_snapshot"] in curator_backup.list_snapshots()


def test_pass_start_snapshot_survives_keep_prune(sandbox):
    """codex review R2: KEEP=5 pruning must never delete the snapshot that
    predates a whole curation pass (mark_curated records + protects it)."""
    import curator_backup
    _seed(sandbox, "any-skill", 1)
    assert sandbox.curator.mark_curated() is True
    import json
    state = json.loads((sandbox.home / ".claude" / "self-improve" /
                        "curator_state.json").read_text(encoding="utf-8"))
    pass_snap = state["last_pass_snapshot"]
    for _ in range(curator_backup.KEEP + 3):  # push well past the keep limit
        curator_backup.make_snapshot()
    import os
    assert os.path.isfile(pass_snap)  # protected from the prune


def test_snapshot_excludes_symlinks_and_stays_restorable(sandbox):
    """codex review R2: a symlink in a skill package must not make every
    snapshot permanently unrestorable (rollback refuses link members)."""
    import curator_backup
    import os
    d = sandbox.make_skill("linky-skill")
    os.symlink("/etc/hosts", str(d / "references-link"))
    assert curator_backup.make_snapshot()
    sandbox.curator.archive_one("linky-skill", force=True)
    res = curator_backup.rollback()
    assert res["ok"], res
    assert (sandbox.skills / "linky-skill" / "SKILL.md").is_file()
    assert not (sandbox.skills / "linky-skill" / "references-link").exists()


def test_provenance_mention_in_body_stays_protected(sandbox):
    """codex review R2: marker text in the BODY must not defeat the unknown-
    ownership fail-closed guard — frontmatter only."""
    sandbox.make_skill("docs-about-plugin",
                       "---\nname: docs-about-plugin\ndescription: d\n---\n"
                       "This documents provenance: self-improving-skills and "
                       "origin: distilled markers.\n")
    res = sandbox.curator.archive_one("docs-about-plugin")
    assert res["ok"] is False and "created_by=user" in res["reason"]


def test_prune_preview_lists_use_count(sandbox):
    _seed(sandbox, "dusty-skill", 50, use_count=1)
    res = sandbox.curator.prune_idle(30, dry_run=True)
    cand = {c["name"]: c for c in res["candidates"]}
    assert "dusty-skill" in cand and cand["dusty-skill"]["use_count"] == 1
    assert (sandbox.skills / "dusty-skill").is_dir()  # preview only
