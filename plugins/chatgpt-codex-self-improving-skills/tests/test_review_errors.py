import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from review_errors import classify_failure


def event(error):
    return json.dumps({"type": "turn.failed", "error": error})


@pytest.mark.parametrize("text", [
    "401 Unauthorized", "2026-09-13T01:00:00.401Z", "authentication required",
    '{"type":"error","status":401}',
])
def test_quoted_tool_and_model_content_never_become_auth_errors(text):
    output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    assert classify_failure(1, output).code == "codex_failed"
    assert classify_failure(0, output) is None


@pytest.mark.parametrize("payload,code,retryable", [
    ({"message": json.dumps({"status": 400, "error": {"message": "The 'gpt-6-astra' model requires a newer version of Codex. Please upgrade."}})}, "cli_upgrade_required", False),
    ({"status": 401, "message": "private credential details"}, "authentication_required", False),
    ({"code": "refresh_token_reused", "message": "private token"}, "authentication_required", False),
    ({"message": "unexpected status 401 Unauthorized"}, "authentication_required", False),
    ({"message": "a timestamp at 12:00:00.401Z"}, "codex_failed", True),
    ({"status": 403, "message": "Forbidden"}, "codex_failed", True),
    ({"code": "model_not_found", "message": "model unavailable"}, "model_unavailable", False),
    ({"status": 429}, "rate_limited", True),
    ({"code": "usage_limit_reached"}, "rate_limited", True),
    ({"status": 503}, "network_error", True),
    ({"message": "stream disconnected before completion"}, "network_error", True),
    ({"code": "mcp_start_failed"}, "mcp_start_failed", True),
])
def test_terminal_error_classification(payload, code, retryable):
    result = classify_failure(1, event(payload))
    assert result.code == code
    assert result.retryable is retryable
    assert "private" not in result.message


def test_final_failure_wins_over_recoverable_warning():
    output = json.dumps({"type": "error", "message": "model not found"}) + "\n" + event({"status": 401})
    assert classify_failure(1, output).code == "authentication_required"


def test_timeout_has_priority_over_any_partial_event():
    assert classify_failure(1, event({"status": 401}), timed_out=True).code == "timeout"


def test_non_json_output_is_not_interpreted():
    assert classify_failure(1, "401 Unauthorized\nnot json").code == "codex_failed"


def test_tool_runtime_failure_is_not_masked_by_success_exit_code():
    warning = json.dumps({'type':'item.completed','item':{'type':'error',
        'message':'Code Mode is unavailable because code-mode host is disabled.'}})
    failure = classify_failure(0, warning)
    assert failure.code == 'tool_runtime_unavailable' and failure.retryable is False
    quote = json.dumps({'type':'item.completed','item':{'type':'agent_message',
        'text':'Code Mode is unavailable because code-mode host is disabled.'}})
    assert classify_failure(0, quote) is None


def test_required_mcp_startup_failure():
    failure=classify_failure(1,event({'message':'required MCP server self-improving-skills failed to initialize'}))
    assert failure.code=='mcp_start_failed'
