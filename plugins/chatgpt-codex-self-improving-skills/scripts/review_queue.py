#!/usr/bin/env python3
"""Durable SQLite queue for non-blocking Codex skill reviews.

The queue deliberately stores transcript *coordinates*, never transcript
contents.  A worker opens the source transcript only while a job is running
and reads no rows beyond ``transcript_rows``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


JOB_STATUSES = ("pending", "running", "done", "failed", "blocked")
RESULT_STATUSES = ("changed", "nothing_to_save", "candidate", "failed")
DEFAULT_JOB_LEASE_SECONDS = 90
DEFAULT_WORKER_LEASE_SECONDS = 90
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS: Sequence[int] = (30, 300)
RETENTION_DAYS = 30
SQLITE_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")


def _assert_safe_sqlite_paths(path: Path) -> None:
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(f"{path}{suffix}")
        if candidate.is_symlink():
            raise ValueError(f"review queue path must not be a symbolic link: {candidate}")
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f"review queue path must be a regular file: {candidate}")


def _secure_sqlite_paths(path: Path) -> None:
    _assert_safe_sqlite_paths(path)
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        try:
            os.chmod(f"{path}{suffix}", 0o600)
        except OSError:
            pass


class _QueueConnection(sqlite3.Connection):
    """Close every context-managed connection and secure SQLite sidecars."""

    queue_path: Optional[Path] = None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()
            if self.queue_path is not None:
                _secure_sqlite_paths(self.queue_path)


def _fallback_user_home() -> Path:
    for name in ("HOME", "USERPROFILE"):
        configured = os.environ.get(name)
        if not configured:
            continue
        candidate = Path(configured)
        if candidate.is_absolute():
            return candidate.resolve()
    try:
        candidate = Path.home()
        if candidate.is_absolute():
            return candidate.resolve()
    except (OSError, RuntimeError):
        pass
    if os.name != "nt":
        try:
            import pwd

            candidate = Path(pwd.getpwuid(os.getuid()).pw_dir)
            if candidate.is_absolute():
                return candidate.resolve()
        except (ImportError, KeyError, OSError, RuntimeError):
            pass
    else:
        drive = os.environ.get("HOMEDRIVE") or ""
        tail = os.environ.get("HOMEPATH") or ""
        candidate = Path(f"{drive}{tail}")
        if candidate.is_absolute():
            return candidate.resolve()
    raise RuntimeError("No absolute user home directory is available")


def default_queue_path() -> Path:
    """Resolve the shared plugin-data queue without duplicating data rules."""
    try:
        import skill_store

        root = skill_store.data_dir()
    except Exception:
        configured = os.environ.get("PLUGIN_DATA")
        if configured:
            root = Path(configured).expanduser()
        else:
            root = _fallback_user_home() / ".self-improving-skills"
    return root / "review-jobs.sqlite3"


def _now() -> float:
    return time.time()


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or int(pid) <= 0:
        return False
    if os.name == "nt":
        # CPython's os.kill(pid, 0) is destructive on Windows: it reaches
        # TerminateProcess rather than behaving like the POSIX existence
        # probe. Query process state through a read-only WinAPI handle.
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, int(pid)
            )
            if not handle:
                # Access denied proves a process exists; other errors normally
                # mean the PID is gone or invalid.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            # Fail conservatively: never risk duplicate work because a
            # read-only liveness probe was unavailable.
            return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_identity(pid: Optional[int]) -> Optional[str]:
    """Return a process creation identity so PID reuse is not mistaken for liveness."""
    if not pid or int(pid) <= 0:
        return None
    numeric_pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _FileTime(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
                ctypes.POINTER(_FileTime),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information, False, numeric_pid
            )
            if not handle:
                return None
            try:
                created, exited, kernel, user = (
                    _FileTime(),
                    _FileTime(),
                    _FileTime(),
                    _FileTime(),
                )
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                return f"windows:{created.high:08x}{created.low:08x}"
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    proc_stat = Path(f"/proc/{numeric_pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="ascii")
        # Fields after the final ')' start at field 3; index 19 is field 22,
        # the kernel start time in clock ticks since boot.
        fields = raw.rsplit(")", 1)[1].split()
        return f"proc:{fields[19]}" if len(fields) > 19 else None
    except (OSError, IndexError, UnicodeError):
        pass

    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(numeric_pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = result.stdout.strip()
    return f"ps:{started}" if result.returncode == 0 and started else None


def _pid_matches_identity(pid: Optional[int], expected: Optional[str]) -> bool:
    if not _pid_alive(pid):
        return False
    if not expected:
        # Existing rows created before identity tracking remain conservative.
        return True
    actual = _pid_identity(pid)
    return True if actual is None else actual == expected


def _as_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    value = dict(row)
    raw_result = value.pop("result_json", None)
    if raw_result:
        try:
            value["result"] = json.loads(raw_result)
        except (TypeError, json.JSONDecodeError):
            value["result"] = None
    else:
        value["result"] = None
    value["signal"] = bool(value.get("signal"))
    value["model_fallback_used"] = bool(value.get("model_fallback_used"))
    return value


def validate_result(value: Any) -> Dict[str, Any]:
    """Validate and normalize the worker's intentionally small result shape."""
    if not isinstance(value, dict):
        raise ValueError("review result must be a JSON object")
    status = value.get("status")
    if status not in RESULT_STATUSES:
        raise ValueError("review result has an invalid status")
    skills = value.get("skills")
    candidates = value.get("candidates")
    summary = value.get("summary")
    if not isinstance(skills, list) or not isinstance(candidates, list) or not isinstance(summary, str):
        raise ValueError("review result is missing skills, candidates, or summary")

    normalized_skills: List[Dict[str, Any]] = []
    for item in skills:
        if not isinstance(item, dict):
            raise ValueError("review result contains an invalid skill entry")
        name, action = item.get("name"), item.get("action")
        backup_id = item.get("backup_id")
        if not isinstance(name, str) or not name or not isinstance(action, str) or not action:
            raise ValueError("review result skill entries require name and action")
        if backup_id is not None and not isinstance(backup_id, str):
            raise ValueError("review result backup_id must be a string or null")
        normalized_skills.append({"name": name, "action": action, "backup_id": backup_id})

    normalized_candidates: List[Dict[str, str]] = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("review result contains an invalid candidate entry")
        name, reason, proposed = item.get("name"), item.get("reason"), item.get("proposed_change")
        if not all(isinstance(part, str) and part for part in (name, reason, proposed)):
            raise ValueError("review candidates require name, reason, and proposed_change")
        normalized_candidates.append(
            {"name": name, "reason": reason, "proposed_change": proposed}
        )
    return {
        "status": status,
        "skills": normalized_skills,
        "candidates": normalized_candidates,
        "summary": summary,
    }


