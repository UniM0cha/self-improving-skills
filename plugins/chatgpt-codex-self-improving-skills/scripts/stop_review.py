#!/usr/bin/env python3
"""Codex Stop hook: detect self-improvement signals and request review.

Trigger design (Hermes codex_runtime port):
  * The interval trigger counts accumulated TOOL ITERATIONS (bumped by the
    PostToolUse hook into usage.json's `counters.iters_since_review`, under
    the same usage_lock — no bare state.json read-modify-write race), not
    Stop turns. A ten-tool-call turn and a zero-tool chat turn are not the
    same amount of work; Hermes fires its Codex review at N tool iterations
    and resets the counter. Real skill work (create/patch/write through the
    manager) also resets it.
  * The signal regex keeps only EXPLICIT corrective phrasings. Topic words
    (skill/hook/improve/...) made the old regex self-trip on the plugin's own
    vocabulary in ordinary conversation; whether a correction is durable is
    the review prompt's judgement, not the regex's.
  * A transcript-window signal is CONSUMED once seen (scan offset persisted
    per transcript), so the same old message can't re-fire the review on
    every subsequent Stop while it stays inside the window.
  * Skill-use attribution: `$skill-name` mentions in new transcript rows bump
    that skill's use count (the curator's "actually unused" signal needs real
    use data; view-time bumps alone miss prompt-invoked runs).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from typing import Any, Dict, Iterable, List, Tuple

import skill_store
from skill_store import load_state, now_iso, record_review_signal

# Explicit corrective expressions ONLY (see module docstring).
SIGNAL_RE = re.compile(
    r"(remember this|next time|always|don't|do not|stop doing|wrong|incorrect|"
    r"반드시|항상|다음부터|기억해|기억하|하지 ?마|하지 말|틀렸|잘못)",
    re.IGNORECASE,
)

MAX_TRACKED_TRANSCRIPTS = 20
SKILL_MENTION_RE = re.compile(r"\$([a-z0-9][a-z0-9._-]{0,63})")


def _iter_strings(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        if value.strip():
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, dict):
        # "payload": real Codex rollout rows wrap the message as
        # response_item.payload.content — without descending into it, no
        # transcript text is ever seen.
        for key in ("text", "content", "message", "body", "value", "payload"):
            if key in value:
                yield from _iter_strings(value[key], depth + 1)


def _row_role(row: Dict[str, Any]) -> str:
    role = row.get("role") or row.get("type")
    message = row.get("message")
    if not role and isinstance(message, dict):
        role = message.get("role") or message.get("type")
    payload = row.get("payload")
    if isinstance(payload, dict):
        # response_item rows carry the actual role one level down; the outer
        # type ("response_item") must not shadow it
        role = payload.get("role") or role
    return str(role or "").lower()


# Bounded tail: hooks run on EVERY Stop, so the transcript must never be
# held fully in memory (long sessions grow without limit — the old 80-row
# deque cap exists for a reason). TAIL_ROWS bounds both memory and the
# attribution window; offsets below are ABSOLUTE row indices.
TAIL_ROWS = 400


def _read_tail(transcript_path: str) -> Tuple[int, List[Dict[str, Any]], int]:
    """(total_row_count, tail_rows, tail_start_absolute_index) — streaming
    parse keeping only the last TAIL_ROWS parsed rows in memory."""
    total = 0
    tail: deque = deque(maxlen=TAIL_ROWS)
    if not transcript_path or not os.path.isfile(transcript_path):
        return 0, [], 0
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    total += 1
                    tail.append(row)
    except Exception:
        return 0, [], 0
    rows = list(tail)
    return total, rows, total - len(rows)


def _user_text(rows: List[Dict[str, Any]], tail_start: int, signal_start: int,
               window: int = 80, char_limit: int = 20_000) -> str:
    lo = max(int(signal_start) - tail_start, len(rows) - window, 0)
    parts: List[str] = []
    for row in rows[lo:]:
        if _row_role(row) != "user":
            continue
        text = " ".join(_iter_strings(row))
        if text:
            parts.append(text)
    return "\n".join(parts)[-char_limit:]


def _signal_source(last_user: str, last_assistant: str,
                   rows: List[Dict[str, Any]], tail_start: int,
                   signal_start: int) -> Tuple[bool, str]:
    if SIGNAL_RE.search(last_user):
        return True, "last_user_message"
    if SIGNAL_RE.search(last_assistant):
        return True, "last_assistant_message"
    if SIGNAL_RE.search(_user_text(rows, tail_start, signal_start)):
        return True, "transcript_user_messages"
    return False, "none"


def _attribute_skill_uses(rows: List[Dict[str, Any]], tail_start: int,
                          start: int) -> None:
    """Bump use counts for `$skill-name` INVOCATIONS in NEW transcript rows
    (sibling plugin's analyze_turn attribution, adapted to $-invocation).
    USER rows only: assistant prose, tool output, or a SKILL.md read into
    context can all MENTION `$foo` without anyone invoking it — counting
    those would keep genuinely unused skills alive forever. Rows older than
    the kept tail are unscannable and skipped — attribution is best-effort
    telemetry, bounded memory wins."""
    lo = max(int(start) - tail_start, 0)
    if lo >= len(rows):
        return
    try:
        known = {item["name"] for item in skill_store.list_skills()["skills"]}
    except Exception:
        return
    if not known:
        return
    seen = set()
    for row in rows[lo:]:
        if _row_role(row) != "user":
            continue
        for text in _iter_strings(row):
            for match in SKILL_MENTION_RE.finditer(text):
                name = match.group(1).rstrip("._-")
                if name in known:
                    seen.add(name)
    for name in seen:
        try:
            skill_store.record_usage(name, use=True)
        except Exception:
            pass


def _prune_transcripts(tstate: Dict[str, Any]) -> None:
    if len(tstate) <= MAX_TRACKED_TRANSCRIPTS:
        return
    oldest = sorted(tstate.items(), key=lambda kv: str(kv[1].get("t") or ""))
    for key, _ in oldest[: len(tstate) - MAX_TRACKED_TRANSCRIPTS]:
        tstate.pop(key, None)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session = skill_store.hook_session_key(payload)

    if payload.get("stop_hook_active"):
        # End of a review continuation. stop_hook_active is session-wide (ANY
        # Stop hook's continuation sets it), so only consume the counter when
        # OUR marker says this plugin started the continuation — the review's
        # own tool calls inflated the counter, another plugin's didn't earn
        # a reset.
        def _pop_marker(st: Dict[str, Any]) -> bool:
            markers = st.get("awaiting_review_stop")
            if isinstance(markers, dict):
                return bool(markers.pop(session, False))
            # legacy boolean shape: consume it once, whoever gets here first
            return bool(st.pop("awaiting_review_stop", False))

        try:
            if skill_store.mutate_state(_pop_marker):
                skill_store.consume_review_counter(session=session)
        except Exception:
            pass
        return 0

    turn_id = str(payload.get("turn_id") or payload.get("turnId") or "")
    last_assistant = str(payload.get("last_assistant_message") or payload.get("lastAssistantMessage") or "")
    last_user = str(payload.get("last_user_message") or payload.get("lastUserMessage") or "")
    transcript_path = str(payload.get("transcript_path") or payload.get("transcriptPath") or "")

    # Read-only peek at the offsets (the atomic update happens below —
    # concurrent sessions sharing one PLUGIN_DATA must not last-writer-win
    # each other's transcript bookkeeping).
    peek = load_state()
    entry_peek = peek.get("transcripts", {}).get(transcript_path) if transcript_path else None
    entry_peek = entry_peek if isinstance(entry_peek, dict) else {}
    total_rows, rows, tail_start = _read_tail(transcript_path)
    rows_seen = int(entry_peek.get("rows_seen") or 0)
    signal_seen = int(entry_peek.get("signal_rows_seen") or 0)
    if rows_seen > total_rows:
        rows_seen = 0  # transcript rotated/shrank
    if signal_seen > total_rows:
        signal_seen = 0

    try:
        _attribute_skill_uses(rows, tail_start, rows_seen)
    except Exception:
        pass

    signal, signal_source = _signal_source(last_user, last_assistant,
                                           rows, tail_start, signal_seen)
    try:
        iters = skill_store.get_review_counter(session=session)
    except Exception:
        iters = 0
    interval = int(os.environ.get("CODEX_SELF_IMPROVE_INTERVAL", "10") or "10")

    def _bookkeep(state: Dict[str, Any]) -> int:
        state["stop_turns"] = int(state.get("stop_turns") or 0) + 1
        if transcript_path:
            tstate = state.setdefault("transcripts", {})
            entry = dict(tstate.get(transcript_path) or {})
            if signal:
                # Consume on ANY signal source: a payload-sourced signal
                # (last_user/last_assistant) is also present in the transcript
                # rows, so leaving the offset behind would re-fire the SAME
                # message as transcript_user_messages on the next Stop.
                entry["signal_rows_seen"] = total_rows
            entry["rows_seen"] = total_rows
            entry["t"] = now_iso()
            tstate[transcript_path] = entry
            _prune_transcripts(tstate)
        return state["stop_turns"]

    try:
        turns = skill_store.mutate_state(_bookkeep)
    except Exception:
        turns = int(peek.get("stop_turns") or 0) + 1

    record = {
        "at": now_iso(),
        "turn_id": turn_id,
        "turn_count": turns,
        "iters_since_review": iters,
        "signal": signal,
        "signal_source": signal_source,
        "transcript_path": transcript_path,
    }
    record_review_signal(record)

    if not skill_store.auto_continue_enabled():
        return 0

    should_continue = signal or (interval > 0 and iters >= interval)
    if not should_continue:
        return 0

    def _claim(state: Dict[str, Any]) -> bool:
        if state.get("last_auto_turn_id") == turn_id:
            return False  # another hook instance already fired for this turn
        state["last_auto_turn_id"] = turn_id
        markers = state.get("awaiting_review_stop")
        if not isinstance(markers, dict):
            markers = {}
        markers[session] = True  # OUR continuation, THIS session, in flight
        state["awaiting_review_stop"] = markers
        return True

    try:
        if not skill_store.mutate_state(_claim):
            return 0
    except Exception:
        return 0
    try:
        # atomic read-and-zero: a plain reset would erase increments that
        # landed between our earlier read and now (parallel PostToolUse)
        skill_store.consume_review_counter(session=session)
    except Exception:
        pass
    prompt = (
        "Run $self-improving-skills-review as a short post-turn learning pass. "
        "Inspect the current transcript only for durable workflow or skill lessons. "
        "If there is a lesson, act on the EARLIEST applicable rung of the review "
        "ladder: patch a skill that was in play this session > extend an existing "
        "skill > embed the corrected preference in the governing skill > create a "
        "new class-level skill. Patch or create skills only when the lesson is "
        "class-level, backed by this thread, and safe to persist. "
        "'Nothing to save.' is a real option — but it is not the default; walk the "
        "ladder before reaching for it."
    )
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": prompt,
                "systemMessage": "Codex Self Improvement requested a post-turn review.",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
