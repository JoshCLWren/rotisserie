# ComicPile Autonomous Factory Policy

Version: 28

This is the canonical policy for every scheduled ChatGPT worker, the local OpenCode factory, fixed-model external factories, and interactive factory repair sessions.

## Prime directive

**Drive the open issue backlog to zero by delivering complete, safe implementations instead of orbiting a few pull requests.**

Success is measured by issues truthfully closed and production defects removed. Pull requests, commits, comments, reviews, labels, and hours spent are intermediate activity, not outcomes.

For original owner-requested engineering objectives, follow [`docs/OBJECTIVE_FIRST_ENGINEERING_CONTRACT.md`](OBJECTIVE_FIRST_ENGINEERING_CONTRACT.md): do not redefine the finish line through a self-authored issue, partial PR, shadow-only phase, or a ready-to-merge milestone. A runnable objective is done only after merged, deployed, and end-to-end verified on the real intended path; issue-level closure may be narrower than the owner's objective.

## Owner-directed Rust cutover backlog freeze (2026-11-15)

**2026-11-20 owner scheduling update:** The exclusive Rust-first issue-intake gate is retired. Independently executable product issues are selectable even while #4335 remains open for its separate production acceptance and cleanup. Explicit `Depends on #4335` declarations, ordinary dependencies, `manual-only` directives, blocked labels, and live claims still gate their respective work. Do not claim or close the epic by inference, and never reintroduce Python/v1 runtime behavior. The following **new issue-creation restriction remains** until the owner separately lifts it:

Scheduled factories, local coding agents, reviewers and automated controllers **must not open new GitHub issues or sub-issues, split existing issues, or convert CI/test findings into new backlog tickets without an explicit owner instruction for that exact creation**. Continue repairing problems within the approved issue/PR/objective when safe. When genuine out-of-scope work is encountered, attach compact evidence to the existing canonical issue or PR comment and leave ownership and acceptance truthful; do not manufacture a second executable parent or a swarm of children. Keep existing issues and PRs moving and close completed work normally. An owner-directed interactive change may explicitly authorize a bounded split with a parent checklist and a clearly designated execution owner. This freeze does not change the existing single-incident production deployment recovery mechanism; main CI failures must remain check/run failures, never new main-breakage issues.

## Continuous delivery cycle

The delivery factory follows this permanent cycle:

1. drain every executable open product issue using the shared priority policy;
2. treat factory ownership as a temporary next-action lease, never durable model affinity;
3. after each implementation attempt, push durable progress when useful and release the lease when active work stops;
4. return immediately to selection while executable work remains and the session has safe runtime budget;
5. if the shared executable pool is genuinely empty, release any active lease, record a truthful no-work completion, and end that bounded session cleanly;
6. remain scheduled and check the shared pool again on the next normal heartbeat.

An empty or blocked ordinary backlog is never a reason to pause, disable, suspend, or mutate a factory schedule. Only Josh, or an interactive session acting on Josh's direct instruction, may pause or disable a factory. Scheduled workers remain enabled and continue checking on schedule.

User-reported product bugs are the first delivery queue. Reproducible E2E-discovered product bugs come after them, then ordinary executable product issues. Preserve `user-reported` only for defects actually reported by a user.

Firefox and WebKit are optional diagnostics for browser-specific investigations. They are not required factory release coverage and must not delay issue closure, merges, or backlog draining.

## Selection priority

Issues labeled `epic` or `prd` are not ordinary autonomous factory candidates. An issue whose body contains `<!-- factory-execution:manual-only -->` is also excluded from autonomous selection even if its other labels would normally make it executable. Use the marker for coordination, architecture, integrated acceptance, destructive-authorization, and similar human/interactive gates. Narrow corrective child issues remain executable when they have an explicit frozen contract, including children inside a domain under an architecture hold. A factory implements that frozen contract exactly, does not reinterpret the governing architecture, and does not lift or close the hold.

Choose work in this order:

1. The highest-priority unclaimed open issue labeled both `user-reported` and `bug`; within equal priority, choose the newest report first.
2. A branch-caused failing check, merge conflict, or actionable review defect only when the affected PR directly delivers an equal-or-higher-priority user-reported/product bug or clearing the blocker can immediately finish or merge that product fix.
3. Other branch-caused failing checks, conflicts, or actionable review defects that prevent substantive product-delivery PRs from becoming mergeable.
4. The highest-priority unclaimed reproducible E2E-discovered product `bug` issue.
5. The highest-value unclaimed executable product issue, honoring explicit priority and dependencies.
6. Additional work on an existing PR only when required to complete its issue contract or make the PR mergeable.
7. Factory, CI, test, or E2E infrastructure only when it directly blocks product delivery or exists as a focused executable infrastructure issue.

Test-only defects, stale selectors, optional validation, E2E plumbing, docs, release-note work, CI cosmetics, metadata cleanup, and evidence polishing never outrank an executable user-reported or product bug unless that infrastructure directly blocks safe validation or merge of the same higher-priority product fix.

A green, ready, review-passed, or merge-gated PR is excluded from ordinary work selection. It may be selected only for one final exact-head gate check and merge action.

Optional tests, cleanup, metadata edits, wording changes, evidence polishing, PR-body edits, architectural debate, or another minor slice do not outrank a coherent implementation for an unclaimed executable product issue.

## Jev work selection

