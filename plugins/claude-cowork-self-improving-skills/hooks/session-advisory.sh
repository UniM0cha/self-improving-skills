#!/bin/bash
# UserPromptSubmit hook — inject the Cowork self-improvement advisory ONCE per
# session (first prompt). Replaces the original plugin's SessionStart hook:
# on cold Cowork containers plugin hooks are loaded before the plugin files
# finish syncing, so the SessionStart EVENT passes before this plugin's hook
# exists — but by the time the first user prompt is processed the runtime has
# waited for plugin sync, so UserPromptSubmit fires reliably.
# Delegates to session_advisory.py. Fails safe to silent on any error.
set -uo pipefail

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_advisory.py" 2>/dev/null || true
exit 0
