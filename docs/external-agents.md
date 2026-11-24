# External agents: ranked work frontier (issue #177)

Rotisserie is the scheduling authority. GitHub issues and labels are useful
projections, **not permission** for an external agent to start a job.

## Inspecting the frontier

An installed Rotisserie CLI can display the same deterministic work-policy
candidate order as the factory runtime:

```sh
baste next --repo acme/widgets --json
```

Schema `1` includes the repository, 60-second observation expiry, ordered
candidate rank, exact target URL, action, instruction, policy lane/priority,
dependency hints, reported owner, exact PR head, known Neon target lease
details, capacity counters, reason, holds and provenance hints.

The result is **read-only**. The consumer must not treat `status=candidate`,
`rank=1`, `factory:unowned`, a lease expiry, or the absence of a GitHub
owner label as a successful reservation. `requires_claim=true` applies to
every response. If the CLI cannot inspect the live Neon ledger, all candidates
are held and `authoritative_claim_available=false`.

The list is a *snapshot* of the existing deterministic controller policy, not
an independent scheduler. A Jev decision made later, a new claim, a changed
PR head, an Actions job still alive after a lease timeout, or changed
backpressure can invalidate an earlier order. Distinct `parallel_group`
values identify target scopes, **not** proven conflict-free concurrent work.
Unknown file overlap, worker capacity and liveness are explicitly uncertain.

## GitHub-only read access

Once the adopter has installed `rotisserie-external-frontier.yml` from a
Rotisserie revision containing `factory next`, a GitHub-only agent can:

1. Open **Actions > Rotisserie External Work Frontier > Run workflow** on the
   adopter's main branch. This is manual-only, with no worker dispatched.
2. Inspect the job summary for the ranked view, or download the
   `rotisserie-work-frontier-RUN_ID` JSON artifact (retained one day).
3. Treat every record as a **short-lived observation**, not a reservation.
   The GitHub Actions job never changes issue ownership, PR labels, leases or
   approval state. Missing Neon observations are marked held.

The installer injects a checkout of the selected Rotisserie revision and a
factory runtime overlay. The workflow installs from **that pinned local copy**,
not an untrusted or floating remote package. The existing restricted
`factory_state` database connection is supplied only to the trusted runner;
agents obtain only secret-free JSON.

To actually reserve work from GitHub alone, use the trusted claim bridge
(`rotisserie-external-claim.yml`) described in
[external-agent-claims.md](external-agent-claims.md). Never claim by editing
labels or issue comments.

## Universal external-agent instruction

> Ask Rotisserie for the next meaningful work in the target repository.
> Read the ordered actions and their holds. Reserve the highest-ranked action
> you can perform **through the trusted controller**, never by editing labels
> or posting an issue comment. Revalidate the task/head, preserve the lease,
> do the action, report evidence, and release or hand off the claim. Let
> existing review, CI, merge and production gates decide completion.
> If a trusted claim interface is unavailable, report that missing access
> and **do not start changing code**.

## Reserving work

`next` never reserves anything. Reservation, renewal, release, handoff and
operator recovery are `reserve-next`, `renew`, `release`, `handoff` and
`revoke`, over the same Neon target leases the factory uses. See
[external-agent-claims.md](external-agent-claims.md).
