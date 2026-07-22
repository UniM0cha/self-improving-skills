# Self-Improving Skills

**English** | [한국어](README.ko.md)

> **Hermes Agent-style self-improvement for Claude Code, Claude Cowork, Codex, and ChatGPT work.**
>
> It turns hard-won workflow lessons into reusable `SKILL.md` files, validates skill edits, curates stale knowledge, and (since v0.9.0) lets a team share learned skills through a git repo — without ever overwriting anyone's personal customizations.

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
- **Share** proven skills with your team — opt-in, review-gated, personalization always wins.

## Features

- **Automatic distillation nudge**: a `Stop` hook blocks **once per segment of work** when enough tool calls and file edits accumulated since the last distillation. A declined nudge stays declined; it only re-fires after another threshold of *new* work. The nudge includes the transcript path so the background distiller can read what actually happened.
- **Dedicated distiller subagent**: prefers patching existing skills, then umbrella skills, then adding reference files, and creates a new class-level skill only as a last resort. Skill descriptions follow Anthropic's skill-creator guidance (third-person situation match with concrete trigger phrases).
- **Skill edit safety**: pre-edit backups, post-edit validation, provenance stamping, and automatic rollback on malformed `SKILL.md`. Non-blocking quality advisories (e.g. over-long descriptions that cost context in every session).
- **Accurate usage telemetry**: skill use/view/patch counts in `~/.claude/self-improve/skill_usage.json`. Patch counting runs in the `PostToolUse` hook so edits made by *background* subagents are captured too, and bulk reads during curation never reset a skill's idle clock.
- **Curator loop**: unused agent-created skills go stale after 30 days and are archived (recoverably) after 90. Skills proven by repeated use (`use_count >= 3`) age at half speed. The LLM curation pass (`/curate-skills`) is an umbrella-building consolidation modeled on Hermes' curator prompt — plan first, apply only after approval.
- **Team skill sharing (v0.9.0)**: share learned skills through a team git repo with **origin-hash sync** — see below.
- **Manual commands**: `/distill-skill`, `/curate-skills`, `/curator-status`, `/prune-skills`, `/archive-skill`, `/pin-skill`, `/restore-skill`, `/share-skill`, `/sync-team-skills`, `/propose-plugin-improvement`.
- **Fail-safe hooks**: hook errors approve the original action instead of breaking your Claude Code session.

## Team skill sharing

Point the plugin at your team's (usually private) skills repo — nothing is hardcoded:

```jsonc
// ~/.claude/self-improve/team_config.json
{
  "repo": "your-org/your-team-skills",
  "subdir": "skills"
}
```

- **Publish** with `/share-skill <name>`: the skill is scanned (secrets, local paths, injection patterns), generalized (techniques stay, personal style is stripped), shown to you as a diff, and opened as a PR against the team repo. A human merges.
- **Receive** with `/sync-team-skills`: a fresh shallow clone, a read-only plan you confirm, then per-skill transactional apply.

The **origin-hash rule** makes sharing safe by construction. Each installed team skill records a deterministic content hash at install time:

| Your local copy | Sync behavior |
|---|---|
| Untouched (hash == origin) | Auto-updated to the team's latest |
| **Customized by you** | **Never overwritten** — a one-time "diverged" notice; share your version back if you want |
| Deleted or archived by you | Never re-installed (until `--reinstall <name>`) |
| Name collides with a personal skill | Skipped with a warning |

Skills are *instructions to an agent* — i.e. a prompt-injection vector — so every write of team content (first install **and** later updates) passes a static scanner (secrets, destructive commands, injection markers, symlinks, hidden files, size caps). Blocked content lands in quarantine, never in `~/.claude/skills`. Team skills are marked `created_by: team` and are never touched by your personal curator: their owner is the team repo.

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
| `SIS_DISTILL_THRESHOLD` | `12` | Tool-call count since the last distillation before the nudge can trigger |
| `SIS_MIN_FILE_EDITS` | `2` | Minimum file edits since the last distillation; prevents pure research chats from triggering |
| `SIS_CURATE_MIN_SKILLS` | `8` | Minimum learned-skill count before automatic curation runs |
| `SIS_CURATE_INTERVAL_DAYS` | `7` | Automatic curator interval |
| `SIS_STALE_AFTER_DAYS` | `30` | Mark unused agent-created skills as stale after this many inactive days |
| `SIS_ARCHIVE_AFTER_DAYS` | `90` | Move unused agent-created skills to `.archive/` after this many inactive days (doubled for skills with `use_count >= 3`) |
| `SIS_TEAM_SKILLS_REPO` | unset | Team repo override (`owner/name`); the primary source is `~/.claude/self-improve/team_config.json` |
| `SIS_TEAM_SYNC_REMIND_DAYS` | `7` | Days after the last team sync before SessionStart suggests `/sync-team-skills` (no network, once a day) |
| `SIS_PLUGIN_PR` | unset | Set to `1` to allow the opt-in upstream PR helper for this plugin's own source |

## How it works

```text
Claude Code session ends
  ↓
Stop hook parses the transcript and usage offsets
  ↓
If the work was complex and not yet distilled, it returns a one-time block
  ↓
Claude delegates to claude-code-self-improving-skills:skill-distiller (background)
  ↓
The distiller patches or creates a reusable SKILL.md under ~/.claude/skills
  ↓
Validation hooks check frontmatter/size/provenance and roll back bad edits
  ↓
Next session: Claude Code discovers the skill normally
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
                                         #   team sync engine, security scanner, PR plumbing
  agents/skill-distiller.md              # subagent prompt for skill distillation
  commands/                              # slash commands exposed by the plugin
  tests/                                 # pytest suite (uv run --with pytest -- pytest tests/)
  README.md                              # detailed design notes (Korean)
```

## Honest limitations

- Claude Code does not provide Hermes Agent's free background daemon thread. Distillation uses a visible/billable subagent turn.
- This plugin handles procedural memory (`SKILL.md`), not factual memory. Claude Code's native memory features or separate memory plugins should handle facts about you or your projects.
- The curator is intentionally conservative: it archives only agent-created learned skills and keeps recoverable backups.
- Team sync is PR-gated by design, not real-time. A shared real-time skill store would let one compromised session inject instructions into every teammate's agent; the human review gate **is** the security boundary.

## License

MIT