class ReviewQueue:
    """Concurrency-safe queue with a singleton worker lease."""

    def __init__(self, path: Optional[os.PathLike[str] | str] = None) -> None:
        self.path = Path(path) if path is not None else default_queue_path()
        self.path = self.path.expanduser().absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_sqlite_paths(self.path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        _assert_safe_sqlite_paths(self.path)
        conn = sqlite3.connect(
            str(self.path),
            timeout=10,
            isolation_level=None,
            factory=_QueueConnection,
        )
        conn.queue_path = self.path
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        _secure_sqlite_paths(self.path)
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    transcript_path TEXT NOT NULL,
                    transcript_rows INTEGER NOT NULL CHECK (transcript_rows >= 0),
                    signal INTEGER NOT NULL DEFAULT 0,
                    signal_source TEXT NOT NULL DEFAULT '',
                    trigger TEXT NOT NULL,
                    model TEXT,
                    model_fallback_used INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','done','failed','blocked')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    heartbeat_at REAL,
                    worker_pid INTEGER,
                    worker_pid_identity TEXT,
                    result_path TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    last_error TEXT,
                    retry_delay_seconds INTEGER,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS review_jobs_ready
                    ON review_jobs(status, available_at, id);
                CREATE INDEX IF NOT EXISTS review_jobs_session_pending
                    ON review_jobs(session_id, status, id);

                CREATE TABLE IF NOT EXISTS review_job_turns (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    job_id INTEGER NOT NULL REFERENCES review_jobs(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (session_id, turn_id)
                );

                CREATE TABLE IF NOT EXISTS review_worker_lease (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    owner TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    pid_identity TEXT,
                    acquired_at REAL NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_jobs)").fetchall()
            }
            if "model_fallback_used" not in columns:
                conn.execute(
                    "ALTER TABLE review_jobs ADD COLUMN "
                    "model_fallback_used INTEGER NOT NULL DEFAULT 0"
                )
            if "worker_pid_identity" not in columns:
                conn.execute(
                    "ALTER TABLE review_jobs ADD COLUMN worker_pid_identity TEXT"
                )
            worker_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(review_worker_lease)").fetchall()
            }
            if "pid_identity" not in worker_columns:
                conn.execute(
                    "ALTER TABLE review_worker_lease ADD COLUMN pid_identity TEXT"
                )
        _secure_sqlite_paths(self.path)

    def enqueue(
        self,
        *,
        session_id: str,
        turn_id: str,
        transcript_path: str,
        transcript_rows: int,
        signal: bool,
        signal_source: str,
        trigger: str,
        model: Optional[str],
    ) -> Dict[str, Any]:
        session_id = str(session_id or "global")
        turn_id = str(turn_id or "")
        if not turn_id:
            # A stable key is still required for exact-turn deduplication.
            turn_id = f"anonymous-{uuid.uuid4().hex}"
        transcript_path = os.path.abspath(os.path.expanduser(str(transcript_path or ""))) if transcript_path else ""
        cutoff = max(0, int(transcript_rows or 0))
        now = _now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                """SELECT j.* FROM review_job_turns t
                   JOIN review_jobs j ON j.id = t.job_id
                   WHERE t.session_id = ? AND t.turn_id = ?""",
                (session_id, turn_id),
            ).fetchone()
            if duplicate is not None:
                conn.commit()
                return {
                    "enqueued": False,
                    "coalesced": False,
                    "duplicate": True,
                    "job_id": int(duplicate["id"]),
                    "job": _as_dict(duplicate),
                }

            pending = conn.execute(
                """SELECT * FROM review_jobs
                   WHERE session_id = ? AND status = 'pending'
                   ORDER BY id LIMIT 1""",
                (session_id,),
            ).fetchone()
            if pending is not None:
                job_id = int(pending["id"])
                previous_trigger = str(pending["trigger"] or "")
                incoming_trigger = str(trigger or "unspecified")
                combined_signal = bool(pending["signal"]) or bool(signal)
                has_interval = "interval" in previous_trigger or "interval" in incoming_trigger
                combined_trigger = (
                    "signal+interval"
                    if combined_signal and has_interval
                    else "signal"
                    if combined_signal
                    else "interval"
                    if has_interval
                    else incoming_trigger
                )
                sources = []
                for source in (pending["signal_source"], signal_source):
                    source = str(source or "")
                    if source and source != "none" and source not in sources:
                        sources.append(source)
                combined_source = "+".join(sources) if sources else "none"
                incoming_path = transcript_path or str(pending["transcript_path"] or "")
                incoming_cutoff = cutoff if transcript_path else int(pending["transcript_rows"] or 0)
                incoming_model = str(model) if model else pending["model"]
                fallback_used = int(pending["model_fallback_used"] or 0)
                if model and str(model) != str(pending["model"] or ""):
                    fallback_used = 0
                conn.execute(
                    """UPDATE review_jobs SET
                       turn_id = ?, transcript_path = ?, transcript_rows = ?,
                       signal = ?, signal_source = ?, trigger = ?, model = ?,
                       model_fallback_used = ?,
                       updated_at = ?
                       WHERE id = ?""",
                    (
                        turn_id,
                        incoming_path,
                        incoming_cutoff,
                        int(combined_signal),
                        combined_source,
                        combined_trigger,
                        incoming_model,
                        fallback_used,
                        now,
                        job_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO review_job_turns(session_id, turn_id, job_id, created_at) VALUES(?,?,?,?)",
                    (session_id, turn_id, job_id, now),
                )
                row = conn.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone()
                conn.commit()
                return {
                    "enqueued": True,
                    "coalesced": True,
                    "duplicate": False,
                    "job_id": job_id,
                    "job": _as_dict(row),
                }

            cursor = conn.execute(
                """INSERT INTO review_jobs(
                       session_id, turn_id, transcript_path, transcript_rows,
                       signal, signal_source, trigger, model,
                       status, attempts, available_at, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,'pending',0,?,?,?)""",
                (
                    session_id,
                    turn_id,
                    transcript_path,
                    cutoff,
                    int(bool(signal)),
                    str(signal_source or ""),
                    str(trigger or "unspecified"),
                    str(model) if model else None,
                    now,
                    now,
                    now,
                ),
            )
            job_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO review_job_turns(session_id, turn_id, job_id, created_at) VALUES(?,?,?,?)",
                (session_id, turn_id, job_id, now),
            )
            row = conn.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone()
            conn.commit()
            return {
                "enqueued": True,
                "coalesced": False,
                "duplicate": False,
                "job_id": job_id,
                "job": _as_dict(row),
            }

    def get(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            return _as_dict(conn.execute("SELECT * FROM review_jobs WHERE id = ?", (job_id,)).fetchone())

    def list_jobs(self, *, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"invalid review job status: {status}")
        sql = "SELECT * FROM review_jobs"
        args: List[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            return [_as_dict(row) for row in conn.execute(sql, args).fetchall() if row is not None]

    def acquire_worker_lease(
        self,
        owner: str,
        *,
        pid: Optional[int] = None,
        lease_seconds: int = DEFAULT_WORKER_LEASE_SECONDS,
    ) -> bool:
        now = _now()
        pid = int(pid or os.getpid())
        pid_identity = _pid_identity(pid)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM review_worker_lease WHERE singleton = 1").fetchone()
            can_take = (
                current is None
                or current["owner"] == owner
                or not _pid_matches_identity(current["pid"], current["pid_identity"])
            )
            if not can_take:
                conn.commit()
                return False
            acquired_at = now if current is None or current["owner"] != owner else float(current["acquired_at"])
            conn.execute(
                """INSERT INTO review_worker_lease(
                       singleton, owner, pid, pid_identity, acquired_at, heartbeat_at, expires_at
                   ) VALUES(1,?,?,?,?,?,?)
                   ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner, pid=excluded.pid,
                       pid_identity=excluded.pid_identity,
                       acquired_at=excluded.acquired_at, heartbeat_at=excluded.heartbeat_at,
                       expires_at=excluded.expires_at""",
                (
                    owner,
                    pid,
                    pid_identity,
                    acquired_at,
                    now,
                    now + max(10, int(lease_seconds)),
                ),
            )
            conn.commit()
            return True

    def heartbeat_worker(
        self, owner: str, *, lease_seconds: int = DEFAULT_WORKER_LEASE_SECONDS
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_worker_lease SET heartbeat_at = ?, expires_at = ?
                   WHERE singleton = 1 AND owner = ?""",
                (now, now + max(10, int(lease_seconds)), owner),
            )
            return cur.rowcount == 1

    def release_worker_lease(self, owner: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM review_worker_lease WHERE singleton = 1 AND owner = ?", (owner,)
            )
            return cur.rowcount == 1

    def worker_lease(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM review_worker_lease WHERE singleton = 1").fetchone()
            return dict(row) if row is not None else None

    def recover_expired_jobs(self) -> int:
        """Return abandoned jobs to pending, or finish them from a valid result file."""
        now = _now()
        recovered = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM review_jobs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                # Never recover over a live worker merely because a heartbeat
                # lease expired. That would let two Codex reviewers mutate
                # personal skills concurrently until the old worker wakes and
                # observes fencing. A dead PID is the recovery boundary; a
                # live stale worker is surfaced for manual attention.
                if _pid_matches_identity(
                    row["worker_pid"], row["worker_pid_identity"]
                ):
                    continue
                result: Optional[Dict[str, Any]] = None
                result_path = row["result_path"]
                if result_path:
                    try:
                        result_file = Path(result_path)
                        if result_file.is_symlink():
                            raise OSError("result path is a symbolic link")
                        os.chmod(result_file, 0o600)
                        result = validate_result(json.loads(result_file.read_text(encoding="utf-8")))
                    except (OSError, json.JSONDecodeError, ValueError):
                        result = None
                if result is not None and result.get("status") != "failed":
                    conn.execute(
                        """UPDATE review_jobs SET status='done', result_json=?, completed_at=?, updated_at=?,
                           lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                           worker_pid_identity=NULL,
                           error_code=NULL, last_error=NULL WHERE id=?""",
                        (json.dumps(result, ensure_ascii=False), now, now, row["id"]),
                    )
                elif int(row["attempts"] or 0) >= MAX_ATTEMPTS:
                    error_code = (
                        "review_reported_failure"
                        if result is not None
                        else "worker_interrupted"
                    )
                    last_error = (
                        "Review returned status failed"
                        if result is not None
                        else "worker exited before recording a result"
                    )
                    conn.execute(
                        """UPDATE review_jobs SET status='failed', error_code=?,
                           last_error=?, completed_at=?, updated_at=?,
                           lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                           worker_pid_identity=NULL
                           WHERE id=?""",
                        (error_code, last_error, now, now, row["id"]),
                    )
                else:
                    attempts = max(1, int(row["attempts"] or 0))
                    delay = int(
                        RETRY_DELAYS_SECONDS[
                            min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)
                        ]
                    )
                    error_code = (
                        "review_reported_failure"
                        if result is not None
                        else "worker_interrupted"
                    )
                    last_error = (
                        "Review returned status failed"
                        if result is not None
                        else "worker exited before recording a result"
                    )
                    conn.execute(
                        """UPDATE review_jobs SET status='pending', available_at=?, updated_at=?,
                           error_code=?, last_error=?,
                           retry_delay_seconds=?,
                           lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                           worker_pid_identity=NULL
                           WHERE id=?""",
                        (now + delay, now, error_code, last_error, delay, row["id"]),
                    )
                recovered += 1
            conn.commit()
        return recovered

    def claim_next(
        self,
        owner: str,
        *,
        pid: Optional[int] = None,
        lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        self.recover_expired_jobs()
        now = _now()
        pid = int(pid or os.getpid())
        pid_identity = _pid_identity(pid)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM review_jobs
                   WHERE status='pending' AND available_at <= ? AND attempts < ?
                   ORDER BY available_at, id LIMIT 1""",
                (now, MAX_ATTEMPTS),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            started = row["started_at"] if row["started_at"] is not None else now
            conn.execute(
                """UPDATE review_jobs SET status='running', attempts=attempts+1,
                   lease_owner=?, lease_expires_at=?, heartbeat_at=?, worker_pid=?,
                   worker_pid_identity=?,
                   started_at=?, updated_at=?, result_path=NULL, retry_delay_seconds=NULL
                   WHERE id=?""",
                (
                    owner,
                    now + max(10, int(lease_seconds)),
                    now,
                    pid,
                    pid_identity,
                    started,
                    now,
                    row["id"],
                ),
            )
            claimed = conn.execute("SELECT * FROM review_jobs WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return _as_dict(claimed)

    def set_result_path(self, job_id: int, owner: str, result_path: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET result_path=?, updated_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (str(result_path), _now(), int(job_id), owner),
            )
            return cur.rowcount == 1

    def mark_model_fallback_used(self, job_id: int, owner: str) -> bool:
        """Persist the one allowed source-model fallback across queue retries."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET model_fallback_used=1, updated_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (_now(), int(job_id), owner),
            )
            return cur.rowcount == 1

    def heartbeat_job(
        self,
        job_id: int,
        owner: str,
        *,
        lease_seconds: int = DEFAULT_JOB_LEASE_SECONDS,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (now, now + max(10, int(lease_seconds)), now, int(job_id), owner),
            )
            return cur.rowcount == 1

    def complete(self, job_id: int, owner: str, result: Dict[str, Any]) -> bool:
        normalized = validate_result(result)
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET status='done', result_json=?, completed_at=?, updated_at=?,
                   lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                   worker_pid_identity=NULL,
                   error_code=NULL, last_error=NULL, retry_delay_seconds=NULL
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (json.dumps(normalized, ensure_ascii=False), now, now, int(job_id), owner),
            )
            return cur.rowcount == 1

    def fail(self, job_id: int, owner: str, *, code: str, message: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM review_jobs WHERE id=? AND status='running' AND lease_owner=?",
                (int(job_id), owner),
            ).fetchone()
            if row is None:
                conn.commit()
                return {"updated": False, "status": None, "retry_delay_seconds": None}
            attempts = int(row["attempts"] or 0)
            if attempts >= MAX_ATTEMPTS:
                status, delay, available, completed = "failed", None, now, now
            else:
                delay = int(RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)])
                status, available, completed = "pending", now + delay, None
            conn.execute(
                """UPDATE review_jobs SET status=?, available_at=?, completed_at=?, updated_at=?,
                   error_code=?, last_error=?, retry_delay_seconds=?, lease_owner=NULL,
                   lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                   worker_pid_identity=NULL
                   WHERE id=?""",
                (
                    status,
                    available,
                    completed,
                    now,
                    str(code)[:128],
                    str(message)[:4000],
                    delay,
                    int(job_id),
                ),
            )
            conn.commit()
            return {"updated": True, "status": status, "retry_delay_seconds": delay}

    def block(self, job_id: int, owner: str, *, code: str, message: str) -> bool:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET status='blocked', error_code=?, last_error=?, completed_at=?,
                   updated_at=?, lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL,
                   worker_pid=NULL, worker_pid_identity=NULL
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (str(code)[:128], str(message)[:4000], now, now, int(job_id), owner),
            )
            return cur.rowcount == 1

    def retry(self, job_id: int) -> bool:
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE review_jobs SET status='pending', attempts=0, available_at=?, completed_at=NULL,
                   updated_at=?, error_code=NULL, last_error=NULL, retry_delay_seconds=NULL,
                   lease_owner=NULL, lease_expires_at=NULL, heartbeat_at=NULL, worker_pid=NULL,
                   worker_pid_identity=NULL,
                   result_json=NULL, model_fallback_used=0
                   WHERE id=? AND status IN ('failed','blocked')""",
                (now, now, int(job_id)),
            )
            return cur.rowcount == 1

    def next_available_delay(self) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(available_at) AS ready FROM review_jobs WHERE status='pending' AND attempts < ?",
                (MAX_ATTEMPTS,),
            ).fetchone()
        if row is None or row["ready"] is None:
            return None
        return max(0.0, float(row["ready"]) - _now())

    def status(self) -> Dict[str, Any]:
        with self._connect() as conn:
            counts = {name: 0 for name in JOB_STATUSES}
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM review_jobs GROUP BY status"):
                counts[str(row["status"])] = int(row["n"])
            failure = conn.execute(
                """SELECT id, error_code, last_error, updated_at FROM review_jobs
                   WHERE error_code IS NOT NULL ORDER BY updated_at DESC LIMIT 1"""
            ).fetchone()
            worker = conn.execute("SELECT * FROM review_worker_lease WHERE singleton=1").fetchone()
        return {
            "queue_path": str(self.path),
            "counts": counts,
            "worker": dict(worker) if worker is not None else None,
            "last_failure": dict(failure) if failure is not None else None,
        }

    def cleanup(self, *, retention_days: int = RETENTION_DAYS) -> int:
        cutoff = _now() - max(1, int(retention_days)) * 86400
        with self._connect() as conn:
            cur = conn.execute(
                """DELETE FROM review_jobs
                   WHERE status IN ('done','failed') AND completed_at < ?""",
                (cutoff,),
            )
            return int(cur.rowcount)


def enqueue(**kwargs: Any) -> Dict[str, Any]:
    """Convenience wrapper used by the Stop hook."""
    return ReviewQueue().enqueue(**kwargs)


def queue_status() -> Dict[str, Any]:
    return ReviewQueue().status()
