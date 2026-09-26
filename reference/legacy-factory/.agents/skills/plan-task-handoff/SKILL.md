---
name: plan-task-handoff
description: Select the next executable ComicPile performance-campaign issue (from #687), prepare it for implementation, then hand it off to a worker model via opencode. Trigger whenever the user asks to pick the next task, work the next performance issue, or keep the #687 campaign moving with a plan-then-handoff workflow. Combines issue selection, planning/research/validation, and the handoff mechanism into one loop.
compatibility: Requires git, gh CLI authentication, opencode CLI, and the repository's GitHub remote.
---

# Plan-Task-Handoff

One agent (the planner) determines which issue should be done next, prepares it,
researches and validates the risky parts, then hands the fully-specified task to a
worker model. The planner does **not** implement the issue.

The output of this skill is: an issue moved to `ralph-status:in-progress` on a named
branch, a handoff context file, and a launched `opencode run` worker process.

## When to use

Trigger when the user says anything like "do the next task", "what's next in the
campaign", "plan and hand off", "start the next performance issue", or asks to keep
#687 moving without doing the implementation in this session.

## Step 1 — Understand the campaign

Run these in order, reading in bounded chunks:

1. `AGENTS.md`
2. `docs/ISSUE_EXECUTION_PROTOCOL.md`
3. GitHub issue **#687** in full — the authoritative performance roadmap. Note its
   wave ordering and its concurrency/conflict guidance.
4. GitHub issue **#641** when the candidate touches frontend architecture or server state.
5. Inspect the working tree (`git status`) and preserve all existing user changes.
6. Inspect all open PRs (`gh pr list --state open`) and all issues marked
   `ralph-status:in-progress` or `ralph-status:in-review`.

Do not use the archived Markdown kanban as a source of truth. Do not read every
comment on every issue — only the candidate's body, named files, and linked plans.

## Step 2 — Select exactly one issue

Choose one open issue from the #687 campaign using these rules, in order:

1. Must have: `ralph-task`, `ralph-status:pending`.
2. Must NOT have: `epic`, `ralph-status:blocked`, `ralph-status:in-progress`,
   `ralph-status:in-review`, `ralph-status:done`.
3. Every required dependency named in the issue or in #687 must be closed or
   explicitly described as non-blocking.
4. Prefer the **earliest incomplete wave** in #687. (This is the authoritative
   ordering — it overrides a priority-only selector. Example: when a priority-only
   selector picked a later-wave issue, wave ordering still required the earlier
   incomplete wave.)
5. Within that wave, prefer: `ralph-priority:critical` > `high` > `medium` > `low`.
6. Do not select work that conflicts with an open PR or active issue touching the
   same architectural lane or primary files.
7. Do not select #640 or #706 while earlier roadmap work remains.
8. Do not select an epic as an implementation task.
9. Do not choose unrelated work from #638 merely because its issue number is lower.

Respect #687's concurrency and conflict guidance verbatim (e.g. no #696 with a
substantial Queue portion of #704, no #668 with a substantial Roll portion of #704,
no #703 until replacement Roll/Queue/Thread Details paths exist, no #639 until #705's
API contracts are stable, no new custom cache for #667's frontend — use the #701/#702
Query architecture).

If more than one issue is equally eligible, choose the lowest-numbered one.

Before editing, print to the user:

- The selected issue number and title
- Its priority
- Its roadmap wave
- Its dependencies and their status
- Why it does not conflict with current open PRs
- The primary files expected to change

If no issue is executable, report the exact blockers and stop without changing code.

## Step 3 — Start the issue

1. Create a branch from the latest default branch, descriptive name based on the issue
   (e.g. `perf/<issue>-<short-slug>`).
2. Replace `ralph-status:pending` with `ralph-status:in-progress` via `gh issue edit`.
3. Add a short GitHub comment saying implementation has started and naming the branch.
4. Read the complete issue body and every linked local plan (e.g. `docs/issue-plans/<n>.md`).
5. Inspect the relevant implementation and existing tests before planning.

Do not broaden the issue. Do not create additional issues unless a genuinely separate
blocker is required. If the issue is too large for one safe pass, plan the smallest
coherent slice and document the rest in the handoff.

## Step 4 — Plan, research, and validate (planner's real job)

This is what makes the handoff succeed. The planner must reduce the worker's risk to
near zero:

1. Read the primary files the issue names, plus their callers and existing tests.
2. Run the baseline test suite for the affected area. Record the pass count so the
   worker knows the starting state:
   `bash -c 'set -a; source .env.test; set +a; .venv/bin/python -m pytest -o addopts= <focused-tests> -q'`
3. Validate any risky idioms (SQL, concurrency patterns, API shapes) against the live
   test DB **before** handoff. Record the exact working form in the handoff file so the
   worker does not re-derive them. For this repo: test DB is
   `postgresql+asyncpg://postgres:postgres@localhost:5437/comic_pile_test` (from
   `.env.test`).
4. Enumerate the acceptance criteria and map each to a concrete implementation step.
5. Define the exact behavior that must be preserved (error message strings tests assert
   on, function signatures, commit behavior, cache invalidation, ORM refresh patterns).

