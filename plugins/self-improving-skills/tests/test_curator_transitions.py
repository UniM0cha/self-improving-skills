"""Unit tests for the time-based skill lifecycle state machine."""

import datetime


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
    res = sandbox.curator.archive_one("narrow-skill", absorbed_into="umbrella-skill")
    assert res["ok"]
    rec = sandbox.usage_store.all_records()["narrow-skill"]
    assert rec["state"] == "archived"
    assert rec["absorbed_into"] == "umbrella-skill"


def test_prune_preview_lists_use_count(sandbox):
    _seed(sandbox, "dusty-skill", 50, use_count=1)
    res = sandbox.curator.prune_idle(30, dry_run=True)
    cand = {c["name"]: c for c in res["candidates"]}
    assert "dusty-skill" in cand and cand["dusty-skill"]["use_count"] == 1
    assert (sandbox.skills / "dusty-skill").is_dir()  # preview only
