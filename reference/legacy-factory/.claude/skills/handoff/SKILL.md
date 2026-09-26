---
name: handoff
description: Hand the current work off to opencode running a specific model. Saves a context summary, resolves the model name, then launches opencode so it can continue the work autonomously.
argument-hint: <model name or partial> [task description]
allowed-tools: Bash, Read, Write, Glob, Grep
---

The user wants to hand off the current work to opencode running a specific model. $ARGUMENTS contains the model name (or partial name) and optionally a task description.

## Step 1 — Resolve the model ID

Run `opencode models 2>/dev/null` and fuzzy-match the model name from $ARGUMENTS against the output. Pick the best single match. If ambiguous, show the top 3 options and ask the user to clarify before continuing.

## Step 2 — Write a handoff context file

Write a structured handoff file to `.opencode_handoff/handoff_$(date +%Y%m%d_%H%M%S).md` with the following sections, populated from your knowledge of the current session:

```markdown
# Handoff Context
Generated: <timestamp>
Model: <resolved model id>

## What we were working on
<1-3 sentence summary of the current goal>

## What has been completed
<bullet list of concrete things done this session>

## What is pending / needs doing next
<bullet list of remaining work, ordered by priority>

## Key files
<list of files that are most relevant to the task, with one-line descriptions>

## Pipeline state (if applicable)
<current issue states, worker status, any known blockers>

## Errors / blockers
<any known failures, CI status, things that need attention>

## Notes for the next agent
<anything non-obvious the next agent should know — decisions made, things to avoid, patterns to follow>
```

Be thorough. The opencode agent will have zero conversation context — the handoff file is all it gets.

## Step 3 — Build the opencode prompt

Construct a self-contained prompt string. It should:
- Tell the agent to read the handoff file first: `Read .opencode_handoff/<filename>.md`
- State the primary task clearly (from the task description in $ARGUMENTS if provided, otherwise from the pending work in the handoff)
- Reference the CLAUDE.md and relevant project docs
- Remind the agent to commit its work with conventional commits and run `make lint` before committing
- End with: "When you are done, write a brief completion summary to `.opencode_handoff/<filename>_done.md`"

## Step 4 — Launch opencode

Run:
```bash
opencode run -m "<resolved-model-id>" --dir "<current working directory>" "<prompt>"
```

Stream the output. When it finishes, check if a `_done.md` file was written and show the user a summary of what the agent did.

## Notes
- The pipeline infrastructure lives in `scripts/opencode_pipeline.sh` — if the task involves the pipeline, include relevant sections from that file in the context
- Model pool files: `.opencode_logs/model_test_results.txt` (Tier 2) and `.opencode_logs/model_tool_test_results.txt` (Tier 1 / tool-use verified)
- Timesheet: `.opencode_logs/timesheet.jsonl` — useful for understanding what models have worked recently
- Worktrees: `~/.opencode-worktrees/comic-pile/issue_N` — one per open issue
- If the model name is a z.ai / zai model, note in the handoff that zai-coding-plan has a ~$6/mo plan with ~2hr heavy-use window before hitting a 3hr rate limit
