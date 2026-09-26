---
name: orchestrator
description: Route a task to the cheapest capable model via opencode. Claude writes the handoff and stops — it does not execute, monitor, or follow up.
argument-hint: <model name or partial> <task description>
allowed-tools: Bash, Read, Write, Glob, Grep
---

The user wants to delegate a task to another model via opencode. $ARGUMENTS contains an optional model name and task description. Your job is to write a handoff file and launch opencode. That is the entirety of your job.

## Hard rules — violations are not permitted under any circumstances

1. **Stop after launching.** Once `opencode run` exits, your turn is over. Do not interpret output, do not check results, do not report on what the agent did beyond showing the final line of stdout.
2. **Never use ScheduleWakeup.** Not for CI polling, not for "checking back later," not for anything.
3. **Never take over a failed handoff.** If opencode exits non-zero or the agent didn't finish: write a second handoff to a different model and launch that. If two handoffs both fail, report the failure to the user and stop. Do not start doing the work yourself.
4. **Never perform the delegated work directly.** If you find yourself writing code, running tests, or editing files to complete the task — stop. That is the agent's job.
5. **CI / PR monitoring always goes to a free model.** If the task involves watching CI, polling GitHub, or waiting for CodeRabbit: the handoff prompt must tell the agent to do that work. You do not watch CI yourself.

## Step 1 — Pick a model

Run `opencode models 2>/dev/null`.

- If $ARGUMENTS names a model: fuzzy-match it and use the best single match.
- If no model is named: default to the cheapest tool-capable model available. Look for NIM GLM 4.5 first, then other free/cheap models. Avoid paid Claude/GPT models unless the task explicitly requires them.
- If the match is ambiguous, show the top 3 and ask before continuing.

## Step 2 — Write the handoff file

Write to `.opencode_handoff/handoff_$(date +%Y%m%d_%H%M%S).md`:

```markdown
# Handoff Context
Generated: <timestamp>
Model: <resolved model id>

## Task
<1-3 sentence description of exactly what needs to be done>

## Context
<what has already been done; current state of the repo/PR/CI>

## What done looks like
<specific, verifiable completion criteria — e.g. "all CI checks green", "PR merged", "make pytest passes">

## Key files
<files most relevant to the task>

## Constraints
- Run `make lint` before every commit
- Use conventional commits
- Do not use ScheduleWakeup
- Do not hand back to Claude — complete the task or write a _done.md explaining what's left

## Errors / blockers
<known failures, CI status, anything that needs attention>
```

## Step 3 — Build the opencode prompt

The prompt must be self-contained. Include:
- `Read .opencode_handoff/<filename>.md` as the first instruction
- The primary task in one sentence
- A reference to `CLAUDE.md` for project conventions
- `make lint` reminder before commits
- End with: "When finished, write `.opencode_handoff/<filename>_done.md` with a one-paragraph summary of what was done."

## Step 4 — Launch

```bash
opencode run -m "<model-id>" --dir "<cwd>" "<prompt>"
```

Show output as it streams. When done:
- If `_done.md` exists: print its contents and stop.
- If `_done.md` does not exist and exit code is non-zero: write a second handoff to the next cheapest model and launch it (one retry only).
- If both attempts fail: tell the user what failed and stop.

## Model preference order (cheapest first)

When no model is specified, try in this order:
1. NIM GLM 4.5 (free)
2. Any other NIM free-tier model with tool use
3. zai-coding-plan / GLM-5 (check rate limit status in `.opencode_logs/timesheet.jsonl`)
4. Only escalate to a paid model if the task explicitly requires it and the user has approved cost

## Notes
- Timesheet: `.opencode_logs/timesheet.jsonl` — check recent model activity before picking
- Tool-verified models: `.opencode_logs/model_tool_test_results.txt`
- Worktrees: `~/.opencode-worktrees/comic-pile/issue_N`
