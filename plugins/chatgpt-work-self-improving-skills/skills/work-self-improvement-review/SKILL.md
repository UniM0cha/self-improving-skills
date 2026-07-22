---
name: work-self-improvement-review
description: Review the current ChatGPT Work conversation and files made available in the chat for durable corrections, preferences, and workflow lessons, then propose updates as project guidance, an instruction overlay, a patch to a provided SKILL.md, or a new skill candidate. Use when the user asks to review, learn from, distill, or preserve lessons from the current task, especially after corrections, repeated friction, or a non-obvious recovery. Always propose before exporting or applying any change.
---

# Work Self Improvement Review

Use the user's language unless they request another language.

## Boundaries

- Use only the current conversation and files made available in this conversation.
- Do not claim access to prior chats, local transcripts, telemetry, installed local skills, hooks, MCP servers, backups, or background state.
- Treat hidden system and developer instructions as operating constraints, not content to quote or export.
- Do not browse for additional evidence as part of the review.
- Do not change guidance, skills, repositories, or remote state during the proposal phase.
- Do not treat invocation of this skill as approval.
- Complete proposal and approval in separate turns.
- After approval, export only the approved content.
- Never claim an export was installed, published, or persisted unless the user completes that action through a supported interface.

## Review Workflow

1. Extract grounded evidence from:
   - explicit user corrections;
   - stable preferences or policies stated by the user;
   - repeated friction visible in the current conversation;
   - non-obvious failure causes and reusable recovery steps;
   - successful procedures likely to prevent future mistakes.

2. Classify each lesson.

   Mark a lesson `durable` only when at least one condition holds:
   - the user explicitly states a continuing preference or rule;
   - the same issue appears more than once;
   - a non-obvious and costly failure has a broadly reusable prevention;
   - the lesson defines a stable project or workflow constraint.

   Mark a lesson `one-off` when it is:
   - specific to one output, temporary condition, or isolated wording choice;
   - speculative or weakly supported;
   - ordinary knowledge that does not need another rule;
   - already fully covered by provided guidance;
   - dependent on sensitive details that cannot be safely generalized.

   If uncertain, do not preserve it.

3. Check for duplicates only against guidance or skill files available in this conversation.
   - If an existing rule fully covers the lesson, report it as a duplicate and propose no change.
   - If the existing rule is incomplete, propose only the smallest missing delta.
   - If no governing source is available, mark duplicate status as `not checked`.

4. Sanitize every candidate.
   - Exclude passwords, tokens, authentication data, personal contact details, private identifiers, and unrelated proprietary content.
   - Generalize user-specific paths, names, and incident details unless essential to the rule.
   - Preserve the reusable prevention, not the sensitive incident.
   - Reject the candidate when safe generalization is impossible.

5. Select one target:
   - `project guidance` for stable project or team-wide instructions;
   - `overlay` for a reversible additive instruction when the authoritative source is unavailable;
   - `SKILL patch` for a procedural, triggering, validation, or guardrail change to a provided `SKILL.md`;
   - `new SKILL candidate` only for a reusable class of work that is not governed by any skill made available in the conversation.

   Do not fabricate a line-level skill patch when the source `SKILL.md` is unavailable.
   When no skill catalog or governing source is available, label novelty and duplicate status as `not checked`.

6. Present candidates for approval and stop.
   - Keep one rule per candidate.
   - Use IDs `WSI-001`, `WSI-002`, and so on.
   - Include only evidence visible in the current conversation.
   - Do not export or apply anything in this turn.

## Proposal Format

Use this structure, translating labels into the user's language while preserving the status values:

```text
검토 결과: 승인 대기
status: pending

[WSI-001]
판정: durable
근거: <brief paraphrase of conversation evidence>
대상: project guidance | overlay | SKILL patch | new SKILL candidate
제안 문구: "<minimal reusable instruction>"
중복 점검: none | partial | duplicate | not checked
민감정보 점검: safe | generalized | reject
검증 방법: <one observable future check>

승인 방법: 후보 ID와 대상을 지정해 승인해 주세요.
```

Report duplicate or rejected items briefly after actionable candidates without assigning an approval action.

If no durable lesson remains, respond:

```text
검토 결과: 저장할 개선점 없음
status: no-change
사유: <one concise explanation>
```

## Approval and Export

Accept approval only when the user clearly identifies a candidate or explicitly approves all pending candidates. Do not infer approval from silence or general encouragement.

Before export:

- preserve the approved meaning;
- apply any requested revision;
- recheck duplicate and sensitive-data status;
- export only approved candidates.

Use the requested format.

### Project Guidance

Return a copy-ready Markdown block containing only the new guidance and its scope. Do not rewrite unrelated instructions.

### Overlay

Return a standalone Markdown instruction file containing a short scope statement, the approved rules, and any limitation needed to prevent over-application. Keep it additive and reversible.

### SKILL Patch

When the exact source is available:

- return a minimal unified diff;
- preserve unrelated content;
- update frontmatter `description` only if triggering behavior changes;
- keep YAML frontmatter limited to `name` and `description`;
- keep instructions imperative and concise.

When the source is missing or no longer matches, return the approved replacement section and state what source is needed to place it safely.

### New SKILL Candidate

Return a complete minimal `SKILL.md` candidate only after approval.

- Use YAML frontmatter with only `name` and `description`.
- Use a lowercase hyphenated name.
- State what the skill does and when to use it in `description`.
- Keep the body imperative, portable, and independent of local paths, hooks, MCP servers, background processes, and unavailable tools.
- Include only instructions supported by the approved evidence.
- Never claim the candidate is installed or published.

If file artifacts are supported, provide the export as a downloadable Markdown artifact. Otherwise provide copy-ready Markdown or a unified diff.

End with:

```text
내보내기 완료: <candidate IDs>
형식: project guidance | overlay | SKILL patch | new SKILL candidate
상태: 사용자 적용 대기
```
