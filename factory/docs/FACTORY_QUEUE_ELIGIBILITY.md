# Factory queue eligibility

Canonical reference for executable-eligibility and lease reconciliation across
the ComicPile work-selection paths (issue #3039).

## Authoritative roles

| Selector | File | Serves | Owns fixed-model dispatch? |
|---|---|---|---|
| Fixed-model controller | `.github/scripts/factory-work-controller.py` (+ `factory_work_policy.py`) | Fixed-model factory workers (`factory:6`–`factory:79`) | **Yes — sole owner** |
| Ralph selector | `scripts/next_task.py` | Local/Ralph agents working the pending queue | No (advisory for its own queue) |
| Rotisserie adopter | `.github/scripts/factory_rotisserie_adopter.py` | Read-only host-translation shadow of the same rules | No (never assigns work) |

The controller's dispatcher (`assign`, `reconcile`, `release`,
`conflict-recovery`, `stale`, `explain`) is the only path that may create a
fixed-model lease. `scripts/next_task.py` serves the separate local/Ralph
queue; it does not create fixed-model leases or query canonical PRs, Rotisserie
or the remote retry history. Both selectors share the *common* static rules,
but their final `eligible` fields answer different questions. The Rotisserie
adopter shares the common static rules but never assigns work. When the same
static inputs disagree, fix the shared module rather than duplicating rules.

For an **automated fixed-model assignment**, follow the controller verdict.
For a **local issue implementation**, use Ralph selection followed by the
local-factory claim procedure in
`.agents/skills/local-factory-next-task/SKILL.md`: verify live ownership,
open canonical PRs, dependencies and issue stage, then run `make start-task`
and atomically set `factory:local`. A remote intake failure does not by itself
prohibit an otherwise valid local claim.

## Shared semantics

The pure rules live in `scripts/factory_eligibility.py`. All three consumers
import that module; `factory_work_policy` re-exports only `is_manual_only`,
`is_acceptance_parent`, and `parse_declared_dependencies` for callers that
already hold the policy module. Agreement is pinned by
`tests/test_factory_eligibility.py`
(`test_selectors_agree_with_shared_semantics`) and
`tests/test_factory_rotisserie_adopter.py`
(`test_human_gate_uses_the_structured_manual_only_contract`).

Static exclusions, in evaluation order:

1. Not open.
2. Non-executable product numbers (registry, watchdog, protected set).
3. `epic` / `prd` labels, or a body-declared product-acceptance parent
   (acceptance language **plus** a checkbox child graph; either half alone
   stays executable).
4. The structured manual-only directive (see below).
5. Blocked labels (`factory:blocked`, `ralph-status:blocked`, `wontfix`,
   `invalid`, `duplicate`).
6. Terminal labels (`ralph-status:done`, `factory:ready`).
7. An active factory lease (`factory:<worker>` / `factory:local`).
8. Unresolved explicit dependencies (declared target still open).
9. An open canonical PR already owning the issue (controller only).
10. No-diff retry budget exhausted (fixed-model controller's rolling window;
    not a Ralph selector input).

An issue that passes the common static checks may still fail either selector's
additional gates. `factory:unowned` means no active factory owner, not that
all assignment gates have passed.

## Rust cutover scheduling dependency (active #4335)

Until the Rust API cutover epic #4335 is **closed**, unrelated work must wait,
including tickets created after the original backlog sweep and PRs already in
review or repair. The shared eligibility rule only admits issues explicitly
labeled `rust` or `main-breakage`. A real `Depends on #4335` declaration
always blocks, even on a mislabeled Rust issue.

The rule is enforced by the fixed-model issue and PR selectors, local/Ralph
issue selection and explicit start, and the live exact-head merge gate. A PR
with a linked issue inherits its eligibility, including a conflicting explicit
`Closes #N` claim. Unlinked factory PRs must themselves carry the `rust`
label while the epic remains open. This avoids consuming review workers on
deferred work without mutating PR stage or lease labels. After #4335 closes,
normal eligibility and issue dependencies resume automatically. The
`main-breakage` exception protects the production/CI path needed to finish
the cutover, not ordinary feature bugs.

