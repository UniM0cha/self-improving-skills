#!/bin/bash
# SessionStart hook — inject the self-improvement advisory + curator reminder.
# Delegates to session_init.py. Fails safe to silent (no context) on any error.
set -uo pipefail

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_init.py" 2>/dev/null || true
exit 0
