#!/bin/bash
# SessionStart hook — inject the self-improvement advisory + curator reminder.
# Delegates to session_init.py. Fails safe to silent (no context) on any error.
set -uo pipefail
. "${CLAUDE_PLUGIN_ROOT}/hooks/python3.sh"
[ -n "$SIS_PYTHON" ] || exit 0

$SIS_PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/session_init.py" 2>/dev/null || true
exit 0
