# Self-Improving Skills

**English** | [한국어](README.ko.md)

> **Hermes Agent-style self-improvement for Claude Code, Claude Cowork, Codex, and ChatGPT work.**
>
> It turns hard-won workflow lessons into reusable `SKILL.md` files, validates skill edits, curates stale knowledge, and (since v0.13.0) distills in a **detached background worker** — your visible turn ends normally while a headless session captures the technique.

Claude Code already has hooks, subagents, slash commands, and skills. This plugin wires those primitives into a closed learning loop inspired by [Nous Research Hermes Agent](https://github.com/NousResearch/hermes-agent):

```text
complex task → distill what worked → save/patch a skill → rediscover next session
```

## Variants

One repo, four ports of the same closed learning loop:

| Plugin | Environment |
|---|---|
| `claude-code-self-improving-skills` | Claude Code CLI (this README's main subject) |
| `claude-cowork-self-improving-skills` | Claude Cowork (cloud container) — persists skills via claude.ai '스킬 저장' |
| `chatgpt-codex-self-improving-skills` | OpenAI Codex CLI (hooks + MCP skill manager) |
| `chatgpt-work-self-improving-skills` | ChatGPT desktop Work (skills-only package in the shared `plugins/` directory) |

Install one per environment — do not install two variants into the same environment (duplicate hooks/nudges).

## Why this exists

Most coding agents can solve a hard problem once. Fewer can reliably remember the reusable part of that work and apply it later.

Hermes Agent has first-class procedural memory through skills and a curator loop. This project ports that idea into Claude Code as a plugin:

- **Detect** complex work using transcript/tool-call signals.
- **Nudge** the agent at the right time to distill the reusable technique.
- **Write or patch** `~/.claude/skills/<name>/SKILL.md` through a dedicated subagent.
- **Validate and roll back** malformed skill edits automatically.
- **Track usage** so stale generated skills can be archived instead of piling up forever.
- **Distill in the background** by default — a queued job and a detached `claude -p` session, with an in-turn nudge as the automatic fallback.

## Features

- **Background distillation (v0.13.0, default)**: when enough tool calls and file edits accumulate, the `Stop` hook silently enqueues a job (SQLite queue) and a **detached worker** runs a fenced headless `claude -p` session that reads the transcript and writes the skill — your turn ends with zero extra output. If the CLI is missing, outdated, or signed out, the hook automatically falls back to the classic in-turn nudge, which blocks **once per segment of work** and includes the transcript path.
- **Fenced distillation session**: the background child runs with a reduced tool set (no Bash), permission deny rules that survive `bypassPermissions`, a per-job budget cap, and a post-run **skill guard** that snapshots `~/.claude/skills` before the run, validates every touched `SKILL.md` after it, and rolls back anything malformed or out of scope. Distillation prefers patching existing skills over creating new ones (Anthropic skill-creator guidance).
- **Skill edit safety**: pre-edit backups, post-edit validation, provenance stamping, and automatic rollback on malformed `SKILL.md`. Non-blocking quality advisories (e.g. over-long descriptions that cost context in every session).
- **Accurate usage telemetry**: skill use/view/patch counts in `~/.claude/self-improve/skill_usage.json`. Patch counting runs in the `PostToolUse` hook so edits made by *background* sessions are captured too, and bulk reads during curation never reset a skill's idle clock.
- **Curator loop**: unused agent-created skills go stale after 30 days and are archived (recoverably) after 90. Skills proven by repeated use (`use_count >= 3`) age at half speed. The LLM curation pass (`/curate-skills`) is an umbrella-building consolidation modeled on Hermes' curator prompt — plan first, apply only after approval.
- **Manual commands**: `/distill-skill`, `/distill-status`, `/curate-skills`, `/curator-status`, `/curator-rollback`, `/prune-skills`, `/archive-skill`, `/pin-skill`, `/restore-skill`, `/migration`, `/propose-plugin-improvement`.
- **Fail-safe hooks**: hook errors approve the original action instead of breaking your Claude Code session.
- **Cross-platform**: macOS, Linux, and Windows (Git Bash), verified by a 3-OS CI matrix — including UTF-8 output on non-Korean Windows locales.

## Background distillation setup

Background mode needs a `claude` CLI (>= 2.1.205) that can authenticate headlessly. If you use a subscription, generate a long-lived token once and put it where the worker reads it:

```bash
claude setup-token
install -m 600 /dev/null ~/.claude/self-improve/worker.env
# then put one line in that file:  CLAUDE_CODE_OAUTH_TOKEN=<token>
```

An API key (`ANTHROPIC_API_KEY`) in the environment works too. Without working auth the plugin keeps functioning — it just falls back to the in-turn nudge. `/distill-status` shows the queue, recent jobs, and the exact remedy for anything blocked.

The child session writes to `~/.claude/skills` under `bypassPermissions`, so the plugin layers defenses instead of trusting it: no Bash, deny rules on credential and persistence paths (deny rules still apply in bypass mode), a hard budget cap per job, an unguessable evidence boundary around the untrusted transcript, and the post-run skill guard that reverts anything that fails validation. The deny list is a blocklist, not a proof — the plugin README documents the full security model honestly.

## Install

### Claude Code

Add this repository as a Claude Code plugin marketplace from inside Claude Code:

```text
/plugin marketplace add UniM0cha/self-improving-skills
/plugin install claude-code-self-improving-skills@self-improving-skills
```

If your Claude Code version uses a different plugin command shape, add `https://github.com/UniM0cha/self-improving-skills` as a marketplace from Claude Code's plugin UI and install the `claude-code-self-improving-skills` plugin.

### Codex and ChatGPT Work

Add this repository as a Codex plugin marketplace, then install the Codex variant:

```bash
codex plugin marketplace add UniM0cha/self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

The ChatGPT desktop Plugins Directory currently shows both variants together. Choose `chatgpt-codex-self-improving-skills` in Codex and `chatgpt-work-self-improving-skills` in Work mode. The current desktop app does not reliably separate the two surfaces with marketplace `policy.products`.

For an existing installation, refresh the marketplace and reinstall the plugin:

```bash
codex plugin marketplace upgrade self-improving-skills
codex plugin add chatgpt-codex-self-improving-skills@self-improving-skills
```

After upgrading, restart the ChatGPT desktop app so it reloads the marketplace. Both variants appear under `Self-Improving Skills`; install the one for the mode you are using. See the [official plugin documentation](https://learn.chatgpt.com/docs/build-plugins) for the marketplace format.

## Configuration

All configuration is optional. Set these in your shell or in `~/.claude/settings.json` under `env`.

| Variable | Default | Meaning |
|---|---:|---|
| `SIS_REVIEW_MODE` | `background` | `background` (detached worker, zero output in your turn) / `foreground` (classic nudge) / `off`. Background falls back to foreground automatically when the CLI cannot run |
| `SIS_CLAUDE_BIN` | auto-detect | Absolute path to `claude`; useful when a GUI-spawned hook lacks `~/.local/bin` on PATH |
| `SIS_DISTILL_MAX_USD` | `0.50` | `--max-budget-usd` cap per distillation job |
| `SIS_DISTILL_MAX_JOBS_PER_DAY` | `12` | Daily cap on spawned background distillation sessions |
| `SIS_DISTILL_THRESHOLD` | `12` | Tool-call count since the last distillation before distillation can trigger |
| `SIS_MIN_FILE_EDITS` | `2` | Minimum file edits since the last distillation; prevents pure research chats from triggering |
| `SIS_DISTILL_READONLY_THRESHOLD` | `24` | Edit-free segments still distill past this many tool calls (diagnostic technique from long investigations) |
| `SIS_STATE_DIR` | `~/.claude/self-improve` | Moves the queue, backups, and telemetry together |
| `SIS_CURATE_MIN_SKILLS` | `8` | Minimum learned-skill count before automatic curation runs |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | Automatic curator interval |
| `SIS_STALE_AFTER_DAYS` | `30` | Mark unused agent-created skills as stale after this many inactive days |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | Move unused agent-created skills to `.archive/` after this many inactive days (doubled for skills with `use_count >= 3`) |
| `SIS_PLUGIN_PR` | unset | Set to `1` to allow the opt-in upstream PR helper for this plugin's own source |

## How it works

```text
Claude Code session ends
  ↓
Stop hook parses the transcript and usage offsets
  ↓
If the work was complex and not yet distilled, it enqueues a job (SQLite)
and returns approve — your turn ends with no extra output
  ↓
A detached worker claims the job (PID-identity lease, retries, backoff)
  ↓
It runs a fenced headless `claude -p` session: reduced tools, deny rules,
budget cap, the transcript wrapped in an untrusted-evidence boundary
  ↓
The session patches or creates a reusable SKILL.md under ~/.claude/skills
  ↓
The skill guard diffs a pre-run snapshot, validates every touched SKILL.md,
and rolls back anything malformed or out of scope
  ↓
Next session: Claude Code discovers the skill normally
(fallback: with no usable CLI the Stop hook blocks once with the classic nudge)
```

The learned skills live in your user directory, not inside the plugin. Updating the plugin does not erase your accumulated procedural knowledge.

## Repository layout

```text
.claude-plugin/marketplace.json          # Claude Code marketplace manifest (2 plugins)
.agents/plugins/marketplace.json         # Codex + ChatGPT Work marketplace manifest
plugins/claude-cowork-self-improving-skills/   # Cowork variant
plugins/chatgpt-codex-self-improving-skills/   # Codex variant
plugins/chatgpt-work-self-improving-skills/    # ChatGPT Work variant
plugins/claude-code-self-improving-skills/
  .claude-plugin/plugin.json             # plugin metadata
  hooks/                                 # Stop, SessionStart, PreToolUse, PostToolUse wrappers
  scripts/                               # transcript analysis, telemetry, curator, validator,
                                         #   distillation queue, detached worker, skill guard
  agents/skill-distiller.md              # subagent prompt for the foreground fallback
  commands/                              # slash commands exposed by the plugin
  tests/                                 # pytest suite (uv run --with pytest -- pytest tests/)
  README.md                              # detailed design notes (Korean)
```

## Honest limitations

- Background distillation is invisible but not free: the detached `claude -p` session consumes your subscription or API usage (bounded by `SIS_DISTILL_MAX_USD` per job and `SIS_DISTILL_MAX_JOBS_PER_DAY`).
- The background child writes to `~/.claude/skills` under `bypassPermissions`. The deny rules, reduced tool set, and post-run skill guard layer real defenses, but the deny list is a blocklist — the plugin README documents exactly what that does and does not guarantee.
- The evidence is the session transcript, which is untrusted input. It is fenced with an unguessable boundary and framed as data-not-instructions, which lowers, not eliminates, prompt-injection risk.
- This plugin handles procedural memory (`SKILL.md`), not factual memory. Claude Code's native memory features or separate memory plugins should handle facts about you or your projects.
- The curator is intentionally conservative: it archives only agent-created learned skills and keeps recoverable backups.

## License

MIT
