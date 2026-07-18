#!/usr/bin/env python3
"""CLI wrapper for the Codex self-improvement skill store.

Read-before-write, CLI approximation: the CLI process dies between calls, so
the MCP server's exact per-file view registry can't apply. Instead a patch or
write to an EXISTING file requires the skill's usage record to show a view
within the last CLI_VIEW_WINDOW_MINUTES (skill-level approximation — view any
file of the skill, then edit). `--force-unviewed` overrides for humans.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

import skill_store
from skill_store import (
    SkillStoreError,
    archive_skill,
    create_skill,
    curate,
    json_dumps,
    list_backups,
    list_skills,
    load_usage,
    patch_skill,
    pin_skill,
    prune_backups,
    restore_backup,
    restore_skill,
    status,
    view_skill,
    write_support_file,
)

CLI_VIEW_WINDOW_MINUTES = 30


def _require_recent_view(name: str, file_path: str) -> None:
    skill_dir = skill_store.find_skill(name)
    if not skill_dir:
        return  # skill-not-found surfaces downstream with its own error
    rel = skill_store._safe_relative_path(file_path or "SKILL.md")
    if not (skill_dir / rel).exists():
        return  # creating a new file is exempt
    rec = load_usage().get("skills", {}).get(skill_store.validate_name(name), {})
    viewed = skill_store._parse_time(rec.get("last_viewed_at"))
    now = datetime.now(timezone.utc)
    if viewed and (now - viewed).total_seconds() < CLI_VIEW_WINDOW_MINUTES * 60:
        return
    raise SkillStoreError(
        f"Read before write: run `view {name}` first (no view recorded in the "
        f"last {CLI_VIEW_WINDOW_MINUTES} minutes), then retry the edit using "
        "the content just returned. Pass --force-unviewed to override."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex self-improvement skill manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("list")
    sub.add_parser("usage")

    view = sub.add_parser("view")
    view.add_argument("name")
    view.add_argument("--file", default=None, help="supporting file to read instead of SKILL.md")

    create = sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--content-file", required=True)
    create.add_argument("--root")
    create.add_argument("--reason")

    patch = sub.add_parser("patch")
    patch.add_argument("name")
    patch.add_argument("--file", default="SKILL.md")
    patch.add_argument("--old", required=True)
    patch.add_argument("--new", required=True)
    patch.add_argument("--force-unviewed", action="store_true", default=False)

    write = sub.add_parser("write-file")
    write.add_argument("name")
    write.add_argument("file_path")
    write.add_argument("--content-file", required=True)
    write.add_argument("--force-unviewed", action="store_true", default=False)

    archive = sub.add_parser("archive")
    archive.add_argument("name")

    restore = sub.add_parser("restore")
    restore.add_argument("name")
    restore.add_argument("--root")

    pin = sub.add_parser("pin")
    pin.add_argument("name")

    unpin = sub.add_parser("unpin")
    unpin.add_argument("name")

    cur = sub.add_parser("curate")
    cur.add_argument("--dry-run", action="store_true", default=False)
    cur.add_argument("--apply", action="store_true", default=False)
    cur.add_argument("--stale-days", type=int, default=30)
    cur.add_argument("--archive-days", type=int, default=90)

    backups = sub.add_parser("backups")
    backups.add_argument("--skill")

    rollback = sub.add_parser("rollback")
    rollback.add_argument("backup_id")

    prune = sub.add_parser("prune-backups")
    prune.add_argument("--keep-per-skill", type=int, default=5)

    scan = sub.add_parser("scan")
    scan.add_argument("name")

    args = parser.parse_args()
    try:
        if args.cmd == "status":
            result = status()
        elif args.cmd == "list":
            result = list_skills()
        elif args.cmd == "usage":
            result = load_usage()
        elif args.cmd == "view":
            result = view_skill(args.name, file_path=args.file)
        elif args.cmd == "create":
            content = open(args.content_file, "r", encoding="utf-8").read()
            result = create_skill(args.name, content, root=args.root, reason=args.reason)
        elif args.cmd == "patch":
            if not args.force_unviewed:
                _require_recent_view(args.name, args.file)
            result = patch_skill(args.name, args.old, args.new, file_path=args.file)
        elif args.cmd == "write-file":
            if not args.force_unviewed:
                _require_recent_view(args.name, args.file_path)
            content = open(args.content_file, "r", encoding="utf-8").read()
            result = write_support_file(args.name, args.file_path, content)
        elif args.cmd == "archive":
            result = archive_skill(args.name)
        elif args.cmd == "restore":
            result = restore_skill(args.name, root=args.root)
        elif args.cmd == "pin":
            result = pin_skill(args.name, True)
        elif args.cmd == "unpin":
            result = pin_skill(args.name, False)
        elif args.cmd == "curate":
            dry_run = args.dry_run or not args.apply
            result = curate(dry_run=dry_run, stale_days=args.stale_days, archive_days=args.archive_days)
        elif args.cmd == "backups":
            result = list_backups(skill=args.skill)
        elif args.cmd == "rollback":
            result = restore_backup(args.backup_id)
        elif args.cmd == "prune-backups":
            result = prune_backups(keep_per_skill=args.keep_per_skill)
        elif args.cmd == "scan":
            skill_dir = skill_store.find_skill(args.name)
            if not skill_dir:
                raise SkillStoreError(f"Skill '{args.name}' was not found.")
            from scan_skill import SCANNER_VERSION, scan_dir

            findings = scan_dir(str(skill_dir))
            blocking = [f for f in findings if f.get("severity") == "block"]
            result = {
                "name": args.name,
                "scanner_version": SCANNER_VERSION,
                "findings": findings,
                "blocking": len(blocking),
                "warnings": len(findings) - len(blocking),
            }
        else:
            raise AssertionError(args.cmd)
    except SkillStoreError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json_dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
