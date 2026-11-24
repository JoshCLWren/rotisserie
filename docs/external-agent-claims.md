# External next-work claims (issue #177)

Rotisserie owns ranking, GitHub owns PR/review/CI/merge facts, and the existing
The factory store (libSQL) ledger owns work leases. **A frontier candidate is not a
reservation.** No external coding agent needs to join the provider/model roster.

## Agents with a trusted local factory store connection

Run from a checkout with the current installed Rotisserie runtime, GitHub CLI
authenticated as the actual agent principal, and the factory store
configured as for the trusted controller (`FACTORY_TURSO_URL` and a token file in
`FACTORY_TURSO_TOKEN_FILE`):

```bash
baste next --repo acme/widgets --json
baste reserve-next --repo acme/widgets --client chatgpt --json
```

Only a JSON response with `"status": "reserved"` and an exact `claim_id`
authorizes beginning the returned action. A conflict or hold requires refreshing
the frontier, never silently selecting an unrelated issue. To compete for a
previously advertised exact offer, supply `--target pr:123` (or `issue:123`).
If the top eligible offer changed, Rotisserie refuses and returns a refresh
instruction.

Renew before expiration. For example, use the claim UUID returned above:

```bash
baste renew UUID --repo acme/widgets --client chatgpt --json
baste release UUID --repo acme/widgets --client chatgpt --json
gh workflow run rotisserie-external-claim.yml --repo acme/widgets --ref main \
  -f operation=handoff -f client=chatgpt -f claim_id=UUID \
  -f evidence=https://github.com/acme/widgets/pull/123
```

Handoff records evidence in the factory store and releases the claim. It **does not**
approve a review, bypass required checks, merge a PR, close the issue, or
authorize deployment. Use ordinary controller gates after recording evidence.

### What an outside agent can take

`implement`, `review`, `review-findings`, `ci-repair` and `merge-conflict`. Use
the repository's exact name (`--repo` is compared with GitHub's `full_name`):
lease keys and the advisory lock are built from it. `reserve-next` walks the
ranking and reserves the highest offer the agent can do that is live right
now, so a top PR still waiting on CI does not hide lower work. `--can ACTION`
narrows the choice to what the agent can do; it still receives the
highest-ranked offer of those. Merge and post-merge verification stay with the
factory: they need merge authorization and production evidence.

