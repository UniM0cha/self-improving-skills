#!/bin/bash
# PreToolUse hook (Write|Edit|MultiEdit) — back up a learned SKILL.md before edit
# so validate-skill.sh can roll back a structure-breaking change. Silent; never
# blocks the edit (fail-safe).
set -uo pipefail
. "${CLAUDE_PLUGIN_ROOT}/hooks/python3.sh"
[ -n "$SIS_PYTHON" ] || exit 0

$SIS_PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/backup_skill.py" 2>/dev/null || true
exit 0