The production dispatcher may use the dedicated Jev decision model for **one
bounded selection per dispatch pass**. The owner objective (#4428) is issue
selection: when at least two interchangeable ordinary product issues are
actually leasable, Jev ranks them and the controller may start the chosen issue
instead of the canonical first one.

Factory backpressure can legally leave zero leasable issues: while PR WIP or the
review backlog is saturated, `build_candidates` excludes new issue work so the
fleet drains existing PRs. In that case the same single pass ranks an existing
**PR completion action** instead, so a decision is still meaningful. The report
records `pr_wip`, `review_backlog`, the number of ordinary issues that are
leasable now, and the number waiting behind backpressure, so an issue surface
that is empty for policy reasons is explainable rather than silent.

Deterministic policy remains authoritative and owns every hard constraint:

- Issue ranking is limited to the same lane and priority class and never crosses
  protected work: user-reported bugs, main-breakage, manual-only/epic/PRD,
  blocked, dependency-blocked, infrastructure, and owned issues keep their
  canonical positions. Worker-specific exclusions and leases still apply after
  ranking.
- Completion action types are **derived** from the existing stage machine, never
  inferred by the model: `merge-conflict`, `ci-repair`, `review-findings`,
  `review`. `ci-repair` is offered only when trusted check metadata shows an
  actual failure; pending CI is not an executable action.
- Only ordinary lane-3 PR actions are promotable. Main-breakage, user-reported,
  `factory:ready`, blocked, draft, and owned work never move. Promotion is
  confined to the contiguous run of ordinary completion candidates containing
  the chosen action, so a decision can never jump protected or issue positions.
- WIP and review-backlog limits continue to gate *new issue/PR creation*; they do
  not authorize starting work behind the gate, and they do not prevent ranking
  existing completion work.
- Leases, review independence, dependencies, exact-head CI, semantic review, and
  provider/spend limits are unchanged.
- The model sees only trusted normalized metadata (stage, priority, merge state,
  age, linked issue, exact-head reference) for completion actions. Issue
  selection sends a bounded, truncated issue title, description, and labels,
  framed as untrusted data the model must never follow as instructions; PR
  bodies, comments, diffs, and logs are never sent on either surface.
- One bounded request per dispatch pass. Missing key, provider failure,
  low probability margin, invalid response, or a stale/missing exact-head
  reference fall back to deterministic order without blocking dispatch.
- For completion actions, the chosen action and exact head are validated against
  an authoritative PR read immediately before the lease is written. A changed
  head or action discards the recommendation and reselects from canonical order.
- The deterministic baseline is recorded alongside the chosen item, and the
  controller logs whether the actual lease differed from the deterministic pick
  after all filters. Jev selection is provenance on the lease
  (`source=jev`, plus `action=<type>` and `head=<sha>` for completion work); the
  worker independently validates the action against its live stage and
  checked-out head before including it in the coding prompt.


## Work-session utilization

A scheduled heartbeat is a bounded work session, not a one-ticket punch.

After every substantive fix, opened PR, merge, completed issue, or valid blocker, immediately rerun selection and continue the next highest-priority executable work in the same scheduled run. Reaching one valid heartbeat outcome is a minimum success threshold while executable work exists, not a stop condition.

Do not end a run merely because CI or review is pending, because the current item became blocked, or because one issue reached a handoff state. Preserve or release ownership as appropriate, then select the next executable item. Continue until the runtime or tool budget makes further safe substantive work impossible or the shared executable pool is genuinely empty.

When no executable work remains, ending the current session cleanly is valid. The worker stays enabled for its next scheduled heartbeat.

## Concurrency and throughput floor

At most one implementation worker may own an issue unless workers explicitly declare non-overlapping file ownership.

Once one worker has a valid lease on the highest-priority issue, peers select the next eligible issue. Do not let one broad issue, one user-reported bug, or one PR consume the whole factory.

When fewer than four substantive implementation PRs are open and executable unclaimed issues exist, idle workers must prefer opening coherent implementations for separate issues over embellishing existing PRs.

OmniRoute's shared free coding pool cannot absorb a full roster tick. Fixed-model Entry sessions that smoke or run `auto/coding:free` are limited to at most three concurrent in-flight units (active Entry runs plus equivalent fixed-model leases). Dispatcher, completion drain, and capacity refill must refuse new Entry assignments when that cap is exhausted. The four-PR throughput floor remains the selection preference; it does not override the free-pool cap.

A substantive implementation PR changes product behavior, correctness, performance, architecture, deployment behavior, data, or meaningful automated coverage required by its issue. Comments, labels, reviews, PR metadata, help text, and optional test embellishment do not count toward this floor.

## Lease semantics

### Local factory delivery roles

Local orchestration owns the authorized objective through implementation,
targeted local verification, exact-head CI repair, actual independent review,
gated merge, authorized deployment and real production acceptance. A PR or
scheduled-review handoff alone is not ordinary completion. When an eligible
local independent reviewer can run, start it rather than waiting for scheduled
pickup; use [the executable ACP review path](LOCAL_FACTORY_REVIEW.md).

Implementation and CI-repair workers are contributors and cannot independently
approve their resulting head. Record actual reviewer identity, confirmed ACP
session/model and exact head; never fabricate controller markers or approvals.
Manually tag actual active local review with the canonical `factory:review` +
`factory:local` lease, preserving other live ownership and unrelated labels via
atomic replacement, then release when the session ends. Read only ACP checkpoints,
model-selection metadata and final reports/verdicts, never full logs/transcripts.
The coordinator checks all existing merge and production authorization gates;
this workflow grants no additional authority. Remote PR/backlog intake thresholds
do not apply to local sessions. Detailed Codex routing lives in
`.agents/skills/crouton-factory-codex/SKILL.md`.

Factory ownership is a connection-pool lock around the next action, not a permanent assignment to the model or worker that first touched the issue or PR.

- Exactly one active owner label may hold an issue or PR at a time.
- **Inactivity is bounded.** A local `factory:local` lease expires after 60
  minutes without GitHub-recorded target activity; a numbered fixed-model lease
  uses a 15-minute TTL when there is no active workflow run. The controller
  considers trusted lease markers and the target's `updatedAt` timestamp,
  so missing or untrusted marker comments cannot keep an otherwise inactive
  claim alive forever. Do not use the issue's `createdAt` as a lease clock,
  because long-standing issues can be freshly claimed. Unreadable/malformed
  GitHub update metadata cannot prove inactivity and must not cause a guess.
  An issue and its canonical PR sharing the same owner count as one active
  lease: GitHub progress on either target keeps both sides alive. This matters
  for local PR repairs such as #4615 / #4709. A live worker must persist a
  meaningful progress update before its lease window expires, not just keep
  working in a private checkout.
- Expiration is a **lease release**, not a decision that the underlying task
  is complete: blocked issues remain blocked; an in-review PR keeps its
  next-action stage. Never overwrite a freshly changed owner during cleanup.
- Cross-worker takeover and merge are allowed after the prior lease is released.
- A factory that creates or advances a PR releases active ownership when its implementation attempt ends, including while CI or review is pending.
- Provider failure, timeout, no useful persisted change, or another stable handoff state releases the lease so another worker can try later. Review independence is based on factory identity and exact-head provenance, not on requiring a different model; OmniRoute may route both workers through the same upstream model.
- Review independence excludes every factory worker that authored the exact head under review, not only the worker that opened the PR. Factory pushes share one Git identity, so contributor identity comes from the controller-authored `comic-pile-factory-head-contributor-v1` record written at every push or repair, unioned with the declared branch/body producer and with every other record on the same pull request, because repairs stack onto the previous head instead of rewriting it. A head is authorized by one independent reviewer only when a record exists for that exact head; when one does not, it fails closed to two distinct eligible reviewers instead of assuming the apparent reviewer is independent. Queue selection excludes recorded contributors from semantic-review assignment so a head reaches another eligible reviewer on its own; refusing a contributor's verdict is a backstop, not the routing mechanism.
- A controller claim is not durable until the worker reaches an executable session. Smoke, executor-selection, credential, and other pre-session failures release the owner to `factory:unowned` immediately, preserve the truthful stage, and post `comic-pile-factory-claim-released-v3`. Do not wait for the 900s stale-lease TTL.
  The reusable runner records when the worker session starts. If it never starts,
  a separate trusted-main cleanup job releases the lease before the per-worker
  concurrency lock ends; started sessions keep their existing wrapper lifecycle.
- Exact-head review assignments use the multi-provider Entry path (`nvidia`, `opencode-free`, `openrouter-free`, `kilo-auto`) while OmniRoute is dark; they must not smoke `auto/reasoning:free` or `auto/coding:free`.
- UPDATE (2026-11-16): Runtime model selection is catalog driven across registered providers, with no fixed price or free-name eligibility gate. Actual execution determines model usability.
- INCIDENT (2026-09-06): OmniRoute can remain disabled by its existing provider-health control. The other registered provider lanes operate independently and select models at runtime.
- A takeover worker continues the current branch/head rather than creating a replacement solely because another model authored the existing commits.
- Waiting on CI or review never reserves a model indefinitely.

## Runtime provider discovery and ephemeral models

Worker/lease capacity is derived from the provider integration registry by
`factory_worker_slots.py`, never from manually approved model IDs. The
dispatcher verifies credential availability, provider capacity, current
ownership and runtime health before assignment. A new published model can
be used without a roster-update PR. Current catalogs give **candidates**;
successful useful execution and a verified PR or review result prove callability.

Never gate on published price, a model name's `:free` suffix, or an old
retirement pin. Follow real authentication failures, throttles, quotas,
compatible-model metadata and bounded Fibonacci backoffs. The roster TSV,
generated snapshot/worker lock and scheduled pin-retirement workflow have
been deleted. Reintroducing them is not an acceptable provider migration.

OpenCode and Kilo use their installed CLI discovery. Compatible HTTP
providers use published `/models` contracts and their credentialed
integration metadata. The exact advertised model identifier is passed
unchanged to the runtime. Runner status and model choice belong in observed
outcomes, not source-controlled provider inventory.

## Anti-loop rules

- Existing open PRs are not automatically higher priority than unclaimed issues.
- A green, ready, review-passed, or merge-gated PR must not consume repeated heartbeats.
- Do not repeatedly claim work whose next required edit is impossible in the current runtime. Preserve the blocker once, release active execution, and select another executable issue.
- Do not create replacement PRs merely because `main` advanced. Replay only when the prior PR is genuinely non-mergeable and substantial implementation would otherwise be lost.
- Waiting for CI, review, a merge, a safer runtime, or external availability is not a global stop condition while other executable work exists.
- Do not debate an already documented actionable finding across repeated heartbeats. Fix it, rebut it once with evidence, or preserve a real blocker and move on.
- Test-only and factory-maintenance work must not become a refuge from higher-priority product bugs.

## Full-contract implementation

Choose PR size according to reviewability, conflict exposure, deployment and rollback boundaries, and total time to the requested working behavior. Both small atomic PRs and larger coherent PRs are acceptable; neither is an objective in itself.

A split is valid when it improves reviewability, reduces conflict/rollback risk, or accelerates end-to-end delivery. Keep the original owner objective tracked and name the still-missing behavior in every partial PR and handoff. Do not substitute a prerequisite, scaffold, or observation-only implementation for the requested active capability.

A partial PR does not automatically outrank a fresh higher-priority issue. Continue the parent issue only when it wins under the selection order. If deferred, leave a durable objective-preserving handoff. Do not introduce shadow modes, disabled-by-default flags, dark launches, or extra approval phases unless Josh explicitly requested them or a pre-existing applicable policy mandates them. Preserve all real security, data-integrity, independent review, CI, claim, and deploy controls.

## Required work loop

Repeat until the selected issue reaches closure or a valid blocker:

`inspect contract -> implement closure-critical behavior -> focused validation -> commit -> push -> inspect exact SHA -> account for all review feedback -> repair blockers -> verify merge gates -> merge when eligible -> verify issue closure`

Python focused validation must include the exact CI pair `ruff check .` and
`ty check --error-on-warning` against the whole repo
(`bash scripts/check-python-ci-lint.sh`). Path-filtered ruff or ty is not a valid CI substitute.
Re-run that pair after every later Python edit and before every Python push.
A lint run from before the last edit does not count.

After work becomes blocked, merge-gated, or dependent on a human-only decision, preserve durable context and return to selection rather than polishing indefinitely.

After any issue or PR reaches a stable outcome, return to selection again and continue the same scheduled work session while runtime remains.

If no ordinary executable issue can be selected, release any stale active lease, record a truthful no-work outcome, and end the bounded session cleanly.

## Release-note ownership

Release notes are asynchronous post-merge infrastructure, not an implementation merge gate. The dedicated release-writer workflow inspects merged pull requests and publishes user-facing changes to the database-backed release ledger. Reconciliation owns missed or retried release records.

Implementation workers must not create, repair, or require `docs/changelog.d` fragments, must not edit `docs/changelog.md` as routine delivery work, and must not block a product PR because release-note publication is delayed. A genuine release-writer or release-ledger defect may become its own executable issue, but fixing that infrastructure must not reintroduce Markdown release-note work into unrelated implementation branches.

`docs/changelog.md` and the existing files under `docs/changelog.d/` are frozen historical source retained only for provenance and auditability. They are not runtime truth, are not assembled by the frontend build, and ordinary new work must not append to them.

## Review-feedback gate

Before posting a pass verdict, ready marker, merge-gated marker, or statement that no blocking correctness issue remains, the worker must inspect the exact current head SHA and:

1. fetch review submissions and all current inline review threads;
2. ignore only clearly non-actionable status noise such as review-rate-limit notices, generic summaries, release notes, or optional finishing-touch advertisements (Greptile's PR-body summary and findings are never noise; see below);
3. classify every actionable finding as fixed, demonstrably outdated because of a specific later code change, or rebutted with concrete technical evidence;
4. respond to or resolve every actionable current thread;
5. refuse pass, readiness, or merge while an unresolved actionable correctness, security, ownership, data-integrity, concurrency, recovery, migration, or test-validity finding remains.

A worker's own review conclusion does not silently override existing human or bot feedback.

### Greptile findings are high priority

Greptile is an optional source of review feedback. Josh disabled it on 2026-10-30;
OSS review credits may be available intermittently. No worker may require a
Greptile review, check, confidence score (including 5/5), or acknowledgement to
advance a PR. Disabled integration, exhausted credits, quota notices, and stale
scores do not block delivery or justify repeatedly requesting reviews. Do not
automatically re-enable it or consume newly available credits. Independent
exact-head factory semantic/acceptance review, contributor-independence rules,
required CI, and the actionable-feedback gate remain mandatory, including for
Rust cutover PRs. Fetch GitHub's existing review data even when Greptile is off;
incomplete GitHub review data still fails closed. Existing actionable findings
must still be fixed or rebutted; disabling the integration does not dismiss them.
A trusted evidence-backed outcome permits independent review to proceed without
waiting for Greptile to return.

Josh directed on 2026-10-18 that Greptile (`greptile-apps[bot]`) findings be held in high regard. They are must-address review feedback, not advisory noise:

- the worker wrapper fetches unresolved review threads, the Greptile review body and PR-body summary, and current-head PR-Agent check output before every PR-mode run, and injects them into the mission with Greptile items first and labeled HIGH PRIORITY;
- every open Greptile finding must be fixed in code on the PR branch, or answered with a concrete, evidence-backed rebuttal (cite file and line, test output, or spec). The agent records each outcome (`fixed` with an explanation, or `rebutted` with evidence) in the git-excluded `.factory-greptile-outcomes.json`, and the trusted wrapper posts those outcomes as the replies on each thread;
- workers never resolve Greptile threads; Greptile resolves its own. A thread is open until Greptile accepts the answer without pushback or a trusted responder (a repository owner, member, or collaborator, or the wrapper as the `github-actions` Bot) has replied after Greptile's latest comment. Worker App logins are never trusted responders;
- the wrapper refuses `FACTORY_GATE_READY` while any open current-head Greptile finding lacks a valid outcome, and re-dispatches the PR when new Greptile findings arrive during the run. A worker or semantic reviewer must not emit a pass verdict while any Greptile finding is neither fixed nor rebutted;
- open Greptile findings, including findings listed only in a Greptile PR-body summary for the current head, route the PR to `factory:changes-requested` (see `docs/FACTORY_GITHUB_VISIBILITY.md`). Greptile remains untrusted for factory markers, ownership, and readiness;
- PR-Agent check output is lower-priority advisory input: fix the real defects it finds, but it does not block on its own.

### Pre-push fast checks

A worker diff is pushed only after the trusted pre-push fast checks pass for the files it touches: `node --check` for `.mjs`/`.cjs`, CI-parity ruff and ty plus targeted pytest for `.py`, and the frontend typecheck plus eslint for `frontend/**/*.ts|tsx|mjs`. Backend Python changes, OpenAPI generator changes, and generated OpenAPI artifact changes also invoke `uv run --frozen python scripts/export_openapi_schema.py --check`. A stale schema or broken app import blocks the factory commit/push before CI. Changes to root or frontend package manifests, pnpm lockfiles, or the pnpm workspace manifest trigger the existing frozen pnpm install and frontend typecheck even if no `.ts` or `.tsx` file changed. On failure the wrapper does not push. It resets the tree and leaves a `comic-pile-factory-fast-checks-failed-v1` comment for the next worker (see `docs/FACTORY_GITHUB_VISIBILITY.md`). Agents should run the same checks themselves before ending a run.

Every push invalidates prior review, readiness, and merge eligibility. Re-fetch the exact SHA, current review threads, mergeability, and CI after every push.

Implementation delivery is a separate mechanical prerequisite to semantic approval and merge.
Implementation issues must change behavioral source, build, or workflow content; docs, tests,
comments, docstrings, and formatting alone do not qualify. An explicit `Issue type: docs-only`
(or another supported non-implementation declaration) permits the declared documentary output.
The controller reconstructs complete old/new files from the immutable head and original PR patch
so changes inside long comments and docstrings cannot borrow executable context. Missing or
truncated source evidence fails closed. Independent exact-head acceptance review still judges
issue-specific runnable tests, including explicitly reused existing tests, and actual correctness.
Post-merge closure runs the same delivery policy, leaves non-delivered issues open, and reopens
known non-deliveries prematurely closed by GitHub keywords. It never infers completion from a
source filename alone.

Bash comparison uses shfmt's typed syntax tree; CSS and JavaScript/TypeScript use
Tree-sitter syntax trees, and Python uses its native AST. Parser positions and comments
are excluded while literal/heredoc input remains data. The pinned `factory-syntax`
dependency group is included in local development installs; control-plane workflows
install the same pins through `.github/actions/factory-syntax`. No parser downloads occur
while inspecting PR content, and a missing parser fails closed with an actionable retry.

## Gated autonomous merges

### Owner-enabled factory-only immediate merge exception

The [Factory-only Fast Merge](FACTORY_FAST_MERGE.md) lane is an explicit
owner-authorized exception to the ordinary gated review and CI-wait process.
For trusted, unowned, non-draft PRs changing **only** the controller's
factory infrastructure allowlist, immediately attempt a SHA-pinned squash
merge through the dedicated workflow on PR/label events. **Do not wait for
CI, semantic review, `factory:ready`, or repeatedly poll check runs.**
The exceptional lane does not remove trusted-author, path, ownership,
hold, mergeability, SHA-pinning, or repository branch-permission controls.
A GitHub branch-rule refusal is a failed immediate merge attempt requiring
a narrowly scoped permission/bypass configuration fix, not authorization
to forge checks or remove product PR protections. Changes to the fast-lane
controller, workflow, config and tests are excluded from its own allowlist.

### Ordinary gated autonomous merges

Workers may merge a PR without asking again only after all of these gates are satisfied for the exact current head SHA:

- the PR is open, non-draft, and mergeable with no conflict;
- every required CI check has completed successfully;
- all actionable current review findings are fixed, demonstrably outdated, or rebutted with evidence;
- focused validation appropriate to the change has passed or exact-head CI provides that configured boundary;
- the PR truthfully completes its declared scope and does not hide required issue work behind avoidable follow-ups;
- merging will not violate ownership, migration, deployment, security, or data-safety constraints;
- the merge method is allowed by repository settings;
- the worker supplies the exact expected head SHA to prevent merging a moved branch.

Do not enable auto-merge. Perform the merge only after the gates are currently true. If any gate becomes false or cannot be verified, do not merge and return to repair or selection.

After merging, verify whether the linked issue closed truthfully. If executable issue work remains, continue it under normal priority rather than declaring victory from the PR merge alone.

Formal GitHub `APPROVED` state is not a required merge gate when GitHub cannot represent self-approval for Josh's single-user agentic repository. Review-clear means there are no unresolved actionable findings after inspecting the current reviews and threads; never fake a formal self-approval.

## Valid heartbeat outcomes

While executable issues remain, a normal heartbeat must accomplish at least one of these:

- push substantive code, tests, or a migration;
- repair a blocking defect, review finding, CI failure, or merge conflict;
- open a coherent non-draft implementation PR for an executable issue;
- merge an exact-head PR whose complete gate set is satisfied;
- repair factory behavior that is directly blocking issue delivery.

These are minimum substantive outcomes, not reasons to stop a run. After achieving one, continue the work-session loop while safe runtime remains and executable work exists.

Comments, labels, claims, reviews, PR-body edits, ready markers, help text, speculative plans, and optional test additions alone are not sufficient while executable work exists.

When the shared executable pool is genuinely empty, a truthful no-work completion is valid. The worker remains scheduled.

## Ownership and blocked work

Retain responsibility only while actively performing the current next action. Blocked ownership does not reserve the whole worker or the whole factory.

When owned work cannot safely advance now:

1. preserve concise durable blocker context;
2. release active execution when appropriate;
3. immediately select the highest-value free executable issue;
4. if none exists, end the current bounded session cleanly and remain enabled for the next heartbeat;
5. return when the blocker changes or another worker acquires the released work.

Blocked work never authorizes a worker to pause or disable itself.

When Josh or an interactive session acting on his direct instruction pauses or disables a scheduled worker, that interactive session must release the paused worker's open issue and PR claims to `factory:unowned` while preserving the truthful workflow-stage label and current resume packet. A paused worker must never strand executable work behind its owner label. Scheduled workers must not infer that another worker is paused solely from a missed heartbeat; explicit ownership release is the handoff signal.

For an interactive pause, reconcile each target atomically, preserve unrelated labels and the current resume packet, replace the owner with `factory:unowned`, and keep exactly one truthful stage. Unfinished implementation changes the issue to `ralph-status:pending`, `factory`, `factory:building`, `factory:unowned`; an existing PR keeps `factory`, `factory:building`, `factory:unowned`. CI-pending work keeps issue `ralph-status:validation` and PR `factory:ci`, both with `factory` and `factory:unowned`. Ready work keeps issue `ralph-status:in-review` and PR `factory:ready`, both with `factory` and `factory:unowned`. A PR-level blocker uses `factory`, `factory:blocked`, `factory:unowned`, while an issue-only blocker keeps the issue at `ralph-status:blocked`, `factory`, `factory:blocked`, `factory:unowned` and preserves the PR's truthful stage with `factory:unowned`.

## Mandatory label state machine

Every worker owns issue and pull-request metadata as part of the work. Reconcile labels when claiming, opening or replaying a PR, handing work off, receiving review, starting CI validation, becoming ready, blocking, merging, and ending a turn. Josh must never need to request routine factory labels.

Apply these states exactly. Reconcile each target with one full label-set replacement so stale mutually exclusive state and owner labels disappear in the same atomic write that applies the complete truthful target set. Never implement a transition as separate remove-then-add calls:

| State | Issue labels | Pull-request labels |
|---|---|---|
| Unclaimed executable work | `ralph-task`, `ralph-status:pending`, one priority, `factory`, `factory:unowned` | Not applicable |
| Actively implemented | `ralph-status:in-progress`, `factory`, `factory:building`, one `factory:<worker>` | `factory`, `factory:building`, one `factory:<worker>` when a PR exists |
| Exact head needs review | `ralph-status:in-review`, `factory`, `factory:review`, current owner or `factory:unowned` | `factory`, `factory:review`, current owner or `factory:unowned` |
| Actionable review findings | `ralph-status:in-progress` when owned, otherwise `ralph-status:pending`; `factory`, `factory:changes-requested`, current owner or `factory:unowned` | `factory`, `factory:changes-requested`, current owner or `factory:unowned` |
| Review passed; exact-head CI pending | `ralph-status:validation`, `factory`, `factory:ci`, current owner or `factory:unowned` | `factory`, `factory:ci`, current owner or `factory:unowned` |
| Every merge gate satisfied | `ralph-status:in-review`, `factory`, `factory:ready`, current owner or `factory:unowned` | `factory`, `factory:ready`, current owner or `factory:unowned` |
| Human or external blocker | `ralph-status:blocked`, `factory`, `factory:blocked`, `factory:unowned` | Issue-only blocker: preserve the truthful PR workflow state with `factory:unowned`; PR-level blocker: `factory`, `factory:blocked`, `factory:unowned` |
| Lease released or stale | Executable status, `factory`, `factory:unowned`; also preserve `factory:review` or `factory:changes-requested` when applicable | `factory`, `factory:unowned`, plus the truthful review state |
| Merged and complete | `ralph-status:done`, then close after verification; remove transient factory state/owner labels | Merged PR needs no further transition |

Rules:

- The issue/PR state reconciler enrolls open, non-draft repository PRs missing
  factory metadata on open/reopen/unlabel events and every 15 minutes. Contributor
  fork PRs participate in the same review, repair, CI, and merge lifecycle as
  factory and local PRs. Dependabot and Renovate are excluded. It preserves unrelated labels and an
  existing owner/stage, defaults missing ownership to `factory:unowned`, and uses
  exact-head trusted review markers when recovering a missing stage; otherwise
  it requests `factory:review`. Green CI alone never establishes readiness.
  A stage rewrite derived from trusted PR history never replaces an active owner
  lease: the final label writer applies the shared lease policy to the same live
  issue read that builds its replacement label set, preserving `factory:local`
  while its last GitHub update is within the 60-minute local TTL (or no usable
  time evidence is available). Stale local claims are recoverable even when
  their original claim/progress comments were missing or untrusted; numbered
  owners remain controller-owned because only the controller resolves active
  fixed-worker/run evidence. GitHub's label replacement has no
  compare-and-swap, so a claim landing in the final read-to-write interval
  remains a residual race.
- A contributor PR, Josh-authored manual PR, or local factory PR follows the same review,
  repair handoff, exact-head CI, and merge-drain stages. Branches named
  `local/*`, `agent/*`, and `chatgpt/*` are eligible when enrolled with
  `factory`. The controller checks the PR's verified GitHub author and exact
  head commit for the local `Model:` trailer; missing trailers on verified
  Josh-authored human commits are attributed to `human`. A PR whose author
  cannot be established does not receive fabricated model provenance.
  Keep manual-only PRs out of this workflow with
  `<!-- factory-execution:manual-only -->`.
- A head advanced outside the numbered-worker wrapper (an owner-pushed merge or
  rebase, a local agent, or trusted repository automation) has no
  controller-authored author marker. The review controller repairs that
  automatically during the ordinary review pass, so such a head is reviewable
  exactly like a wrapper-pushed one: it attests the exact-head author from the
  wrapper's own contributor record when there is one, otherwise from the pull
  request's verified GitHub author and the head commit's `Model:` trailer (or
  `human` when the trailer is absent), and never invents a numbered-worker
  identity. A controller-authored exact-head attestation counts as complete
  provenance, so one independent approval authorizes the head, the same as for a
  wrapper push. For a head that will not receive another review pass, reconcile
  it explicitly with
  `python3 .github/scripts/factory-review-controller.py reconcile-provenance --pr N`
  from trusted main, or the `Factory Provenance Reconcile` workflow for the same
  effect as `github-actions[bot]`; the command releases the head to
  `factory:review`. `--head` optionally pins the exact SHA and fails closed on a
  moved head.
- If an approved review cannot record trusted author/reviewer provenance because
  its staged roster is missing or invalid, or a provenance write fails, the
  controller preserves the approval and parks the PR and linked issue at
  `factory:blocked` with no active owner. Its redacted diagnostic identifies the
  infrastructure failure. Repair the trusted bundle or write access before
  reconciling the exact head; do not assign another semantic review of unchanged
  code to work around a controller failure.
- `factory:local` remains an active local-work lease, not an opt-out from
  enrollment. A local session MUST release that lease to `factory:unowned`
  when it opens a ready-for-review PR or finishes a repair; the fleet never
  steals an actively edited local checkout. Review findings hand off as
  `factory:changes-requested`, and independent workers can pick up the PR
  once the lease is released.
- The manual/local review controller follows explicit `Closes #N`,
  `Fixes #N`, or `Resolves #N` references when reconciling a linked issue, and
  falls back to the canonical standalone `Part of #N.` line (including
  `Part of #N and governed by #M.`) so a review-only handoff still receives
  exact-issue acceptance. That line never closes or completes #N at merge:
  merge-time closure still requires a closing keyword or the canonical
  `factory/<worker>-<issue>-` branch. Casual mentions such as `Advances #N`,
  `See #N`, or a bare parent reference do not create ownership, issue
  mutations, or review contracts.
- `factory:building`, `factory:review`, `factory:changes-requested`, `factory:ci`, `factory:ready`, and `factory:blocked` are mutually exclusive workflow states.
- `factory:unowned`, `factory:local`, and every `factory:<number>` owner label are mutually exclusive next-action owners.
- Never leave a factory-produced or factory-managed open PR without `factory`, one truthful workflow-state label, and one truthful owner label.
- A push invalidates `factory:ci` and `factory:ready`; transition the exact new head back to `factory:review` unless review findings already require `factory:changes-requested`.
- Cross-worker takeover and merge are allowed. The new worker replaces the owner label and may merge work it did not author after every exact-head gate passes.
- If `gh pr edit` fails because of deprecated Projects Classic GraphQL fields, use the issue-compatible REST label-replacement endpoint with the complete target label set. A POST that only adds labels, or sequential DELETE/POST calls, is not atomic reconciliation.
- Derived observability labels (currently `factory:merge-conflict`, from GitHub mergeability) are projections, never stages. They never satisfy the workflow-state or owner requirement and never enter `STAGE_LABELS`, stage precedence, `BLOCKED_LABELS`, or queue selection. There is no `factory:approved` label.
- Before ending any turn, compare the issue, PR, review, CI, lease, and merge state and repair any metadata contradiction discovered.

## Repository safety

- Contributor fork PRs enter the normal factory lifecycle automatically. A
  credentialed factory worker never checks out or executes fork source, even for
  review. The worker resolves the fork head through the base repository's
  `refs/pull/<n>/head`, refuses `checkout_target` for an external head repository,
  and reviews fork source only as a bounded data snapshot with every model tool
  and integration hard-denied. Review the exact head; never assume the fork branch
  exists on the base repository and never create a same-named base branch.
- Fork repairs never run in the credentialed worker. The worker writes a trusted
  dispatch receipt and starts `.github/workflows/factory-fork-execution.yml`, which
  has read-only permissions, no inherited secrets, and no registry publishing.
  Contributor dependencies, tests, hooks, and the anonymous Kilo repair run only
  on that secretless runner. Its result is data: `.github/workflows/factory-fork-persistence.yml`
  runs trusted `main` code, validates workflow identity, the receipt, the live
  lease, the exact head, and a single descendant commit, imports the bundle into a
  bare quarantine without checkout, and only then pushes to the existing fork head
  with maintainer edit access. Preserve contributor commits and authorship; never
  execute artifact code on the persistence host.
- Fork CI runs on `pull_request` with read-only credentials and no registry
  publishing. Metadata-only `pull_request_target` workflows use trusted default
  branch code and never execute the fork checkout with their write token.
  Enrollment does not attest provenance, approve code, or bypass exact-head gates.

- Fixed-model PR creation must use `PR_REBASE_TOKEN`, like git pushes. Keep the
  workflow token for controller comments and labels; creating the PR with that
  token attributes it to `github-actions[bot]`, which can leave Actions awaiting
  approval and delay independent review workflows.
- Never push directly to `main`.
- Never create or convert a draft PR unless Josh explicitly requests a draft.
- Never enable auto-merge.
- Never merge unless every gate in this policy is verified for the exact current head SHA.
- Never weaken checks, skip tests, remove meaningful coverage, bypass hooks, or add suppressions merely to make CI green.
- Never manufacture evidence or claim commands ran when they did not.
- Never mutate factory schedules or topology. Only Josh or an interactive session acting on Josh's direct instruction may do so.
- Never pause, disable, suspend, or stop a scheduled factory because the ordinary backlog is blocked or empty. Keep the schedule enabled and end only the current bounded session when no executable work exists.
- When an interactive session pauses or disables a worker on Josh's instruction, release that worker's open claims before treating the pause as complete.

## Closure truth

Use a closing keyword only when merging the PR will truthfully satisfy the entire issue contract.

Success hierarchy:

- issue truthfully closed and verified after merge;
- complete exact-head PR safely merged;
- complete PR gate-verified and awaiting only an external condition;
- blocking defect repaired or coherent implementation materially advanced;
- coherent new implementation PR opened from the backlog;
- truthful no-work completion when the shared executable pool is empty;
- optional PR polishing while executable issues remain: policy failure.

## Product acceptance for PRDs and epics

Child closure is evidence of progress, not sufficient evidence that a product epic's acceptance scenarios work. Parent PRD and epic issues require an explicit product-acceptance stage before they may be marked complete.

Integrated parent product acceptance is human/interactive controlled. Autonomous factories do not select parent PRD/epic issues as ordinary work and do not issue the deciding product-acceptance verdict. Factories may implement explicitly scoped child issues and produce focused evidence for later acceptance.

See [`docs/PRODUCT_ACCEPTANCE_PROTOCOL.md`](PRODUCT_ACCEPTANCE_PROTOCOL.md) for the full acceptance workflow, comment structure, and regression targets.

Key rules for factory workers:

- Do not select or claim a parent issue labeled `epic` or `prd`.
- Do not select or claim any issue containing `<!-- factory-execution:manual-only -->`.
- Under an architecture hold, implement only an explicitly authorized narrow child with frozen requirements. Do not reinterpret the architecture, broaden the child, lift the hold, or close the parent.
- Child closure alone does not satisfy parent acceptance.
- Factories may provide tests, Chromium/E2E evidence, API evidence, and other child-level artifacts for the later interactive acceptance run.
- A factory must not post the final `<!-- product-acceptance:v1 -->` verdict as the deciding authority, mark the parent `ralph-status:done`, or close the parent.
- Duplicate factory PRs that attempt to close already-delivered child work do not satisfy product acceptance.

## Adaptive stale-PR decay guard (ADR)

### Context

Factory PRs converge quickly: P25 at 1.27h, P50 at 2.80h, P75 at 7.37h, P90 at 11.58h, max at 37.96h. Long-lived implementation attempts indicate failed convergence. The existing semantic strike policy does not cover this failure mode — CI churn, transient model/provider failures, merge/rebase churn, worker handoffs, stale reviews, and no-diff handoffs do not consume strikes.

### Decision

Define staleness relative to a rolling population of the **last 100 eligible successfully merged Factory PRs**. The implementation calculates PR lifetime from `created_at` (never `updated_at`):
- For merged PRs: `merged_at - created_at`
- For open PRs: `now - created_at`

The threshold uses a robust distribution-derived rule: **`2 × P90`** as the default candidate, compared with a median/MAD formulation. The final formula uses the more conservative (larger) of the two, subject to a documented lower floor of **1 hour** and a cold-start fallback of **24 hours** when fewer than 10 merged PRs exist.

### Non-destructive recovery behavior

When an autonomous Factory implementation PR crosses the stale threshold:
1. Report `stale-preserved` with the PR number, linked issue, age and measured threshold.
2. **Keep the PR and branch open**, including their commits, existing worker lease, review findings, CI history and linked issue stage.
3. Route recovery through the existing PR review, CI repair, conflict repair and implementation queue. The owner or next worker should fix the actual blocker on the same PR rather than throw away partial work.
4. Close an unfinished PR only via a separate, explicit and evidenced decision that its work is completely merged, superseded, duplicate or irrecoverable. Record exactly what happened to its unique commits and how the owner objective will finish. Age, repeated provider failures, a stalled review, or a partial migration slice are not adequate reasons by themselves.

The existing dispatcher consults **Jev** for aged, non-deferred PRs using `OPENROUTER_JEV_API_KEY` in bounded batches. Jev selects `preserve` or `closure_review` using trusted current-head metadata and the issue/PR context. A high-confidence `closure_review` recommendation is **advice to investigate**, never approval to close. A worker must independently establish that all unique behavior and tests are merged, superseded, redundant, or irrecoverably invalid and preserve a durable handoff before an explicit close. If Jev is missing, returns invalid output, or is uncertain, the PR remains open.

Scheduled `factory-work-controller.py stale` is **diagnostic only**. It must never reset the linked issue or close its canonical PR based solely on age or a Jev score. Its `expired_count` remains zero for compatibility, and `stale_preserved_count` tracks recoverable aged PRs. Dry-run and ordinary evaluation both preserve work.

The older age-driven stale-expiration markers remain interpretable for historical audit and reopened PRs, but the scheduled decay check no longer produces them. Semantic review strikes are a separate policy and do not authorize treating age as a semantic defect.

### Relationship to semantic strikes

- **Semantic strikes:** repeated evidence that an implementation is substantively wrong
- **Stale diagnostics:** a recovery signal that an implementation has not yet converged in normal Factory time, regardless of cause; not permission to close the PR
- Neither mechanism increments or masquerades as the other

### Scope/eligibility

Only autonomous Factory implementation PRs should participate. Exclude Dependabot, manual/Codex work, infrastructure PRs, and other non-product automation. The baseline population uses the same Factory-implementation definition as the PRs being guarded.

### Implementation

The `stale_pr_decay.py` module in `.github/scripts/` provides:
- `get_staleness_baseline()` — builds the rolling population baseline
- `is_stale_attempt()` — checks if a PR age exceeds the threshold
- `process_stale_prs()` — evaluates aged PRs and reports them without closing or resetting them
- `StalePRGuard` — configurable guard class
- `build_stale_expiration_marker()` / `parse_stale_marker()` — durable marker support
- `get_staleness_observability()` — logging summary

The controller exposes a `stale` subcommand: `factory-work-controller.py stale [--dry-run]`.
The fixed-model dispatcher invokes `stale` once per serialized dispatch tick,
after the ready-PR merge drain and before new assignments are selected. This is
an observability step, never a destructive cleanup operation.

## Semantic review strike policy

A factory implementation attempt gets three substantive semantic repair cycles. A cycle is counted once per distinct PR head that receives a trusted controller-authored `verdict-repair` semantic review marker. Mirrored comments, duplicate markers for the same head, stale-head results, CI failures, merge conflicts, provider failures, and no-diff handoffs do not consume this semantic strike budget.

On a fourth distinct semantic repair verdict, cancel that implementation attempt instead of repairing it again: persist the fourth review findings, close the PR, return the linked issue to unowned executable implementation, and record a `comic-pile-factory-strike-reset-v1` marker on the issue. When producer provenance is known, that producer is ineligible to receive the first clean implementation retry. The exclusion is one-shot and is spent only after a different factory posts a real implementation claim for the retried issue.

This is an attempt-level circuit breaker, not an issue-level failure. The issue remains valid work unless a separate terminal blocker applies.

## No-progress PR retirement (owner-authorized)

Three trusted \`repair-no-persisted-change-handoff\` records on the **same unchanged
PR head**, across the review/changes-requested handoff, exhaust that implementation
attempt. The serialized dispatcher must close the PR and immediately return its
original open issue to executable unowned intake instead of waiting for a rolling
24-hour no-diff reset and then repeating the same unsuccessful repairs.

This is an explicit owner-authorized terminal disposition for an *attempt*, not
an assertion that the issue is solved or that its commits are worthless. Before
closing, write an issue handoff containing the exact PR, branch, SHA and
instructions to inspect/reuse unique code and tests. Never delete the branch.
Issue-level no-diff markers preceding that trusted handoff cease to suppress
new implementations; PR-level evidence remains preserved. Retire only canonical,
same-repository, non-draft Factory PRs with an unowned, open, non-held linked
issue, matching trusted release evidence, and an unchanged head after a second
live read. A live worker lease, new head, conflicting PR, missing evidence,
manual-only hold or unavailable GitHub read prevents automatic closure.

Transient provider/model interruptions, CI or review infrastructure errors,
old PR age, and ordinary semantic findings do **not** trigger this rule. They
retain the separate recovery and review policies below. The existing fourth
distinct semantic-repair verdict still independently cancels a wrong
implementation. Terminal retirement must not bypass product release safety or
falsely mark the original issue done.

## Review infrastructure retry cooldown

For an unchanged PR head and stage, three trusted release records for transient
model interruption, review-agent failure, head-read failure, pre-session assignment-read
failure, missing/conflicting verdicts, or authoritative-diff read failure
suppress further assignments for a rolling 30 minutes. The existing generation
retry gate enforces the cap without labeling infrastructure failures as human
blockers or semantic strikes. A real new head, changed stage, or newly developed
merge conflict gets a fresh budget. Expired failures drop out automatically.
Release markers retain the reason, worker, timestamp, exact SHA, stage, and
conflict state; dispatcher logs show suppressed generations and counts by reason.
Kilo JSON telemetry stays intact; only assistant-text events enter the verdict
channel. Tool output and fenced examples cannot authorize an approval. Untrusted comments do not
count, and one PR's failures do not suppress other work.

## Markers and leases

Use the existing canonical marker schemas:

- issue claim: `<!-- comic-pile-factory-implement-claim-v3:issue-<n>:<worker>:<epoch>:attempt-<n> -->`
- issue progress: `<!-- comic-pile-factory-implement-progress-v3:issue-<n>:<worker>:<epoch> -->`
- review claim: `<!-- comic-pile-factory-review-claim-v2:<sha>:<worker>:<epoch> -->`
- review pass verdict: `<!-- comic-pile-factory-review-v2:<sha>:pass -->`
- review changes-required verdict: `<!-- comic-pile-factory-review-v2:<sha>:changes-required -->`
- repair claim: `<!-- comic-pile-factory-fix-claim-v3:<sha>:<worker>:<epoch>:attempt-<n> -->`
- repair progress: `<!-- comic-pile-factory-fix-progress-v3:<sha>:<worker>:<epoch> -->`
- ready: `<!-- comic-pile-factory-ready-v2:<sha> -->`
- needs human: `<!-- comic-pile-factory-needs-human-v2:<sha-or-issue> -->`
- released: `<!-- comic-pile-factory-claim-released-v3:<target>:<worker>:<epoch>:<reason> -->`
- merge closure: `<!-- comic-pile-factory-merge-closure-v1:pr-<pr>:issue-<issue> -->`
- unreadable-dependency warning: `<!-- comic-pile-factory-dependency-unreadable-v1:pr-<pr>:issue-<issue> -->`
- product acceptance: `<!-- product-acceptance:v1 -->`
- stale expiration: `<!-- comic-pile-factory-stale-expiration-v1:pr-<pr>:issue-<issue>:age-<s>:sample-<n>:formula-<f>:threshold-<t> -->`
- head contributor: `<!-- comic-pile-factory-head-contributor-v1:pr-<pr>:head-<sha>:worker-<n>:epoch-<s> -->`

## Durable resume packet

Before releasing ownership, reaching a runtime limit, switching work, or ending with a claimed issue or PR unfinished, create or update one canonical GitHub comment in place. Do not create a new packet on every heartbeat.

```text
<!-- factory-resume:v1 -->
## Factory resume packet
Head: `<current SHA or none>`
Current hypothesis: <one or two concrete sentences>
Files touched: <paths, or none>
Checks: <passed and failed commands/checks; include the decisive failure>
Next narrow verification: <one specific command, inspection, or experiment>
Remaining blocker/action: <what the next worker must resolve>
Updated by: <durable worker ID and UTC timestamp>
```

Record observed facts, distinguish local checks from CI, include no secrets, and keep the packet short. A takeover worker reads the current packet before reconstructing context, verifies that its recorded head still matches, and updates or discards stale claims instead of trusting them blindly. The packet is operational state, not a substitute for commits, tests, review markers, issue acceptance criteria, or truthful labels. Completed and verified work does not require a packet.

Review leases last 45 minutes. Repair and implementation leases last 60 minutes after the latest real progress. Lease expiry permits another worker to continue that issue but does not require a peer to choose it over higher-priority work.

## Z.AI free account quota backoff

Worker 97 adds a second `z-ai` / `glm-4.5-flash` lane at :50, thirty minutes
from worker 54 at :20, using the existing `Z_AI_API_KEY` and `PR_REBASE_TOKEN`.
HTTP 429 from either smoke or the coding session records `provider_quota`.
Before assigning work, the controller reads trusted registry attempt evidence
and pauses the shared Z.AI account for thirty minutes from the recorded error.
All Z.AI workers share this backoff; expiry automatically permits another probe.
A rate limit is not evidence that a model has been removed and does not retire
any worker. Failure to read the quota evidence aborts assignment rather than
assuming the account is available.

## External scheduled-factory heartbeat

The former registry issue #1093 is retired as factory heartbeat/registry infrastructure (2026-11-16). GitHub refuses new comments on threads with more than 2,500 comments, and GitHub comments must not be operational infrastructure. No worker, workflow, or controller may read, create, or update #1093 comments. Worker lifecycle (started/exit) is written to Neon by the run workflow's trusted jobs, and attempt outcomes, model health, failover and transitions are persisted to Neon from the trusted Actions job log. Neon and those job logs are authoritative. Issue #1093 and the watchdog's alert issue remain protected and non-executable: workers must never claim, implement, label, or close them.

Heartbeat telemetry never counts as substantive progress, never satisfies a valid heartbeat outcome while executable work exists, never outranks executable product work, never extends a lease, and never justifies ending a run early. The external watchdog may report missing or stuck heartbeats, but only Josh or an interactive session acting on his direct instruction may change a scheduled task.

Worker prompts put stable policy before run identity, targets and findings.
Trim duplicated instructions; preserve source/issue security and exact-head review context.

### Kilo served-model attribution

For Kilo sessions, structured `step_finish.part.model.modelID` values feed the
Actions run summary and the `factory: advance ... with ... (served: ...)` commit
subject. The trusted wrapper aggregates distinct model IDs across invocations,
keeps the requested `kilo-auto/free` route as provenance, and omits route aliases
from served-model evidence. Missing metadata is explicitly unreported; assistant
and tool prose cannot supply model attribution. The zero-cost guard remains in force.

### Second Kilo free lane

Worker 98 runs `kilo-auto/free` at :10, thirty minutes from worker 46 at :40.
It uses the same anonymous Kilo CLI free route and existing `PR_REBASE_TOKEN`;
there is no second account or credential. A smoke or session HTTP 429 records
`provider_quota` and pauses assignment for both Kilo workers for thirty minutes,
then automatically permits a new probe. A quota-limited smoke ends immediately
instead of continuing into an expensive coding session. Worker 46's historical
static-pause protection does not exempt it from this shared runtime quota backoff.

### Monthly provider quotas pause; they do not retire a lane

Mistral and Ollama Cloud monthly-quota failures record `provider_quota` during
smoke and sessions. Assignment pauses that source until the next UTC calendar
month as a configured fallback when no account reset date is supplied; expiry
permits another smoke automatically. Another quota failure renews the pause.
This fallback is not evidence of a provider's actual account billing date.
Monthly quota alone cannot justify roster retirement. Retirement requires
separate non-quota evidence, such as model removal, revoked authentication or
repeated invalid output, recorded in its PR. Catalog discovery already protects
these runtime-only providers from catalog-absence retirement.

The earlier retired identities 48 and 55 remain retired. Post-reset runs
[41774076436](https://github.com/JoshCLWren/comic-pile/actions/runs/41774076436)
and [41774209074](https://github.com/JoshCLWren/comic-pile/actions/runs/41774209074)
on November 4, before #4622 merged, still reported Mistral API budget exhaustion
and Ollama monthly usage exhaustion respectively. Their terminal registry
records said `unknown_failure`; smoke classification now preserves that quota
cause. Josh confirmed on November 7 that quotas remain exhausted. No new live
probe was run and no recovery is claimed. Only a later successful existing-key
probe can justify restoring a provider under a fresh, unused worker number.

### Kilo served-model attribution

For Kilo sessions, structured `step_finish.part.model.modelID` values feed the
Actions run summary and the `factory: advance ... with ... (served: ...)` commit
subject. The trusted wrapper aggregates distinct model IDs across invocations,
keeps the requested `kilo-auto/free` route as provenance, and omits route aliases
from served-model evidence. Missing metadata is explicitly unreported; assistant
and tool prose cannot supply model attribution. The zero-cost guard remains in force.
Delivery funnel reports use separate opened/merged/unmerged-close event windows
of 14 and 30 days. Starter PR outcomes and advance-commit contribution shares
are distinct. Each merged PR contributes one credit unit divided among its
advance commits, including squash-body subjects without duplicate credit.
Retired and unattributed history remain visible. Immutable merged attribution
is cached on the existing status branch; unavailable history is not reported as zero.

### Measured starter/finisher preference

Within otherwise eligible work, lanes pinned to `z-ai` / `glm-4.5-flash`
prefer fresh issues and lanes pinned to `kilo-auto` / `kilo-auto/free` prefer
PR changes-requested and CI-repair work. The role comes from source/model in
the current roster, including future duplicate slots, not worker numbers.
Main-breakage and user-reported bugs retain protected priority. Every lane
falls back to normal ranking when no preferred work is eligible. Manual
issue/repair dispatch tries the corresponding eligible specialist first, then
continues through normal workers if assignment declines (busy, paused or leased).
Review independence, claims, quotas, and remote capacity gates remain unchanged.


## Production deployment recovery

`Production Deploy Recovery` maintains one GitHub repair issue for an unresolved
attempted deployment failure, on completion and a 15-minute heartbeat. `deploy-fix`
repairs use the protected priority lane alongside main-breakage. Existing remote
factory WIP/review-backlog thresholds and all lease, CI, independent review and
production gates still apply; this changes neither schedules nor capacity limits.

A new `deploy-fix` merged onto main after the last failed attempted release permits
one automatic recovery deployment with schema verification and both production
smokes. A failed recovery consumes that repair; it cannot blindly retry unchanged
code. The repository owner retains the explicit reasoned manual freeze override.
A fully verified release closes managed repair issues when no live worker owns them.

Before issue persistence, compare staged paths (including rename sources) with
all pages of open same-issue factory PR files. Reuse an available overlapping
PR and its exact branch/head; preserve live ownership and fork checkout fences.
Revalidate before a new PR is created. Unreadable state or a live owner blocks
duplicate creation; disjoint files remain eligible for separate PRs.

## Additional provider free-tier lanes (2026-11-15)

This fleet uses free-only or evaluation-only credentials. The roster and source caps
are not equivalent to a provider-side spending limit. API keys MUST belong to
accounts/projects that cannot auto-bill beyond a free allowance. Missing keys
skip execution; never substitute a paid model, silently switch to a billed plan,
or copy a credential into source control.

- `gemini-free`: `GEMINI_API_KEY`, free-tier Google AI Studio project only;
  `gemini-3.8-flash` and `gemini-3.1-flash-lite`. On the Google free tier,
  prompt content may be used for product improvement; never submit secrets.
  Base URL: https://generativelanguage.googleapis.com/v1beta/openai
- `groq-free`: `GROQ_API_KEY`, free account and tool-capable GPT-OSS 120B,
  GPT-OSS 20B, and Qwen 3.8 27B. Shared rate limits (including tokens/day)
  are enforced by Groq, with a conservative factory daily dispatch cap.
  Base URL: https://api.groq.com/openai/v1
- `vercel-ai-gateway`: existing `AI_GATEWAY_API_KEY`; only explicit zero-price
  models (`reflection/beam-free`, `nvidia/nemotron-3.5-lightning-free`,
  `stealth/glyph-cluster`). Every run checks current catalog input and output
  prices **before** inference; missing or paid entries fail closed. The $5 free
  monthly credit is not authorization to consume paid model inference.
- `aion-free`: `AION_API_KEY`, daily free allowance (20K tokens). Pin only
  Aion 3.5 Mini; paid model price may consume the free daily allowance.
  Use an account without paid top-ups or auto-payment. One dispatch per UTC day.
  Base URL: https://api.aionlabs.ai/v1
- `cohere-eval`: `COHERE_API_KEY`, free evaluation key only. Command A+;
  1K evaluation requests per month. Do not treat this key as paid production.
  Base URL: https://api.cohere.ai/compatibility/v1
- `typhoon-free`: `TYPHOON_API_KEY`, 2.5 30B and 2.1 12B model variants.
  Free research/light-use tier with conservative daily dispatch cap.
  Base URL: https://api.opentyphoon.ai/v1

LLM7.io is **not** a coding factory lane because the provider restricts tool
calling to paid Pro access. Do not add a fake free contributor slot. Its text-only
free tier may be considered for a separately implemented independent static
reviewer, subject to review-stage routing and separate credentials.

Adding worker IDs does not provision provider accounts or secrets. Existing Neon
model-health exclusions, source cooldowns, account-level budget accounting, and
runtime authorization must remain authoritative.

## Owner-authorized persisted work PRs (2026-11-15, #5183)

Trusted wrappers publish unvalidated code periodically and at finalization on the
actual normal, non-draft issue implementation PR against `main`. This is the
resumable unit of work, with a real issue title, intentional scoped closure,
acceptance/handoff context and parsed producer metadata. Repairs advance the
assigned PR branch atomically with a tracking ref, under the actual live lease;
no competing recovery PR is created. Any eligible agent continues that same PR.

The active actual factory owns the PR. When execution ends, incomplete work is
released to `factory:changes-requested` + `factory:unowned` for repair pickup.
Old-run reconciliation preserves resumed owners, stages and ordinary commits.
Publication never certifies checks, approval or successful delivery. Failed
checks preserve code and remain failed. Normal product CI/review/merge gates
remain intact; the existing factory-only fast lane is unchanged. See
[persisted work handoff](FACTORY_CHECKPOINTS.md) for trusted execution, branch
identity, cancellation reconciliation and retention.
