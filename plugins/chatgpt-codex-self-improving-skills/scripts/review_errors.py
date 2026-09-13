"""Classify Codex JSON error events without interpreting transcript echoes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ReviewFailure:
    code: str
    stage: str
    retryable: bool
    message: str


def _failure(code: str, stage: str, retryable: bool, message: str) -> ReviewFailure:
    return ReviewFailure(code, stage, retryable, message)


GENERIC = _failure("codex_failed", "execution", True, "Codex review execution failed")


def _terminal_error(stdout: str) -> Any:
    """Only actual error/turn.failed envelopes are authoritative.

    In particular item.completed can carry model output or tool results that
    merely quote error JSON. Plain stderr is never part of this parser.
    """
    error: Any = None
    failed: Any = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            failed = event.get("error")
        elif event.get("type") == "error":
            error = event.get("error", event)
    return failed if failed is not None else error


def _error_fields(value: Any, depth: int = 0) -> tuple[set[str], set[int], list[str]]:
    if depth > 4:
        return set(), set(), []
    codes: set[str] = set()
    statuses: set[int] = set()
    messages: list[str] = []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict):
            return _error_fields(decoded, depth + 1)
        return codes, statuses, [value[:16000].lower()]
    if not isinstance(value, dict):
        return codes, statuses, messages
    for key in ("code", "type"):
        item = value.get(key)
        if isinstance(item, str):
            codes.add(item.lower())
    for key in ("status", "status_code", "http_status"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool):
            statuses.add(item)
    for key in ("error", "message"):
        if key in value:
            more_codes, more_statuses, more_messages = _error_fields(value[key], depth + 1)
            codes.update(more_codes)
            statuses.update(more_statuses)
            messages.extend(more_messages)
    return codes, statuses, messages


def classify_failure(returncode: int, stdout: str, *, timed_out: bool = False) -> Optional[ReviewFailure]:
    if timed_out:
        return _failure("timeout", "execution", True, "Codex review exceeded the 600 second timeout")
    if returncode == 0:
        return None
    codes, statuses, messages = _error_fields(_terminal_error(stdout))
    message = "\n".join(messages)
    if "requires a newer version of codex" in message or "cli_upgrade_required" in codes:
        return _failure("cli_upgrade_required", "compatibility", False, "Update the selected Codex CLI to use this model")
    auth_codes = {"authentication_required", "invalid_api_key", "invalid_authentication",
                  "token_expired", "refresh_token_reused", "refresh_token_expired", "invalid_grant"}
    if 401 in statuses or codes & auth_codes or re.search(
        r"(?:unexpected status|http(?: status)?(?: code)?)\s*:?\s*401\b|^401 unauthorized\b",
        message,
    ):
        return _failure("authentication_required", "authentication", False, "Codex authentication is required")
    if 429 in statuses or codes & {"rate_limit_exceeded", "usage_limit_reached", "rate_limited"}:
        return _failure("rate_limited", "rate_limit", True, "Codex usage or rate limit reached")
    if codes & {"model_not_found", "model_unavailable", "unsupported_model"} or re.search(
        r"(?:unknown|unsupported|unavailable|not found|does not exist|no access|access denied).{0,100}model|"
        r"model.{0,100}(?:unknown|unsupported|unavailable|not found|does not exist|no access|access denied)",
        message, re.DOTALL,
    ):
        return _failure("model_unavailable", "model", False, "The requested Codex model is unavailable")
    if codes & {"mcp_start_failed", "mcp_startup_failed"} or re.search(r"mcp.{0,80}(?:startup|start|handshake).{0,40}failed", message):
        return _failure("mcp_start_failed", "mcp", True, "The skill-manager connection could not start")
    if codes & {"network_error", "connection_error", "stream_disconnected", "server_error"} or statuses & {500, 502, 503, 504} or re.search(
        r"stream disconnected|connection (?:reset|refused|closed)|network (?:error|unreachable)|error sending request", message,
    ):
        return _failure("network_error", "network", True, "A temporary Codex connection error occurred")
    return GENERIC
