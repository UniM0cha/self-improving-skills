"""Tests for the deterministic directory hash and manifest robustness."""

import os
import time


def _skill(tmp_path, name="some-skill", body="content"):
    d = tmp_path / name
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: {0}\ndescription: d\n---\n{1}\n".format(name, body),
                                encoding="utf-8")
    (d / "references" / "notes.md").write_text("notes", encoding="utf-8")
    return d


def test_hash_ignores_mtime_and_noise(sandbox, tmp_path):
    tm = sandbox.team_manifest
    d = _skill(tmp_path)
    h1 = tm.dir_hash(str(d))
    assert h1
    # mtime change must not affect the hash
    later = time.time() + 1000
    os.utime(str(d / "SKILL.md"), (later, later))
    # OS/noise files must not affect the hash
    (d / ".DS_Store").write_bytes(b"\x00noise")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert tm.dir_hash(str(d)) == h1


def test_hash_changes_on_content_and_structure(sandbox, tmp_path):
    tm = sandbox.team_manifest
    d = _skill(tmp_path)
    h1 = tm.dir_hash(str(d))
    (d / "SKILL.md").write_text("changed", encoding="utf-8")
    h2 = tm.dir_hash(str(d))
    assert h2 != h1
    # restoring identical content restores the hash
    d2 = _skill(tmp_path / "copy")
    assert tm.dir_hash(str(d2)) == h1
    # a rename (same bytes, different relpath) changes the hash
    os.rename(str(d2 / "references" / "notes.md"), str(d2 / "references" / "renamed.md"))
    assert tm.dir_hash(str(d2)) != h1


def test_hash_missing_or_empty_dir_is_none(sandbox, tmp_path):
    tm = sandbox.team_manifest
    assert tm.dir_hash(str(tmp_path / "nope")) is None
    empty = tmp_path / "empty-skill"
    empty.mkdir()
    assert tm.dir_hash(str(empty)) is None


def test_corrupt_manifest_backed_up_and_reset(sandbox):
    tm = sandbox.team_manifest
    os.makedirs(os.path.dirname(tm.MANIFEST_PATH), exist_ok=True)
    with open(tm.MANIFEST_PATH, "w", encoding="utf-8") as fh:
        fh.write("{ not json !!!")
    data = tm.load()
    assert data["skills"] == {}  # degraded to empty (safe direction)
    backups = [f for f in os.listdir(os.path.dirname(tm.MANIFEST_PATH))
               if f.startswith("team_sync.json.corrupt-")]
    assert backups


def test_mutate_roundtrip(sandbox):
    tm = sandbox.team_manifest

    def f(m):
        m["skills"]["x"] = {"origin_hash": "abc"}
    tm.mutate(f)
    assert tm.load()["skills"]["x"]["origin_hash"] == "abc"
