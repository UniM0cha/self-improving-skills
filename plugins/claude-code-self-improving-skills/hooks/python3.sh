# Resolve a Python 3 interpreter, sourced by every hook in this plugin.
#
# `python3` does not exist on a standard Windows install — the official
# installer ships `python.exe` and the `py` launcher. Claude Code runs command
# hooks through Git Bash on Windows, so the script itself runs fine; it is the
# interpreter name inside it that silently fails, turning every hook into a
# no-op that nobody notices.
#
# Sets SIS_PYTHON. Leaves it empty when nothing usable is found, which callers
# treat as "stay silent" — a missing interpreter must never block a session.

sis_find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      # Reject the Windows Store stub, which exists on PATH but only prints an
      # advert and exits non-zero.
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)' \
          >/dev/null 2>&1; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c 'pass' >/dev/null 2>&1; then
    printf '%s' "py -3"
    return 0
  fi
  return 1
}

SIS_PYTHON="$(sis_find_python || printf '')"
