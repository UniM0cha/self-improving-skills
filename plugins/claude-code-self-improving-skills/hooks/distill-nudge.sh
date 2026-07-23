#!/bin/bash
# Stop hook — queue a background distillation (or nudge, in foreground mode).
# Delegates all logic to analyze_turn.py. Fails safe to {"decision":"approve"}
# on ANY error so a broken analyzer can never wedge a session shut.
set -uo pipefail
. "${CLAUDE_PLUGIN_ROOT}/hooks/python3.sh"
if [ -z "$SIS_PYTHON" ]; then
  echo '{"decision":"approve"}'
  exit 0
fi

out=$($SIS_PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/analyze_turn.py" 2>/dev/null) || {
  echo '{"decision":"approve"}'
  exit 0
}
if [ -z "$out" ]; then
  echo '{"decision":"approve"}'
  exit 0
fi
printf '%s\n' "$out"
exit 0