An outside review counts for the merge gate (#191). The trusted bridge
records the reviewer's declared model, and refuses the review when that model
authored the current head: the head's recorded author model, else its `Model:`
trailer, followed through a clean sync merge to the commit it carries. A model
that authored only an earlier head may review.
GitHub itself refuses a formal review from the pull request's author. Any
other login can review with a model that did not author the PR; the factory
does not compare logins.

### Handoff evidence

The only evidence is the pull request that carries the work:

- `implement`: an open, non-draft PR in the same repository whose body says
  `Closes #ISSUE` for the claimed issue, in no other factory stage.
- `review-findings`, `ci-repair`, `merge-conflict`: the claimed PR itself,
  with at least one new commit since the claim (its head moved), still in a
  completion stage.

The trusted controller checks that evidence and moves the PR into the
existing review queue (`factory`, `factory:review`, `factory:unowned`),
exactly as a factory worker does when it finishes. Both steps run **inside
the ledger's locked transaction** that releases the claim: if either fails,
the transaction rolls back and the claim stays held, and no other claim,
revoke or factory acquisition can interleave. With no PR to show, `release`
the claim instead.

- `review`: the claimed PR, with a formal GitHub review by the claimant on the
  claimed head, submitted as **Approve** or **Request changes**. The review body
  needs one `Model: <exact model id>` line. An approval also needs the issue's
  `FACTORY_ACCEPTANCE_V1=` report (the same report factory reviewers write),
  because the merge gate requires an acceptance record for the exact head.
  An approval moves the PR to `factory:ci`; a change request moves it to
  `factory:changes-requested`. Hand off `.../pull/N#pullrequestreview-ID` to
  name one review, or the PR URL for the claimant's latest one.

The PR body must say `Closes #ISSUE` for the claimed issue, and a factory
branch name, if used, must name the same issue: GitHub closes what the body
names while the factory links by branch first.

### Authorship

Every delivered commit must end with exactly one `Model: <exact model id>`
trailer, or the handoff is refused and the claim stays held. Delivered commits
are the PR's own commits (not on its base branch); for a repair, only those
new since the claimed head. The bridge writes a controller-trusted
author-model marker for each (agent `external-CLIENT`), so no model that
contributed the work can approve it. Only
the Actions bot's comments count as author records, so **every handoff runs
through the GitHub bridge**: a CLI agent reserves, renews and releases from
the CLI, and `baste handoff` prints the exact `gh workflow run`
command to use instead. If the PR head moves while the handoff runs, it is
refused and the claim stays held; hand off the new head.

The GitHub account alone is never treated as a model identity.

Do not put store credentials into a model session, its prompt, tool output,
artifact, commit, or issue comment. The local direct CLI is only available to
an appropriately trusted controller environment. An ordinary agent without
that access must use the GitHub bridge.

## GitHub-only agents, including phones

An adopter that installed Rotisserie's
`.github/workflows/rotisserie-external-claim.yml` can use **Actions → Rotisserie
External Claim Controller → Run workflow**. Set `operation=reserve-next` and a
stable `client` string, for example `codebuff-phone`. No issue number is
required. Download the run's one-day `rotisserie-external-claim-<run>` JSON
artifact, read the exact target, lease UUID and expiry, and follow the handoff.

The reservation's `handoff` packet carries the exact `gh workflow run`
commands for renewing, releasing and handing off that claim (`renew_github`,
`release_github`, `handoff_github`), its `expires_at`, the evidence it needs
and where to report. Renew well before `expires_at` (claims last 30 minutes).

Agents with GitHub CLI access can dispatch the same authorized workflow:

```bash
gh workflow run rotisserie-external-claim.yml --repo acme/widgets \
  --ref main -f operation=reserve-next -f client=codebuff-phone
```

The Rotisserie CLI does all of this for you with `--via github`: it starts the
workflow, waits for its run, downloads the result and shows it exactly as a
direct call would (a refusal inside the workflow exits 4, as it does locally).
No store connection is needed on the agent's machine:

```bash
baste next --repo acme/widgets --via github
baste wait-for-work --repo acme/widgets --can merge-conflict --via github
baste reserve-next --repo acme/widgets --client claude-code --via github --json
baste renew UUID --repo acme/widgets --client claude-code --via github
baste handoff UUID --repo acme/widgets --client claude-code \
  --evidence https://github.com/acme/widgets/pull/123 --via github
```

`wait-for-work` checks the work list until it offers an unheld candidate for an
action the agent listed with `--can` (by default every supported action), then
shows it and exits; it never reserves. With `--via github` each check is a
workflow run, so it checks every 5 minutes by default (`--interval`, at least
120 seconds) and gives up after an hour (`--timeout`, 0 waits until stopped).
If a claim run does not finish or publishes no result, the CLI reports
`unknown` (exit 6) with the run URL: the run may still have reserved work, so
look before retrying.

By hand, use `gh run list --repo acme/widgets
--workflow rotisserie-external-claim.yml` to identify the initiated run and
`gh run download RUN_ID --repo acme/widgets` to retrieve its
machine-readable result. Match the workflow run you triggered, not another
agent's run. The GitHub bridge supports `renew`, `release`, and `handoff`
with the same `client`, `claim_id` and (for handoff) a same-repository
`evidence` URL.

The manual workflow is a trusted **controller**, not a coding-worker workflow.
Its token can write issues and pull requests only for a verified handoff
(review-stage labels and the trusted author-model comment); it runs no PR code.
It requires the GitHub initiating actor to retain write permission, refuses
reruns and non-manual events, receives the store token only in a 0600 file
that a final step removes. A read-only GitHub token or a missing store connection does
not grant permission to reserve. GitHub labels are advisory projections.

## Label claims (issue #191)

Any agent with write access can find and claim work with ordinary GitHub
search and labels. No Rotisserie CLI, workflow dispatch or store access is
needed. The installed `rotisserie-label-claim.yml` workflow answers.

1. **Pick work.** Any open issue or PR you were told to work on can be
   claimed. Factory capacity, backpressure and ranking never block a named
   claim (#212); only another active lease on the item does. To find work
   yourself, search `is:open label:factory:available`: the label doctor sets
   that label on items the factory would hand out next.
2. **Request a claim.** Add `factory:claimed`. The controller checks your write
   access and the store fenced lease, then answers on the item:
   - accepted: `factory:claimed` stays and `factory:available` goes;
   - refused: `factory:claimed` goes and `factory:claim-rejected` is added.

   One status comment on the item, edited in place, names the claimant, the
   claim UUID and the expiry. A refusal states the reason (for example, who
   holds the lease and until when) and the next step. Do not start work until
   it says **Accepted** with your login. The step to do comes from the item:
   an issue means implement; on a PR, `factory:merge-conflict` means resolve
   the conflict, `factory:changes-requested` means repair the findings,
   `factory:ci` with failing checks means repair them, `factory:review` means
   review (unless you wrote the PR), and anything else means push commits.
3. **Work.** Your pushes to the claimed PR and your comments on the claimed
   item renew the lease. Commits need one `Model: <exact model id>` trailer
   each.
4. **Finish.** For `implement`, open a non-draft PR whose body says
   `Closes #N`; that is the evidence and it hands off at once. For a repair,
   push the fix. For a review, submit it (see the review evidence above). Then
   remove `factory:claimed`. The controller hands off on evidence or releases
   the claim without evidence, and dispatches the factory's next step.

Limits:

- A label claim records only the GitHub login that added the label, with
  client `label`. Agents that share one GitHub login cannot be told apart, and
  either can finish the other's claim.
- Only the claimant (or the operator's `baste revoke`) can end a
  claim. When anyone else removes `factory:claimed`, the controller puts it
  back.
- Events from bots, from forks and from reruns are ignored.

### Label doctor

`baste labels` audits every open factory issue and PR and prints
the exact label changes, with reasons, without writing anything.
`--repair` applies them; `--repair --delete-legacy` also deletes the obsolete
label definitions it lists. The label claim workflow runs the repair every hour
and after each claim event. The doctor:

- adds or removes `factory:available` from the frontier and the factory store;
- removes `factory:claimed` that no store claim backs, after a 10-minute grace
  period, and restores it when a label claim exists;
- clears `factory:claim-rejected` after an hour;
- collapses several stage labels to the one the work policy already reads,
  and applies the work policy's own owner and stage rules;
- removes `factory:merge-conflict` from issues;
- removes obsolete and malformed labels. A quoted label outside the factory
  and Ralph names, such as `"bug"`, becomes `bug`; a quoted factory or Ralph
  label such as `"factory:ready"` is only removed, so the doctor never invents
  a stage, an owner or a priority;
- leaves manual-only items untouched, as every factory label writer does;
- withholds `factory:available` from a PR whose required checks are still
  running, because a claim on it would be refused.

When the factory store cannot be read, the doctor removes `factory:available` everywhere and
touches no claim. It holds an item with several active owners without changing
the owners. It writes each item only if its labels are unchanged since it read
them, so a second run changes nothing.

## Universal agent instruction

> Do the next meaningful work in this repository. Ask Rotisserie for its authoritative
> ranked work and reserve the highest eligible action using the current shared
> lease authority. Use your own stable client identity. Do not act on a
> read-only candidate or assume labels confer ownership. Never bypass active
> worker claims, unresolved dependencies, capacity limits, live CI or an
> unknown lease state. Follow the returned exact target, goal, prerequisites
> and head SHA. Maintain the same fenced lease through work, then report a
> GitHub PR/issue/run URL as durable evidence and hand it off to the existing
> review, CI, merge and deployment controllers. You must not approve code
> that you authored. If you lack trusted reservation access or your claim is
> refused, stop and report the reason without inventing a reservation.
> Do not force, take over, or recover a claim without controller-verified
> inactivity.

## Lease semantics and recovery

Claims use the existing `factory-lease-v1:<repo>:target:<number>` keys, a
repository-wide PostgreSQL advisory transaction lock, and a unique claim UUID.
An agent's identity is its authenticated GitHub actor plus a stable client ID.
Every renewal, release and handoff must match that identity and all target
fences. Linked issue/PR targets are reserved together. A losing agent receives
a structured `conflict` and can refresh.

An expired external claim is *not* proof that its owner stopped. It stays a
hold: the factory never overwrites it, the expired-lease sweep skips it, and
its owner can no longer renew it. Recovery needs authoritative evidence:

1. **The owner gives it back.** The same actor and client can `release` its
   fenced claim even after expiry.
2. **GitHub says the work is gone.** Every `reserve-next` first removes any
   outside claim whose targets are all closed or merged. The removal is
   UUID-fenced and logged as `external_revoked` with the reason.
3. **The operator says so** (below).

Nothing else frees an outside claim; an elapsed timestamp alone never does.

### Operator override

The operator can always take an item back. Either remove the claim directly:

```bash
baste revoke UUID --reason "phone agent went offline"
```

or take the item over for a factory worker, which removes any outside agent's
claim on it first:

```bash
baste run PROVIDER auto --target pr:123 --take-over
```

Both need the factory store connection (`FACTORY_TURSO_URL` and its token file) and write
access to the repository. Removal is fenced by the claim UUID, so a newer claim
on the same item is never touched, and the event log records who removed whose
claim and why. Nothing removes an outside agent's claim automatically.

Outside claims set no GitHub owner label, so the factory's label-based gates
read the factory store instead: while an outside claim holds a PR or its linked issue, CI
reconciliation leaves the PR alone and merge authorization waits (the PR keeps
`factory:ready` and merges after release, handoff or revoke). An unreadable
claim store holds closed.

External claims count in normal factory WIP and completion backpressure; they
do not consume fictitious numeric provider slots. Owner-promoted priorities and
review independence remain in force. Completing the task needs independent
exact-head review, required CI, acceptance and production verification through
the existing pipeline.
