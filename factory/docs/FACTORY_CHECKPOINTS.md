# Deterministic persisted factory work

Owner correction (2026-11-15, #5183): preserved code is the actual resumable
implementation PR, not a separate recovery artifact. The trusted wrapper opens
an ordinary **non-draft PR against main**, using the issue's real title, one
intentional `Closes #issue` link, acceptance and continuation context, and the
parsed `Worker: opencode-free-model-factory-N` producer line. Copied issue
context cannot introduce extra closure directives. Existing custom PR bodies
and titles are preserved.

The trusted wrapper stages the writer, handoff helper and review controller
(with its dependencies) from `main` before loading model-editable PR code. It
uses a private Git index, commits and pushes tracked and non-ignored new files
every 45 seconds, and flushes synchronously on finalization or handled signals.
Publication retries even without another edit. Git and PR publication are
wrapper behavior, independent of model cooperation. The model's index and HEAD
are untouched during execution.

New issue branches identify original issue, model and run:
`factory/work-checkpoints/issue-4853/kilo-auto-free/43724877464`.
Full model/runtime identity, original issue, source branch and initial base are
commit trailers. Legacy run-only refs and old checkpoint messages remain
readable. Origin discovery walks at most 200 ancestors and fails closed; normal
commits from a subsequent agent do not make the branch disposable.

For an assigned PR repair, the live writer fast-forwards the existing PR branch
and the per-attempt tracking ref **atomically**. Ownership and the canonical
source run are checked before writes; no force push or competing side PR is
used. Legacy unintegrated saved refs remain linked from the original PR for
explicit reconciliation without overwriting a newer owner's work.

Finalization adopts persisted ancestry before overlap detection, retaining the
initial base for full-attempt validation. It does not reapply an already-saved
patch or create another implementation PR. Focused checks and ordinary commits
continue on this same branch. Failed checks retain code and remain failures;
publishing saved code is never evidence of validated delivery or approval.
Normal product CI, independent review and merge gates remain authoritative.

## Ownership and continuation

The actual numbered factory from the canonical source run owns the work PR
while its source assignment remains active. Exact contributed heads are recorded
through the trusted review controller. When execution ends, incomplete work is
released to `factory:changes-requested` + `factory:unowned`, with the linked
issue unowned too. Successfully checked work hands off at `factory:review`.
Any eligible factory or interactive agent can claim, check and complete the same
PR/branch. There is no durable model affinity.

`factory-checkpoint-reconcile.yml` runs from trusted main on source completion,
hourly and on manual run selection. It reads GitHub objects without executing
saved source. It repairs publication after hard cancellation, preserving live
leases and resumed owners/stages. An unchanged old run cannot reset later work.
No system can preserve edits after the last snapshot if its runner is destroyed.

## Retention and outcome evidence

Reporting distinguishes saved code, published work PR and exact-head validated
delivery. A failed/canceled worker remains failed even when its code is saved.
There is no age-based deletion. Open canonical PR branches are never deleted as
disposable snapshots. Tracking refs can retire only after the source completes,
no live owner/hold remains and every changed path's bytes and mode (including
deletions) are proven incorporated. Closed or unintegrated work is retained;
ordinary subsequent commits and moved refs are preserved.
