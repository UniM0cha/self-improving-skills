"""MCP contract tests: dynamic serverInfo version + read-before-write guard."""

import json
import os
import subprocess
import sys

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
PLUGIN_ROOT = os.path.abspath(os.path.join(SCRIPTS_DIR, ".."))


def _drive(tmp_path, skills_root, requests):
    env = dict(os.environ,
               PLUGIN_DATA=str(tmp_path / "data"),
               CODEX_SELF_IMPROVE_SKILL_ROOTS=str(skills_root),
               CODEX_SELF_IMPROVE_CREATE_ROOT=str(skills_root))
    stdin = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS_DIR, "skill_manager_mcp.py")],
        input=stdin, capture_output=True, text=True, env=env, check=False)
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _call(message_id, tool, arguments):
    return {"jsonrpc": "2.0", "id": message_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}


def test_serverinfo_version_matches_plugin_json(tmp_path):
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    ])
    manifest = json.loads(open(
        os.path.join(PLUGIN_ROOT, ".codex-plugin", "plugin.json"), encoding="utf-8").read())
    assert responses[0]["result"]["serverInfo"]["version"] == manifest["version"]


def test_patch_requires_view_first(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "guarded"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: guarded\ndescription: d\n---\nbody\n", encoding="utf-8")
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call(2, "codex_skill_patch",
              {"name": "guarded", "old_text": "body", "new_text": "body v2"}),
        _call(3, "codex_skill_view", {"name": "guarded"}),
        _call(4, "codex_skill_patch",
              {"name": "guarded", "old_text": "body", "new_text": "body v2"}),
    ])
    by_id = {r["id"]: r for r in responses}
    blind = by_id[2]["result"]
    assert blind["isError"] is True
    assert "Read before write" in blind["content"][0]["text"]
    assert by_id[3]["result"]["isError"] is False
    assert by_id[4]["result"]["isError"] is False  # unlocked by the view
    assert "body v2" in (skill / "SKILL.md").read_text(encoding="utf-8")


def test_new_support_file_exempt_from_guard(tmp_path):
    skills_root = tmp_path / "skills"
    skill = skills_root / "fresh"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fresh\ndescription: d\n---\nbody\n", encoding="utf-8")
    responses = _drive(tmp_path, skills_root, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call(2, "codex_skill_write_file",
              {"name": "fresh", "file_path": "references/notes.md", "content": "hello"}),
    ])
    by_id = {r["id"]: r for r in responses}
    assert by_id[2]["result"]["isError"] is False  # creating a NEW file is exempt
