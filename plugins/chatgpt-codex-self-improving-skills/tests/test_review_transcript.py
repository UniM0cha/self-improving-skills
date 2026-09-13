import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import review_transcript as transcripts


def _metadata(session_id="session-a"):
    return json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n"


def _paths(tmp_path):
    original = tmp_path / "sessions" / "2026" / "rollout-session-a.jsonl"
    archived = tmp_path / "codex-home" / "archived_sessions" / original.name
    archived.parent.mkdir(parents=True)
    return original, archived, {"CODEX_HOME": str(archived.parent.parent)}


def _link(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symbolic links are unavailable")


def test_existing_original_wins_without_archive_identity_check(tmp_path):
    original, archived, env = _paths(tmp_path)
    original.parent.mkdir(parents=True)
    original.write_text("original reader validates this\n", encoding="utf-8")
    archived.write_text(_metadata("other-session"), encoding="utf-8")

    assert transcripts.resolve_transcript_path(str(original), "session-a", env) == (original, False)


def test_missing_original_resolves_same_filename_with_matching_session(tmp_path):
    original, archived, env = _paths(tmp_path)
    archived.write_text("bad json\n\n[]\n" + _metadata(), encoding="utf-8")

    assert transcripts.resolve_transcript_path(str(original), "session-a", env) == (archived, True)
    assert not original.exists()


@pytest.mark.parametrize("variable", ["HOME", "USERPROFILE"])
def test_codex_home_falls_back_to_supplied_home(tmp_path, variable):
    original = tmp_path / "sessions" / "rollout.jsonl"
    archived = tmp_path / "home" / ".codex" / "archived_sessions" / original.name
    archived.parent.mkdir(parents=True)
    archived.write_text(_metadata(), encoding="utf-8")

    assert transcripts.resolve_transcript_path(
        str(original), "session-a", {variable: str(tmp_path / "home")}
    ) == (archived, True)


def test_codex_home_tilde_uses_supplied_environment(tmp_path):
    original = tmp_path / "sessions" / "rollout.jsonl"
    archived = tmp_path / "home" / "custom" / "archived_sessions" / original.name
    archived.parent.mkdir(parents=True)
    archived.write_text(_metadata(), encoding="utf-8")

    assert transcripts.resolve_transcript_path(
        str(original), "session-a", {"HOME": str(tmp_path / "home"), "CODEX_HOME": "~/custom"}
    ) == (archived, True)


@pytest.mark.parametrize(
    "content",
    [
        _metadata("another-session"),
        _metadata("another-session") + _metadata(),
        json.dumps({"type": "session_meta", "payload": {}}) + "\n" + _metadata(),
        json.dumps({"type": "event_msg", "payload": {"id": "session-a"}}),
        "",
    ],
)
def test_archive_rejects_wrong_or_missing_metadata(tmp_path, content):
    original, archived, env = _paths(tmp_path)
    archived.write_text(content, encoding="utf-8")

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_original_and_archive_absent_report_source_missing_without_tree_search(tmp_path):
    original, archived, env = _paths(tmp_path)
    nested = archived.parent / "nested" / archived.name
    nested.parent.mkdir()
    nested.write_text(_metadata(), encoding="utf-8")

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "source_missing"


@pytest.mark.parametrize("target_exists", [False, True])
def test_original_symlink_never_falls_back_even_when_dangling(tmp_path, target_exists):
    original, archived, env = _paths(tmp_path)
    archived.write_text(_metadata(), encoding="utf-8")
    original.parent.mkdir(parents=True)
    target = tmp_path / "target.jsonl"
    if target_exists:
        target.write_text(_metadata(), encoding="utf-8")
    _link(original, target)

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_missing_original_below_symlink_does_not_enable_fallback(tmp_path):
    original, archived, env = _paths(tmp_path)
    archived.write_text(_metadata(), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(tmp_path / "sessions", outside, directory=True)

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


@pytest.mark.parametrize("location", ["file", "archive_directory", "codex_home"])
def test_archive_rejects_leaf_and_parent_symlinks(tmp_path, location):
    original, archived, env = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    if location == "file":
        target = outside / archived.name
        target.write_text(_metadata(), encoding="utf-8")
        _link(archived, target)
    elif location == "archive_directory":
        (outside / archived.name).write_text(_metadata(), encoding="utf-8")
        archived.parent.rmdir()
        _link(archived.parent, outside, directory=True)
    else:
        (outside / "archived_sessions").mkdir()
        (outside / "archived_sessions" / archived.name).write_text(_metadata(), encoding="utf-8")
        archived.parent.rmdir()
        archived.parent.parent.rmdir()
        _link(archived.parent.parent, outside, directory=True)

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_archive_must_be_regular_file(tmp_path):
    original, archived, env = _paths(tmp_path)
    archived.mkdir()

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_permission_failure_on_original_does_not_enable_fallback(tmp_path, monkeypatch):
    original, archived, env = _paths(tmp_path)
    archived.write_text(_metadata(), encoding="utf-8")
    inspect = transcripts._inspect_path

    def deny_original(path):
        if path == original:
            raise PermissionError("fixture permission failure")
        return inspect(path)

    monkeypatch.setattr(transcripts, "_inspect_path", deny_original)
    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


@pytest.mark.parametrize("bound", ["bytes", "lines"])
def test_archive_metadata_scan_is_bounded(tmp_path, monkeypatch, bound):
    original, archived, env = _paths(tmp_path)
    if bound == "bytes":
        monkeypatch.setattr(transcripts, "MAX_METADATA_BYTES", 128)
        prefix = "x" * 256 + "\n"
    else:
        monkeypatch.setattr(transcripts, "MAX_METADATA_LINES", 2)
        prefix = "{}\n" * 3
    archived.write_text(prefix + _metadata(), encoding="utf-8")

    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_archive_metadata_never_reads_later_oversized_dialogue(tmp_path):
    original, archived, env = _paths(tmp_path)
    archived.write_text(_metadata() + "x" * (transcripts.MAX_METADATA_BYTES + 1), encoding="utf-8")

    assert transcripts.resolve_transcript_path(str(original), "session-a", env) == (archived, True)


def test_archive_open_detects_replacement_before_reading_metadata(tmp_path, monkeypatch):
    original, archived, env = _paths(tmp_path)
    archived.write_text(_metadata(), encoding="utf-8")
    real_open = os.open

    def replaced_open(path, flags, *args, **kwargs):
        if path == archived or path == archived.name:
            replacement = archived.with_suffix(".replacement")
            replacement.write_text(_metadata(), encoding="utf-8")
            replacement.replace(archived)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transcripts.os, "open", replaced_open)
    with pytest.raises(transcripts.TranscriptResolutionError) as error:
        transcripts.resolve_transcript_path(str(original), "session-a", env)

    assert error.value.code == "unsafe_transcript"


def test_resolved_archive_still_honors_worker_captured_row_cutoff(tmp_path):
    import background_review_worker as worker

    original, archived, env = _paths(tmp_path)
    captured = json.dumps({"type": "event_msg", "payload": {"message": "captured"}})
    later = json.dumps({"type": "event_msg", "payload": {"message": "SECRET_AFTER_CUTOFF"}})
    archived.write_text(_metadata() + captured + "\n" + later + "\n", encoding="utf-8")

    resolved, relocated = transcripts.resolve_transcript_path(str(original), "session-a", env)
    evidence = worker._read_transcript(str(resolved), 2)

    assert relocated
    assert "captured" in evidence
    assert "SECRET_AFTER_CUTOFF" not in evidence
