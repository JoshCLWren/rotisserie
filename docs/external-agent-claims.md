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
rotisserie factory handoff UUID --repo JoshCLWren/comic-pile --client chatgpt \
  --evidence https://github.com/JoshCLWren/comic-pile/pull/123 --json
```

Handoff records evidence in Neon and releases the claim. It **does not**
approve a review, bypass required checks, merge a PR, close the issue, or
authorize deployment. Use ordinary controller gates after recording evidence.

An `implement` claim must produce an open, non-draft PR in the same repository
with an explicit `Closes #ISSUE` reference to the claimed issue. Handing off
that PR URL causes the **trusted controller** to verify the link and project
`factory`, `factory:review` and `factory:unowned` labels onto the PR before
releasing the fenced Neon claim. This enrolls the PR in the **existing**
independent-review queue; it does not authorize merge or replace ownership
with labels. An unrelated PR, issue URL or workflow run cannot complete
an implementation claim. A repair claim must use evidence for its own PR.

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

An expired external claim is *not* proof that its owner stopped. Until the
controller establishes inactivity, it remains an unreconciled hold; the factory
must not silently overwrite it. The same owner can voluntarily release its
fenced claim even after expiry. Recovery without owner cooperation requires
independent authoritative inactivity evidence and explicit reconciliation;
never delete a lease merely because its timestamp elapsed.

External claims count in normal factory WIP and completion backpressure; they
do not consume fictitious numeric provider slots. Owner-promoted priorities and
review independence remain in force. Completing the task needs independent
exact-head review, required CI, acceptance and production verification through
the existing pipeline.
