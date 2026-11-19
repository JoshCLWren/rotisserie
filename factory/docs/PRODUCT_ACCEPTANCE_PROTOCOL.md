# Product Acceptance Protocol

Version: 4

This document defines the mandatory product-acceptance stage for PRDs and epics in Comic Pile. It is the canonical source for distinguishing implementation completion from parent-level product acceptance.

## Merge-automation guard (issue #1620)

Merge automation must never close an acceptance parent. `factory_post_merge_closure.py` refuses closure for any acceptance parent and reports one of:

- `acceptance-parent-incomplete` — a declared child is still open, or one of
  the parent's own acceptance-criterion checkboxes is still unchecked (even
  when every child is closed);
- `acceptance-parent-requires-verdict` — children and criteria are
  complete. The parent still must not be closed by automation: the
  transition to completed is owned by the interactive acceptance
  run, and even a durable `<!-- product-acceptance:v1 -->` report
  with an ACCEPTED verdict never authorizes merge automation to
  close the parent on its own.

A third outcome, `manual-only-issue`, covers issues that opt out of autonomous
execution with `<!-- factory-execution:manual-only -->`, including operator
production-acceptance and cutover parents such as #2129. Those transitions are
human-controlled by contract, so a merged delivery PR never closes them.

The transition helper `acceptance_parent_may_close` returns true only when
every declared child is closed, every parent criterion is checked, and the
latest acceptance report verdict is ACCEPTED. Child-count completion, CI
success, code coverage, semantic review, and duplicate factory PRs that
re-close already-delivered child work never satisfy it.

An acceptance parent is any issue labeled `epic` or `prd` (even with no child
graph), or any issue whose body both **declares itself a parent** and
**declares a checkbox child graph** — the #1615 incident shape, where the parent
carried only an `enhancement` label and the guard missed it. Parent declaration
means `acceptance parent` or `parent acceptance criteria` language; the child
graph means at least one `- [x] #NNN` reference, the same shape the child gate
reads. Both halves are required, and the guard never infers a parent from a bare
mention of production acceptance.