Dynamic gates applied on top by the controller: worker WIP caps, review
backlog backpressure, Rotisserie intake authorization, live GitHub dependency
blockers, and lease liveness. `explain` reports every gate it can observe
except lease liveness and the per-worker strike-retry exclusion, which are
evaluated at assignment time for a specific worker. Under the expanded
Rotisserie issue-intake configuration, a missing local `rotisserie` executable
makes the *controller's* intake fail closed; it does **not** mean a local
Ralph agent must install Rotisserie before claiming its own work.
`reconcile` repairs contradictory labels and releases provably stale leases
automatically; no hand-authored release comment is ever required. Local
leases expire after 60 minutes of GitHub inactivity even when trusted lease
markers are absent: the controller falls back to the issue/PR `updatedAt`,
with a final live ownership/update check before releasing. Fixed-model
leases without active runs use the same fallback with their 15-minute TTL.
The creation date of a long-standing issue is never a substitute for its
claim/update timestamp.

## Manual-only directive contract

Autonomous execution is excluded by either of these standalone lines, outside
fenced code blocks and inline code spans:

```html
<!-- factory-execution:manual-only -->
```

```text
manual-only
```

Raw substring matching is not the contract. Merely discussing the marker in
prose, or quoting it in code (backticks, fenced blocks), never excludes an
issue — that brittleness caused the Factory-41 misread on #3039, where
release-note prose mentioning the marker was mistaken for the directive. To
mention the marker without triggering exclusion, wrap it in code spans or
place it inside a sentence.

One deliberate exception: `factory_post_merge_closure.py` keeps the broader
substring guard. That path decides whether merge automation may *close* an
issue, so it stays maximally conservative and never becomes more permissive
than the execution contract. Do not tighten it to the structured rule without
a separate decision.

## `ralph-task` / `ralph-status:pending` scope

These two labels are **Ralph-selector metadata, not universal execution
metadata**. `scripts/next_task.py` requires them because it serves the local
Ralph queue. The fixed-model controller never requires them and selects on
factory labels instead. An issue without `ralph-task` can be perfectly
eligible for fixed-model dispatch while being invisible to `next_task`, and
that is by design.

## Comment trust

Marker trust is not an eligibility rule. `factory_work_policy.comment_is_trusted`
is the single gate used by every lease-activity, no-diff, and strike-reset scan:
it accepts a trusted `OWNER`/`MEMBER`/`COLLABORATOR` association **or** a
workflow-posted comment (`performed_via_github_app.slug == github-actions`),
matching [`FACTORY_GITHUB_VISIBILITY.md`](FACTORY_GITHUB_VISIBILITY.md). Nothing
in `scripts/factory_eligibility.py` reads comment provenance; eligibility reads
issue state, labels, and body only.

## Retry-history reads during dispatch

Each serialized dispatcher assignment step creates a new runner-local retry-history
snapshot. The first assignment reads every page in the existing rolling window;
later assignments fully paginate updates since the preceding query **started**, with
a one-second overlap, and merge changed comment bodies by GitHub comment ID. The
same trusted-marker parser and retry limits consume the refreshed rows. Failed
reads never return stale cached evidence. Other callers keep their ordinary full
reads, and the next dispatch pass starts with a new snapshot.

Ownership, leases, dependencies, contributor exclusions and exact-head reads are
not cached. A deleted comment can remain conservatively counted until the next
pass; an edited comment is refreshed in the current pass. Snapshots are also
discarded after an hour or when their repository/window metadata does not match.
The `attempt-history` log records `source`, `pages_read`, `comments` and
`full_history_pages`, so real dispatches can demonstrate reduced reads without
changing quota reserves or inferring savings from elapsed time.

## Queue diagnostics

To ask why a specific issue is or is not eligible:

```bash
# Authoritative controller view (static verdict + live dynamic gates):
python3 .github/scripts/factory-work-controller.py explain --issue 3039
python3 .github/scripts/factory-work-controller.py explain --issue 3039 --json

# Local/Ralph queue view (shared static rules + Ralph metadata only):
python3 scripts/next_task.py explain 3039
```

Both commands are read-only and use `explain_issue_eligibility` for the inputs
they share. **Their top-level `eligible` values are not interchangeable**:
the controller also checks open canonical PRs, Rotisserie authorization,
remote no-diff retry history, and live dependency blockers; worker capacities
and some lease/strike checks apply when it actually assigns work. The Ralph
report lists these omitted checks under `not_evaluated`. In particular,
`Ralph eligible: true` alongside `controller eligible: false` does not
automatically make the issue globally claimable or forbid a local session:
perform the live local contention checks and follow the local claim workflow.

Current owner labels and live issue/PR state determine active ownership.
Historical claim/release comments and resume packets are append-only audit
and handoff context, **not independent leases**. A previous claim with no
matching release comment cannot override `factory:unowned` by itself.
If actual labels contradict each other or a lease appears stale, run the
controller's `reconcile` instead of hand-authoring release comments; never
overwrite a foreign live owner.
