"""Resolve an archived Codex transcript without changing its queue coordinates."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Dict, List, Optional, Tuple


MAX_METADATA_BYTES = 1_048_576
MAX_METADATA_LINES = 256


class TranscriptResolutionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unsafe(message: str) -> TranscriptResolutionError:
    return TranscriptResolutionError("unsafe_transcript", message)


def _is_link(info: os.stat_result) -> bool:
    # Windows junctions and other reparse points can redirect a parent path too.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _inspect_path(path: Path) -> List[os.stat_result]:
    """Inspect every component without following links, including dangling ones."""
    result = []
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        if _is_link(info):
            raise _unsafe("transcript path must not contain symbolic links or reparse points")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise _unsafe("transcript parent path is not a directory")
        result.append(info)
    return result


def _expanded_path(value: str, values: Dict[str, str]) -> Path:
    # Honor the supplied environment in tests and detached worker environments.
    home = values.get("HOME") or values.get("USERPROFILE")
    if home and (value == "~" or value.startswith(("~/", "~\\"))):
        return Path(home).joinpath(value[2:] if len(value) > 1 else "").absolute()
    return Path(value).expanduser().absolute()


def _codex_home(values: Dict[str, str]) -> Path:
    configured = str(values.get("CODEX_HOME") or "").strip()
    if configured:
        return _expanded_path(configured, values)
    home = values.get("HOME") or values.get("USERPROFILE")
    return _expanded_path(str(home), values) / ".codex" if home else Path.home() / ".codex"


def _open_archive(path: Path) -> int:
    """Open the candidate safely and return an owned, regular-file descriptor."""
    before = _inspect_path(path)
    if not stat.S_ISREG(before[-1].st_mode):
        raise _unsafe("archived transcript is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = -1
    try:
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            # Pin each directory while walking. A concurrent rename cannot turn
            # a checked parent into a symlink followed by this open operation.
            directory_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_CLOEXEC", 0)
            parent_fd = os.open(path.anchor, directory_flags)
            try:
                for part in path.parts[1:-1]:
                    next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                    os.close(parent_fd)
                    parent_fd = next_fd
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
        else:
            fd = os.open(path, flags)
        info = os.fstat(fd)
        after = _inspect_path(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_link(info)
            or not os.path.samestat(info, before[-1])
            or not os.path.samestat(info, after[-1])
            or any(not os.path.samestat(old, new) for old, new in zip(before, after))
        ):
            raise _unsafe("archived transcript path changed while opening")
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _check_session(fd: int, session_id: str) -> None:
    """Read only a bounded metadata prefix from the descriptor we validated."""
    if not session_id:
        raise _unsafe("archived transcript requires a session identity")
    with os.fdopen(os.dup(fd), "rb") as handle:
        remaining = MAX_METADATA_BYTES
        for _ in range(MAX_METADATA_LINES):
            raw = handle.readline(remaining + 1)
            if not raw:
                break
            if len(raw) > remaining:
                break
            remaining -= len(raw)
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict) or event.get("type") != "session_meta":
                continue
            payload = event.get("payload")
            actual = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(actual, str) or not actual:
                raise _unsafe("archived transcript session metadata is invalid")
            if actual != session_id:
                raise _unsafe("archived transcript session identity does not match the queued job")
            return
    raise _unsafe("archived transcript session metadata is missing from its bounded prefix")


def resolve_transcript_path(
    original_path: str,
    session_id: str,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[Path, bool]:
    """Return ``(path, relocated)``; only a missing original permits fallback.

    An archived candidate must have the exact original basename and matching
    session metadata. This performs no recursive search and never writes queue
    state. The caller still applies its captured row cutoff and read checks.
    """
    if not original_path:
        raise TranscriptResolutionError("source_missing", "transcript path is missing")
    values = os.environ if env is None else env
    try:
        original = _expanded_path(str(original_path), values)
        try:
            _inspect_path(original)
        except FileNotFoundError:
            pass
        else:
            # Preserve the existing reader's validation for original sources.
            return original, False
        archived = _codex_home(values) / "archived_sessions" / original.name
        fd = _open_archive(archived)
        try:
            _check_session(fd, session_id)
        finally:
            os.close(fd)
        return archived, True
    except TranscriptResolutionError:
        raise
    except FileNotFoundError as exc:
        raise TranscriptResolutionError(
            "source_missing", "transcript is absent from both its original and archive locations"
        ) from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise _unsafe(f"transcript path cannot be validated: {exc.__class__.__name__}") from exc
