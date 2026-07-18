#!/usr/bin/env python3
"""Minimal stdio MCP server for Codex self-improvement skill operations.

Read-before-write guard (Hermes skill_manager 20871c1d): patching or
overwriting an EXISTING file requires that this server session viewed exactly
that file first (codex_skill_view registers the resolved path). Creating a
new file is exempt. Unlike Hermes there is no background-review origin to
scope the guard to — the Stop hook continues the same session — so the guard
applies to the whole MCP session unconditionally (documented in README).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

import skill_store
from skill_store import (
    SkillStoreError,
    archive_skill,
    create_skill,
    curate,
    list_backups,
    list_skills,
    load_usage,
    patch_skill,
    pin_skill,
    plugin_root,
    prune_backups,
    restore_backup,
    restore_skill,
    status,
    view_skill,
    write_support_file,
)

# Resolved file paths this MCP session has actually read via codex_skill_view.
VIEWED_PATHS: set[str] = set()


def _plugin_version() -> str:
    """serverInfo.version comes from plugin.json — a hardcoded literal here
    already drifted once (0.1.0 vs 0.1.1 after a partial bump)."""
    try:
        manifest = json.loads(
            (plugin_root() / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return str(manifest.get("version") or "unknown")
    except Exception:
        return "unknown"


def _resolve_target(name: str, file_path: str) -> Path | None:
    """Resolved path of `file_path` inside skill `name` (None: skill absent)."""
    skill_dir = skill_store.find_skill(name)
    if not skill_dir:
        return None
    rel = skill_store._safe_relative_path(file_path or "SKILL.md")
    return (skill_dir / rel).resolve()


def _require_viewed(name: str, file_path: str) -> None:
    """Reject a write to an existing file this session never viewed."""
    target = _resolve_target(name, file_path)
    if target is None or not target.exists():
        return  # skill lookup errors surface downstream; new files are exempt
    if str(target) not in VIEWED_PATHS:
        raise SkillStoreError(
            f"Read before write: call codex_skill_view first for '{name}' "
            f"(file_path='{file_path or 'SKILL.md'}'), then retry the edit "
            "using the content just returned."
        )


def _schema(properties: Dict[str, Any], required: list[str] | None = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOLS: Dict[str, Dict[str, Any]] = {
    "codex_self_improvement_status": {
        "description": "Show plugin status, data directory, skill roots, and telemetry counts.",
        "inputSchema": _schema({}),
    },
    "codex_skill_list": {
        "description": "List Codex user/repo skills visible to the self-improvement manager.",
        "inputSchema": _schema({}),
    },
    "codex_skill_view": {
        "description": "Read a skill's SKILL.md (or a supporting file via file_path) and record a view+use event. Viewing also unlocks patch/write for that file (read-before-write guard).",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "file_path": {"type": "string", "default": "SKILL.md"},
            },
            ["name"],
        ),
    },
    "codex_skill_usage": {
        "description": "Return raw sidecar usage telemetry.",
        "inputSchema": _schema({}),
    },
    "codex_skill_create": {
        "description": "Create a new user/repo Codex skill with validated SKILL.md frontmatter (provenance-stamped). Pass reason to record why the skill was created.",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "content": {"type": "string"},
                "root": {"type": "string"},
                "reason": {"type": "string"},
            },
            ["name", "content"],
        ),
    },
    "codex_skill_patch": {
        "description": "Patch SKILL.md or a supporting file with backup and validation.",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "file_path": {"type": "string", "default": "SKILL.md"},
            },
            ["name", "old_text", "new_text"],
        ),
    },
    "codex_skill_write_file": {
        "description": "Write SKILL.md or a supporting file under references/templates/scripts/assets.",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            ["name", "file_path", "content"],
        ),
    },
    "codex_skill_archive": {
        "description": "Archive a skill reversibly. Pinned skills are protected.",
        "inputSchema": _schema({"name": {"type": "string"}}, ["name"]),
    },
    "codex_skill_restore": {
        "description": "Restore an archived skill.",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "root": {"type": "string"},
            },
            ["name"],
        ),
    },
    "codex_skill_pin": {
        "description": "Pin or unpin a skill. Pin blocks archive/delete but allows patching.",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "pinned": {"type": "boolean", "default": True},
            },
            ["name"],
        ),
    },
    "codex_skill_curate": {
        "description": "Run deterministic curator candidate selection. Dry-run is default. Writes a per-run report under logs/curator/.",
        "inputSchema": _schema(
            {
                "dry_run": {"type": "boolean", "default": True},
                "stale_days": {"type": "integer", "default": 30},
                "archive_days": {"type": "integer", "default": 90},
            }
        ),
    },
    "codex_skill_backups": {
        "description": "List skill backups (newest last), optionally filtered to one skill.",
        "inputSchema": _schema({"skill": {"type": "string"}}),
    },
    "codex_skill_rollback": {
        "description": "Restore a skill from a backup by exact backup_id. The current content is backed up first, so the rollback itself is undoable (undo_backup in the result).",
        "inputSchema": _schema({"backup_id": {"type": "string"}}, ["backup_id"]),
    },
    "codex_skill_prune_backups": {
        "description": "Keep only the newest N backups per skill and remove the rest.",
        "inputSchema": _schema({"keep_per_skill": {"type": "integer", "default": 5}}),
    },
    "codex_skill_scan": {
        "description": "Advisory security scan of one skill (secrets / prompt injection / invisible unicode / local paths). Never blocks — findings are for review.",
        "inputSchema": _schema({"name": {"type": "string"}}, ["name"]),
    },
}


def call_tool(name: str, args: Dict[str, Any]) -> Any:
    args = args or {}
    if name == "codex_self_improvement_status":
        return status()
    if name == "codex_skill_list":
        return list_skills()
    if name == "codex_skill_view":
        result = view_skill(args["name"], file_path=args.get("file_path"))
        viewed = (Path(result["path"]) / result.get("file", "SKILL.md")).resolve()
        VIEWED_PATHS.add(str(viewed))
        return result
    if name == "codex_skill_usage":
        return load_usage()
    if name == "codex_skill_create":
        return create_skill(args["name"], args["content"], root=args.get("root"),
                            reason=args.get("reason"))
    if name == "codex_skill_patch":
        file_path = args.get("file_path") or "SKILL.md"
        _require_viewed(args["name"], file_path)
        return patch_skill(
            args["name"],
            args["old_text"],
            args["new_text"],
            file_path=file_path,
        )
    if name == "codex_skill_write_file":
        _require_viewed(args["name"], args["file_path"])  # existing files only
        return write_support_file(args["name"], args["file_path"], args["content"])
    if name == "codex_skill_archive":
        return archive_skill(args["name"])
    if name == "codex_skill_restore":
        return restore_skill(args["name"], root=args.get("root"))
    if name == "codex_skill_pin":
        return pin_skill(args["name"], bool(args.get("pinned", True)))
    if name == "codex_skill_curate":
        return curate(
            dry_run=bool(args.get("dry_run", True)),
            stale_days=int(args.get("stale_days", 30)),
            archive_days=int(args.get("archive_days", 90)),
        )
    if name == "codex_skill_backups":
        return list_backups(skill=args.get("skill"))
    if name == "codex_skill_rollback":
        return restore_backup(args["backup_id"])
    if name == "codex_skill_prune_backups":
        return prune_backups(keep_per_skill=int(args.get("keep_per_skill", 5)))
    if name == "codex_skill_scan":
        skill_dir = skill_store.find_skill(args["name"])
        if not skill_dir:
            raise SkillStoreError(f"Skill '{args['name']}' was not found.")
        from scan_skill import SCANNER_VERSION, scan_dir

        findings = scan_dir(str(skill_dir))
        blocking = [f for f in findings if f.get("severity") == "block"]
        return {
            "name": args["name"],
            "scanner_version": SCANNER_VERSION,
            "findings": findings,
            "blocking": len(blocking),
            "warnings": len(findings) - len(blocking),
        }
    raise SkillStoreError(f"Unknown tool: {name}")


def send(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result(message_id: Any, value: Any) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "result": value})


def error(message_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}})


def handle(message: Dict[str, Any]) -> None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        result(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "self-improving-skills", "version": _plugin_version()},
            },
        )
        return

    if method == "tools/list":
        result(
            message_id,
            {
                "tools": [
                    {"name": name, **spec}
                    for name, spec in sorted(TOOLS.items(), key=lambda item: item[0])
                ]
            },
        )
        return

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        try:
            payload = call_tool(tool_name, args)
            result(
                message_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                        }
                    ],
                    "isError": False,
                },
            )
        except Exception as exc:
            result(
                message_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        return

    if message_id is not None:
        error(message_id, -32601, f"Unsupported method: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except json.JSONDecodeError as exc:
            error(None, -32700, f"Parse error: {exc}")
        except Exception as exc:
            error(None, -32603, f"Internal error: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