An executable implementation issue may legitimately discuss acceptance — for
example, a user-reported bug whose body has a "Production acceptance split"
section naming the issue that owns the operator verification pass (#3037), a
harness issue that hands its go/no-go to another issue (#2718), or a narrow
child that says a parent "can use this path for production acceptance" (#2128).
Those issues are ordinary implementation work: they stay in factory intake and
intake/claim fences (`factory_work_policy.py`, `scripts/next_task.py`) and merge
closure still closes them when their delivery PR merges. Over-broad detection
here is a delivery defect, not a safe default.

Every parent criterion checkbox must also be checked before acceptance. Only a
leading `#NNN` child declaration defers to the child open/closed gate; a
criterion that merely quotes an issue reference is still an unmet criterion.

## Problem

Several large issues and epics have reached `completed` because their child issues or implementation PRs closed, even though parent-level user workflows were not fully executable on current `main`. Child closure is evidence of progress. It is not sufficient evidence that a product epic's acceptance scenarios work.

## Scope

This protocol applies to:

- Issues labeled `epic` or `prd` that define parent-level acceptance criteria.
- Parent issues whose completion depends on a set of child implementation issues.
- Any issue where the acceptance criteria describe a user workflow that spans multiple components, features, or child issues.

This protocol does not apply to ordinary implementation issues that have self-contained acceptance criteria.

## Distinction: Implementation completion vs. product acceptance

- **Implementation completion**: A child issue's PR has merged, the child is closed, and its own acceptance criteria are satisfied. This is necessary but not sufficient for parent completion.
- **Product acceptance**: The parent's own acceptance criteria are verified against integrated current `main` (or the exact release candidate state), not isolated child branches. This is required before a parent PRD or epic can be marked complete.

Integrated parent product acceptance is human/interactive controlled. Autonomous factory workers may implement narrow children and produce evidence, but they do not select a parent PRD/epic as ordinary factory work and do not issue the deciding acceptance verdict.

## Requirements

### For parent PRD/epic issues

1. Parent completion must require an explicit acceptance result against the parent's own acceptance criteria, not merely all children closed.
2. Acceptance must run against integrated current `main` or the exact release candidate state, not isolated child branches.
3. Where the parent describes UI workflows, acceptance must include focused Chromium/E2E coverage or an equivalent reproducible browser verification.
4. Acceptance output must identify each parent criterion as pass/fail/not-applicable with evidence.
5. A failed criterion must reopen/retain the parent and create or reference an executable follow-up issue rather than silently accepting partial implementation.
6. Generated or factory metadata must not automatically close a parent solely because GitHub sub-issue completion reaches 100%.
7. Duplicate factory PRs that attempt to close already-delivered child work must not satisfy product acceptance.
8. The final integrated acceptance verdict and parent closure are performed by Josh or an interactive session acting on Josh's direct instruction.

### For factory workers

1. Do not select issues labeled `epic` or `prd` as ordinary autonomous factory work.
2. Do not select an issue whose body contains `<!-- factory-execution:manual-only -->`.
3. Implement only explicitly scoped child issues. When a child is authorized inside a domain under an architecture hold, treat its written contract as frozen: do not redefine the architecture, broaden the scope, lift the hold, or close the parent.
4. Produce focused tests, browser evidence, API evidence, or other durable child-level evidence when useful for later human/interactive acceptance.
5. Do not post the final `<!-- product-acceptance:v1 -->` verdict as the deciding authority, set the parent `ralph-status:done`, or close the parent based on autonomous judgment.

### For issue selectors

1. Issues labeled `epic` or `prd` are excluded from ordinary autonomous factory selection.
2. Issues containing `<!-- factory-execution:manual-only -->` are excluded from autonomous selection regardless of ordinary labels.
3. Narrow child implementation issues remain eligible when otherwise executable, including frozen corrective children created under an architecture hold.
4. Parent acceptance is not a separate autonomous-factory work type. It is human/interactive controlled.

## Acceptance workflow

The following workflow is executed by Josh or an interactive session acting on Josh's direct instruction. Autonomous factories may provide supporting evidence from their completed child work, but they do not own the final verdict.

### Step 1: Pre-acceptance check

Before running acceptance, verify:

- All child issues of the parent are closed.
- No open child issues remain with `ralph-status:pending`, `ralph-status:in-progress`, or `ralph-status:blocked`.
- The parent issue itself is not already labeled `ralph-status:done`.

### Step 2: Acceptance execution

For each criterion in the parent issue's acceptance criteria:

1. Determine whether the criterion is pass, fail, or not-applicable.
2. For UI/workflow criteria: run focused Chromium/E2E coverage against current `main` or provide equivalent reproducible evidence.
3. For backend criteria: run focused API tests or backend validation against current `main`.
4. Record the result with supporting evidence (test output, screenshots, API responses).

### Step 3: Acceptance report

Post a durable GitHub comment on the parent issue containing:

- The parent issue number and title.
- The commit SHA or branch tested against.
- Each acceptance criterion with its pass/fail/not-applicable result.
- Evidence references (test runs, CI links, screenshots).
- Any follow-up issues created for failed criteria.
- Overall verdict: acceptance passed or acceptance failed.

Use this comment structure:

```text
<!-- product-acceptance:v1 -->
## Product acceptance report
Parent: #<number> — <title>
Tested against: <SHA or branch>
Date: <UTC timestamp>

### Criteria results
1. <criterion text> — PASS/FAIL/N/A
   Evidence: <brief reference>
2. ...

### Follow-up issues
- #<number>: <description of gap>

### Verdict
 ACCEPTED / NOT ACCEPTED
```

### Step 4: Closure or retention

- If all criteria pass: label the parent `ralph-status:done` and close it with the acceptance report as the closing comment.
- If any criterion fails: keep the parent open. The failed criteria become executable follow-up issues (or are linked to existing issues). The parent retains its current status labels.

## Regression targets

Use these completed initiatives as fixtures and examples for the acceptance policy:

- #836 continuity planning PRD
- #853 visual continuity planning epic
- #927 checkpoints/convergence child
- #1016 external knowledge epic
- #1023 external-template adoption

The policy should have caught the missing planner checkpoint/convergence UI and the missing external-template browse/reconcile/adopt UI before those epics were marked complete.

## Anti-patterns

- Closing a parent because all children are closed without running acceptance.
- Accepting a parent based on CI success, code coverage, or child-count completion alone.
- Running acceptance against an isolated child branch instead of integrated `main`.
- Creating acceptance-only documentation PRs that do not verify the actual workflow.
- Treating acceptance as optional when the parent defines UI workflows.
- Allowing an autonomous factory to self-certify and close the parent whose children it implemented.
