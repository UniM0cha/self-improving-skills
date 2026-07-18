"""Unit tests for usage_store.py (sandboxed HOME via reload)."""

import datetime


def _utc_iso(**delta):
    d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)
    return d.isoformat()


def test_apply_events_seeds_bumps_and_reactivates(sandbox):
    us = sandbox.usage_store
    us.apply_events([("a", "use", "user"), ("a", "view", "user")])
    rec = us.all_records()["a"]
    assert rec["use_count"] == 1 and rec["view_count"] == 1
    assert rec["created_by"] == "user"
    us.set_fields("a", state="stale")
    us.apply_events([("a", "use", "user")])
    assert us.all_records()["a"]["state"] == "active"  # stale skill used again


def test_created_by_fixed_at_seed_time(sandbox):
    us = sandbox.usage_store
    us.apply_events([("a", "use", "user")])
    us.apply_events([("a", "patch", "agent")])  # later events can't relabel
    assert us.all_records()["a"]["created_by"] == "user"


def test_offsets_legacy_int_and_migration(sandbox):
    us = sandbox.usage_store
    data = us.load()
    data.setdefault("_meta", {}).setdefault("offsets", {})["legacy"] = 7
    us._save(data)
    assert us.get_offset("legacy") == 7
    us.apply_events([], "fresh", 9)
    v = us.load()["_meta"]["offsets"]["fresh"]
    assert v["o"] == 9 and "t" in v
    assert us.get_offset("fresh") == 9


def test_meta_prune_cap_and_bad_timestamps(sandbox):
    us = sandbox.usage_store
    data = us.load()
    off = data.setdefault("_meta", {}).setdefault("offsets", {})
    for i in range(us.META_MAX_ENTRIES + 5):
        off["s{0}".format(i)] = {"o": 1, "t": us.now_iso()}
    off["bad-ts"] = {"o": 1, "t": "not-a-date"}
    off["too-old"] = {"o": 1, "t": _utc_iso(days=40)}
    us._save(data)
    us.apply_events([], "current", 3)
    m = us.load()["_meta"]["offsets"]
    # prune runs BEFORE inserting the current entry (so it always survives),
    # hence the cap can be exceeded by exactly one.
    assert len(m) <= us.META_MAX_ENTRIES + 1
    assert "current" in m
    assert "bad-ts" not in m and "too-old" not in m


def test_nudge_roundtrip(sandbox):
    us = sandbox.usage_store
    assert us.get_nudge_row("s") == 0
    us.record_nudge("s", 42)
    assert us.get_nudge_row("s") == 42


def test_forget_missing_grace(sandbox):
    us = sandbox.usage_store
    us.apply_events([("ghost", "use", "user")])
    us.forget_missing(set())
    assert "missing_since" in us.load()["ghost"]  # marked, not dropped
    us.forget_missing(set())
    assert "ghost" in us.load()  # still inside grace
    us.set_fields("ghost", missing_since=_utc_iso(hours=25))
    us.forget_missing(set())
    assert "ghost" not in us.load()  # grace expired -> dropped


def test_forget_missing_reappearance_clears_marker(sandbox):
    us = sandbox.usage_store
    us.apply_events([("blinky", "use", "user")])
    us.forget_missing(set())
    assert "missing_since" in us.load()["blinky"]
    us.forget_missing({"blinky"})
    assert "missing_since" not in us.load()["blinky"]


def test_forget_missing_keeps_archived(sandbox):
    us = sandbox.usage_store
    us.apply_events([("old", "use", "user")])
    us.set_fields("old", state="archived", missing_since=_utc_iso(hours=48))
    us.forget_missing(set())
    assert "old" in us.load()  # archived records survive for /restore-skill


def test_seed_if_missing_never_overwrites(sandbox):
    us = sandbox.usage_store
    us.seed_if_missing("a", "user")
    us.seed_if_missing("a", "agent")
    assert us.all_records()["a"]["created_by"] == "user"
