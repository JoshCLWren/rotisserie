---
name: github-issue-kanban
description: Use GitHub Issues as the Comic Pile kanban and execution queue. Trigger whenever the user asks an agent to do the next task, pick the next issue, work from the backlog, update issue status, plan issue work, or keep GitHub work synchronized. Use this skill before editing code for any issue-driven task in this repository.
---

# GitHub issue workflow

Use GitHub Issues as the only backlog and status source of truth. Do not maintain a parallel Markdown kanban. Local plan files are implementation references for large issues, not a second status system.

## Select work

From the repository root, run:

```bash
make next-task
```

Use the issue selected by that command unless the user explicitly names another issue. The selector prefers open issues with `ralph-status:pending`, highest `ralph-priority:*`, and no unresolved issue dependencies. Epics, blocked issues, issues already in progress/review, duplicates, and issues with unresolved dependencies are not selected.

Then read the selected issue body, the files it names, `AGENTS.md`, and
`docs/ISSUE_EXECUTION_PROTOCOL.md`. For factory work, also read
`docs/AUTONOMOUS_FACTORY_POLICY.md` and the latest canonical resume packet. Do not load unrelated
comment history. If the issue points to `docs/issue-plans/<number>.md`, read that plan in bounded
chunks.

## Start work

Before editing code:

1. Confirm the issue is open and still eligible.
2. Confirm dependencies are closed or explicitly non-blocking.
3. Move the issue to active work. For factory work, replace the complete label set atomically so
   it contains `factory`, `factory:building`, and exactly one owner label without exposing an
   intermediate contradictory state:

   ```bash
   gh api --method PUT repos/JoshCLWren/comic-pile/issues/ISSUE/labels \
     --input /path/to/complete-truthful-label-set.json
   gh issue comment ISSUE --body "Starting implementation from the repository issue workflow."
   ```

   Build the JSON label set from the issue's current unrelated labels plus the complete target
   Ralph status, factory stage, and factory owner. Do not use add-only or sequential label calls.

4. Make the smallest complete change within the issue scope.
5. If required work is outside the issue, create a linked issue before expanding scope.

Issue work is expected to produce a pull request. After local verification, commit the
implementation, push the branch, and open a ready-for-review PR linked to the issue. Do not
leave completed issue work only in the local working tree. Use a draft PR only when the user
explicitly requests one.

For any Python change, local verification must include the exact CI pair
`ruff check .` **and** `ty check --error-on-warning` against the whole repo
(`bash scripts/check-python-ci-lint.sh`). Path-filtered ruff or ty is not a valid CI substitute.
Ruff-only or pytest-only is not enough: CI fails the required
type-check job on ty warnings, including `redundant-cast`.
Do not recast a value after `isinstance` has already narrowed it.

## Keep status synchronized

Use these labels:

| State | Label |
| --- | --- |
| Ready to implement | `ralph-status:pending` |
| Actively editing | `ralph-status:in-progress` |
| Code complete and checks running/passed | `ralph-status:in-review` |
| Cannot proceed | `ralph-status:blocked` |
| Completed | `ralph-status:done` |

When blocked, leave the issue open, replace the active label with `ralph-status:blocked`, and comment with the exact blocker and required decision. Do not silently move to another issue.

When implementation is complete, run all required checks locally. Then move to review:

```bash
gh issue edit ISSUE --remove-label "ralph-status:in-progress" --add-label "ralph-status:in-review"
gh issue comment ISSUE --body-file /path/to/verification-comment.md
```

The verification comment must include changed files, acceptance criteria evidence, commands and results, and follow-up issue numbers.

After the PR merges and acceptance criteria and issue closure are verified:

```bash
gh api --method PUT repos/JoshCLWren/comic-pile/issues/ISSUE/labels \
  --input /path/to/complete-done-label-set.json
gh issue close ISSUE --reason completed
```

If claimed work remains unfinished when releasing ownership, switching work, reaching a runtime
limit, or ending the turn, create or update one `<!-- factory-resume:v1 -->` comment in place. Keep
it compact: current head, hypothesis, files touched, passed/failed checks, next narrow verification,
remaining action, worker ID, and UTC timestamp. A takeover worker must verify the recorded head and
discard or update stale claims before acting.

## Issue hierarchy

Use GitHub issue links for hierarchy:

- Epic: broad outcome; never selected as the next coding task.
- Task: independently executable implementation unit.
- Subtask: narrow prerequisite linked to its parent task.

Put `Part of #NUMBER` and `Depends on #NUMBER` in issue bodies. Add `epic`, `ralph-task`, or `ralph-subtask` labels so the selector can distinguish planning work from executable work.

## Rules for cheaper agents

- Do not use the archived `archive/historical/ISSUE_KANBAN.md` markdown kanban as status authority.
- Do not dump issue comment history into context.
- Do not begin work on an issue without updating its status label.
- Do not close an issue without a verification comment.
- Do not skip tests or use CI as a debugger.
- Do not push Python changes after only path-filtered `ruff` or pytest. Run `ruff check .` and `ty check --error-on-warning`; ty warnings fail CI. Path-filtered ruff or ty is not a valid CI substitute.
- If the issue is too large for one run, split it into linked GitHub tasks/subtasks before coding.
