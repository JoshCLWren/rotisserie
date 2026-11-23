# External next-work claims (issue #177)

Rotisserie owns ranking, GitHub owns PR/review/CI/merge facts, and the existing
Neon factory-state ledger owns work leases. **A frontier candidate is not a
reservation.** No external coding agent needs to join the provider/model roster.

## Agents with a trusted local Neon controller connection

Run from a checkout with the current installed Rotisserie runtime, GitHub CLI
authenticated as the actual agent principal, and a restricted
`FACTORY_STATE_DSN_FILE` provisioned by the existing trusted controller:

```bash
rotisserie factory next --repo JoshCLWren/comic-pile --json
rotisserie factory reserve-next --repo JoshCLWren/comic-pile --client chatgpt --json
```

Only a JSON response with `"status": "reserved"` and an exact `claim_id`
authorizes beginning the returned action. A conflict or hold requires refreshing
the frontier, never silently selecting an unrelated issue. To compete for a
previously advertised exact offer, supply `--target pr:123` (or `issue:123`).
If the top eligible offer changed, Rotisserie refuses and returns a refresh
instruction.

Renew before expiration. For example, use the claim UUID returned above:

```bash
rotisserie factory renew UUID --repo JoshCLWren/comic-pile --client chatgpt --json
rotisserie factory release UUID --repo JoshCLWren/comic-pile --client chatgpt --json
gh workflow run rotisserie-external-claim.yml --repo JoshCLWren/comic-pile --ref main \
  -f operation=handoff -f client=chatgpt -f claim_id=UUID \
  -f evidence=https://github.com/JoshCLWren/comic-pile/pull/123
```

Handoff records evidence in Neon and releases the claim. It **does not**
approve a review, bypass required checks, merge a PR, close the issue, or
authorize deployment. Use ordinary controller gates after recording evidence.

### What an outside agent can take

`implement`, `review-findings`, `ci-repair` and `merge-conflict`. Use the
repository's exact name (`--repo` is compared with GitHub's `full_name`):
lease keys and the advisory lock are built from it. `reserve-next` walks the
ranking and reserves the highest offer the agent can do that is live right
now, so a top PR still waiting on CI does not hide lower work. The
frontier still lists `review` offers, but an outside agent never reserves
them: the merge gate only counts an approval whose reviewer model the trusted
runtime recorded, which an outside agent cannot provide. Independent review
stays with the factory's own reviewers. `--can ACTION` narrows the choice to
what the agent can do; it still receives the highest-ranked offer of those.

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

The PR body must say `Closes #ISSUE` for the claimed issue, and a factory
branch name, if used, must name the same issue: GitHub closes what the body
names while the factory links by branch first.

### Authorship

The handed-off head commit must end with exactly one `Model: <exact model id>`
trailer, or the handoff is refused and the claim stays held. The bridge then
writes a controller-trusted author-model marker for that exact head (agent
`external-CLIENT`), so the model that wrote the work never approves it. Only
the Actions bot's comments count as author records, so **every handoff runs
through the GitHub bridge**: a CLI agent reserves, renews and releases from
the CLI, and `rotisserie factory handoff` prints the exact `gh workflow run`
command to use instead. If the PR head moves while the handoff runs, it is
refused and the claim stays held; hand off the new head.

The GitHub account alone is never treated as a model identity.

Do not put Neon credentials into a model session, its prompt, tool output,
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
gh workflow run rotisserie-external-claim.yml --repo JoshCLWren/comic-pile \
  --ref main -f operation=reserve-next -f client=codebuff-phone
```

Use `gh run list --repo JoshCLWren/comic-pile
--workflow rotisserie-external-claim.yml` to identify the initiated run and
`gh run download RUN_ID --repo JoshCLWren/comic-pile` to retrieve its
machine-readable result. Match the workflow run you triggered, not another
agent's run. The GitHub bridge supports `renew`, `release`, and `handoff`
with the same `client`, `claim_id` and (for handoff) a same-repository
`evidence` URL.

The manual workflow is a trusted **controller**, not a coding-worker workflow.
It requires the GitHub initiating actor to retain write permission, refuses
reruns and non-manual events, obtains only the existing restricted Neon role,
and exports no DSN. A read-only GitHub token or a missing Neon connection does
not grant permission to reserve. GitHub labels are advisory projections.

## Universal agent instruction

> Do the next meaningful work in ComicPile. Ask Rotisserie for its authoritative
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
rotisserie factory revoke UUID --reason "phone agent went offline"
```

or take the item over for a factory worker, which removes any outside agent's
claim on it first:

```bash
rotisserie factory run PROVIDER auto --target pr:123 --take-over
```

Both need the factory's Neon connection (`FACTORY_STATE_DSN_FILE`) and write
access to the repository. Removal is fenced by the claim UUID, so a newer claim
on the same item is never touched, and the event log records who removed whose
claim and why. Nothing removes an outside agent's claim automatically.

External claims count in normal factory WIP and completion backpressure; they
do not consume fictitious numeric provider slots. Owner-promoted priorities and
review independence remain in force. Completing the task needs independent
exact-head review, required CI, acceptance and production verification through
the existing pipeline.