## Step 5 — Write the handoff file

Write to `.opencode_handoff/handoff_$(date +%Y%m%d_%H%M%S).md` with this structure:

```markdown
# Handoff Context
Generated: <timestamp>
Model: <resolved worker model id>

## What we were working on
<1-3 sentence summary of the goal, issue number + title>

## What has been completed
<selection, branch, labels, comments, research findings, validated idioms, baseline test counts>

## What is pending / needs doing next (ordered)
<implementation, tests, verification, benchmarks, PR steps — ordered>

## Key files
<files relevant to the task, one-line description each>

## Pipeline state
<issue labels, branch, open PRs, any conflicts>

## Errors / blockers
<known failures, CI status, things to avoid>

## Notes for the next agent
<validated SQL, exact working idioms, behavior to preserve, run commands, constraints>
```

Be thorough — the worker has zero conversation context.

## Step 6 — Resolve the worker model

Run `opencode models 2>/dev/null`.

- If the user named a model, fuzzy-match it.
- Default recommendation: `deepseek/deepseek-v4-pro` — the direct, more capable
  upgrade of the flash tier and well-suited to correctness-critical rewrites. If the
  user has a stated preference (e.g. "use a deepseek one"), honor it.
- If the user asked for the cheapest model, follow the model preference order in the
  `orchestrator` skill (free NIM first, escalate only when required).
- If ambiguous, show the top 3 and ask before continuing.

## Step 7 — Build the worker starting prompt

The worker prompt is a template. Fill in the bracketed placeholders from the selected
issue and handoff file:

```
Read the handoff file first: .opencode_handoff/<handoff-file>.md

Your task: Complete implementation of GitHub issue #[N] on JoshCLWren/comic-pile ('<issue-title>'). The branch <branch> is already created and checked out, and issue #[N] is already set to ralph-status:in-progress with a comment naming the branch.

The handoff file contains: full context, validated idioms (confirmed working against the live test DB), the exact design, environment/run commands, and the pending work list. Follow it exactly.

Mandatory steps:
1. Implement per the design in the handoff.
2. Add regression tests for the actual performance/scalability/correctness problem.
3. Run focused tests first, then the full pytest suite, then ruff + ty, then frontend lint/typecheck/build/test.
4. Collect before-and-after evidence (query count, timing, etc.) and record it.
5. Commit with a conventional message, push the branch, open a ready-for-review PR against main linking #[N], set #[N] to ralph-status:in-review, and add a verification comment.

CRITICAL: Never skip tests. Never use --no-verify or bypass hooks. No linter-ignore comments (# noqa, type: ignore, etc.). Preserve exact error message strings that existing tests assert on. Do NOT use CI as a debugger — everything must pass locally first. Keep database work set-based and bounded. Preserve auth/ownership checks, CSRF, mobile, and accessibility behavior. Preserve unrelated working-tree changes.

Run command: bash -c 'set -a; source .env.test; set +a; .venv/bin/python -m pytest -o addopts= <focused-tests> -q'

When you are done, write a brief completion summary to .opencode_handoff/<handoff-file>_done.md
```

For backend issues the focused test run is a must; for frontend issues lead with the
`cd frontend && pnpm run lint/typecheck/build/test` block. For browser-visible behavior
the worker must run Playwright per `AGENTS.md` (backend on port 9000).

## Step 8 — Launch the worker

```bash
opencode run -m "<resolved-model-id>" --dir "<repo-root>" "<worker-prompt>"
```

Stream the output. When it finishes:

1. Check whether `.opencode_handoff/<handoff-file>_done.md` exists.
2. **Independently verify** the worker's claims before reporting success: `git status`,
   `git log --oneline -5`, `git rev-parse HEAD` vs the remote branch
   (`git ls-remote --heads origin <branch>`), and `gh pr view <pr>` for state/mergeable.
3. Report to the user: selected issue, branch, commit, PR URL, test results,
   before/after evidence, and anything still awaiting review.
4. If the worker failed (non-zero exit, no `_done.md`, or claims don't hold): write a
   second handoff to the next model in preference order and retry once. If it fails
   again, report the failure to the user and stop — do not start implementing yourself.

## Hard rules

- The planner does **not** implement the issue. Once the worker launches, your turn is
  handoff + verification only.
- Never skip tests, never use `--no-verify`, never add linter-ignore comments.
- Do not use CI as a debugger — all checks must pass locally before the worker pushes.
- Do not mark the issue done or close it just because a PR exists. Leave it in review
  until merged and all acceptance criteria pass.
- Do not merge the PR unless the user explicitly authorized autonomous merging.
- The planner may reuse existing skills: `github-issue-kanban` for selection/status
  labels and `handoff` for the launch mechanics; this skill supersedes them for the
  full plan-then-handoff loop.

## Notes

- `.env.test` sets `ENABLE_RATE_LIMITING_IN_TESTS=false`, `SKIP_WORKTREE_CHECK=true`.
- Ports: dev 5435, test 5437, CI 5432. Coverage gate is 94%.
- `.opencode_handoff/` and `.opencode_logs/` hold prior handoffs and model test results —
  useful when deciding the worker model.
