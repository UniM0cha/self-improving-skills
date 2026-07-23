#!/bin/bash
# PostToolUse hook (Write|Edit|MultiEdit) — validate & provenance-stamp learned
# SKILL.md files. Delegates to validate_skill.py. Silent for non-skill edits and
# fails safe to silent on any error.
set -uo pipefail
. "${CLAUDE_PLUGIN_ROOT}/hooks/python3.sh"
[ -n "$SIS_PYTHON" ] || exit 0

$SIS_PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/validate_skill.py" 2>/dev/null || true
exit 0
