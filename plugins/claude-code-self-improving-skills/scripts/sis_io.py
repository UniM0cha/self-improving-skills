#!/usr/bin/env python3
"""Shared stdio helper for this plugin's entry-point scripts.

One place so the UTF-8 policy cannot drift across the ~dozen hooks and CLIs.
"""

import sys


def pin_utf8_stdio(streams=None):
    """Force UTF-8 on std streams, overriding the platform locale codec.

    Claude Code exchanges UTF-8 with hooks over pipes and these CLIs print
    Korean status text, but Python encodes a pipe with the platform locale codec
    — a legacy code page (cp1252) on a non-Korean Windows, the console itself
    being UTF-8 — which raises UnicodeEncodeError/UnicodeDecodeError on this
    plugin's Korean text and (under a hook's fail-safe) silently drops it.

    Best-effort and idempotent: streams are resolved at call time, and one
    without a reconfigure() method — e.g. a pytest capture object — is skipped.
    """
    if streams is None:
        streams = (sys.stdin, sys.stdout, sys.stderr)
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass
