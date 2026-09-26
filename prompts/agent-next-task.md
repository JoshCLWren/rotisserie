# Agent Next-Task Prompt

Work on the next task in this repository.

## Before editing

1. Read `AGENTS.md`.
2. Read `docs/ISSUE_EXECUTION_PROTOCOL.md`.
3. Read `docs/AUTONOMOUS_FACTORY_POLICY.md` when factory work is involved.
4. Inspect `git status` and preserve all existing user changes.
5. Run `make next-task`.
6. Read the selected GitHub issue and any linked local plan.
7. If the issue is too large for one implementation pass, split it into linked GitHub task/subtask issues before editing code.

## While working

- Move the selected issue from `ralph-status:pending` to `ralph-status:in-progress`.
- For factory work, atomically reconcile the complete label set to `factory`, `factory:building`,
  and exactly one owner label; never transition labels with sequential remove/add calls.
- Implement only that issue's scope.
- Add regression tests for changed behavior.
- Fix failures; never skip tests or use CI as a debugger.
- For any Python change, run `ruff check .` and `ty check --error-on-warning`
  against the whole repo (`bash scripts/check-python-ci-lint.sh`) after the last
  edit and before every push. Path-filtered ruff or ty is not a valid CI substitute.
- If required work is outside the issue, create a linked GitHub issue before expanding scope.
- If blocked, mark the issue `ralph-status:blocked`, explain the exact blocker, and stop.
- Do not use the archived Markdown kanban as a status source.
- Do not discard, reset, or overwrite unrelated working-tree changes.
- Before ending with claimed work unfinished, update the canonical `<!-- factory-resume:v1 -->`
  comment with the current head, hypothesis, files touched, checks, next narrow verification,
  remaining action, worker ID, and UTC timestamp.

## Before finishing

- Run all required local checks from `AGENTS.md` and the selected issue.
- Move the issue to `ralph-status:in-review`.
- Add a GitHub comment listing changed files, acceptance-criteria evidence, test commands, and results.
- Commit the implementation with a descriptive message, push the branch, and open a ready-for-review pull request linked to the issue. This is the default completion workflow for every selected issue; do not leave verified work only in the local working tree.
- Leave the issue in review while its PR is open. Mark it `ralph-status:done` and close it only
  after the PR merges and acceptance criteria and issue closure are verified. If the user
  explicitly asks for a draft PR, create a draft instead.

Begin by running `make next-task`.
